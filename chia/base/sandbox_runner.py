"""Pluggable execution transport for agent-CLI subprocess calls.

Agent backends (:class:`~chia.models.copilot.CopilotLLM`,
:class:`~chia.models.claude.ClaudeCodeLLM`) historically shelled out to their
CLI via :func:`subprocess.run` / :class:`subprocess.Popen` directly on the Ray
worker.  This module introduces a thin :class:`SandboxRunner` seam so that the
single subprocess call site in each backend can be routed through a swappable
transport (local subprocess today; an OpenShell sandbox later) without changing
call-site semantics.

:class:`LocalSubprocessRunner` is the default and reproduces today's behavior
exactly: it captures stdout/stderr as text, honors ``cwd``/``env``/``timeout``,
and does **not** swallow exceptions -- ``OSError`` (e.g. ``E2BIG`` for an
oversized argv) and :class:`subprocess.TimeoutExpired` propagate so existing
callers handle them exactly as before.

The module is intentionally dependency-free (no ``ray``/``mcp`` imports) so it
imports cleanly on a plain machine.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import IO, Any


@dataclass
class RunResult:
    """Result of a completed :meth:`SandboxRunner.run` call.

    Mirrors the subset of :class:`subprocess.CompletedProcess` that agent
    backends read: the exit code and the captured text streams.
    """

    returncode: int
    stdout: str
    stderr: str


class SandboxRunner(ABC):
    """Abstract execution transport for agent-CLI commands.

    Implementations execute a command either synchronously (:meth:`run`,
    capturing output) or as a live process (:meth:`popen`, for streaming).
    """

    @abstractmethod
    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> RunResult:
        """Run *cmd* to completion and return its captured output.

        Args:
            cmd: Argument vector to execute (no shell).
            cwd: Working directory, or ``None`` for the current directory.
            env: Environment mapping, or ``None`` to inherit the current env.
            timeout: Seconds before the command is killed, or ``None``.
            input: Text written to the child's stdin, or ``None``.

        Returns:
            A :class:`RunResult` with ``returncode``/``stdout``/``stderr``.
        """
        raise NotImplementedError

    @abstractmethod
    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: str | None = None,
        env: dict | None = None,
        stdin: int | IO[Any] | None = None,
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        text: bool | None = None,
        start_new_session: bool = False,
        shell: bool = False,
    ) -> subprocess.Popen:
        """Start *cmd* as a live process for streaming interaction.

        The keyword arguments mirror :class:`subprocess.Popen`; the returned
        process is owned by the caller (which drains its pipes and waits).
        When ``shell`` is true, *cmd* may be a shell command string and is
        interpreted by the shell (used by :class:`~chia.base.tools.BashTool`).
        """
        raise NotImplementedError


class LocalSubprocessRunner(SandboxRunner):
    """Default transport: run commands as local subprocesses.

    Reproduces the exact semantics agent backends relied on before the
    :class:`SandboxRunner` seam existed.
    """

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> RunResult:
        """Run *cmd* via :func:`subprocess.run` with captured text output.

        Exceptions are **not** swallowed: ``OSError`` (e.g. ``E2BIG``) and
        :class:`subprocess.TimeoutExpired` propagate to the caller unchanged.
        """
        cp = subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        return RunResult(
            returncode=cp.returncode,
            stdout=cp.stdout,
            stderr=cp.stderr,
        )

    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: str | None = None,
        env: dict | None = None,
        stdin: int | IO[Any] | None = None,
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        text: bool | None = None,
        start_new_session: bool = False,
        shell: bool = False,
    ) -> subprocess.Popen:
        """Start *cmd* via :class:`subprocess.Popen`, passing args through.

        ``shell`` is forwarded to :class:`subprocess.Popen`; when true, *cmd*
        may be a command string interpreted by the shell.
        """
        return subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            start_new_session=start_new_session,
            shell=shell,
        )
