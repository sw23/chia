"""OpenShell sandbox execution transport for agent-CLI commands.

This module provides an opt-in :class:`~chia.base.sandbox_runner.SandboxRunner`
implementation that runs the wrapped agent argv inside an
`NVIDIA OpenShell <https://github.com/NVIDIA/OpenShell>`_ policy-governed
sandbox instead of directly on the Ray worker.  It is deliberately
dependency-light: only the standard library and
:mod:`chia.base.sandbox_runner` are imported at module top so it loads cleanly
on a plain machine (no ``ray``/``mcp`` imports here).

The actual ``openshell`` CLI is invoked through an *inner*
:class:`SandboxRunner` (defaulting to
:class:`~chia.base.sandbox_runner.LocalSubprocessRunner`).  Injecting a fake
inner runner in tests makes every command construction path unit-testable
without a real ``openshell`` binary or a live gateway.

A one-shot command is run inside an existing sandbox with
``openshell sandbox exec -n <name> --no-tty -- <argv...>`` (see
:meth:`OpenShellRunner._exec_argv`); streaming (``popen``) is not supported by
this transport.  The network/filesystem policy shapes emitted here follow the
OpenShell policy schema (``version: 1`` with ``filesystem_policy`` and
``network_policies`` sections); see
https://docs.nvidia.com/openshell/sandboxes/policies.
"""

from __future__ import annotations

import copy
import logging
import os
import tempfile
from dataclasses import dataclass, field

from chia.base.sandbox_runner import (
    LocalSubprocessRunner,
    RunResult,
    SandboxRunner,
)

logger = logging.getLogger(__name__)


@dataclass
class OpenShellConfig:
    """Configuration for :class:`OpenShellRunner`.

    Attributes:
        sandbox_from: Value for ``--from <image|dir|catalog>`` when creating
            the sandbox, or ``None`` to omit the flag.
        policy: Either a path to a YAML policy file, or an inline ``dict`` that
            :class:`OpenShellRunner` serializes to a temporary ``.yaml`` file.
        providers: Mapping of environment-variable name to OpenShell provider
            type (e.g. ``{"ANTHROPIC_API_KEY": "anthropic"}``).  For each entry
            whose env var is set, a provider is created from the existing
            credential.
        gpu: Whether to request GPU access (``--gpu``) for the sandbox.
        gateway_url: Optional OpenShell gateway URL.
        openshell_bin: Name/path of the ``openshell`` executable.
        reuse_sandbox: When ``True`` (default), create the sandbox once per
            runner and ``exec`` per turn to avoid per-turn creation latency.
        extra_create_args: Additional raw arguments appended to the
            ``sandbox create`` command.
        sandbox_name: Optional explicit sandbox name; when unset a name is
            parsed from create output or generated.
        agent_binaries: Filesystem paths (inside the sandbox) of the agent CLI
            binaries permitted to reach the chia MCP tool endpoints. OpenShell
            allows an outbound connection only when both the destination
            endpoint *and* the calling binary match, so the generated MCP
            egress rule binds these binaries to the tool endpoints. Must match
            the agent binary location in the sandbox image (e.g.
            ``/usr/bin/copilot`` in the OpenShell base image).
        env: Environment variables injected into the sandbox for each ``exec``
            (as ``--env K=V``). The host process environment is *not* forwarded.
        provider_names: OpenShell provider names attached at creation
            (``--provider``). Use the native ``copilot`` provider so OpenShell
            injects the credential and its inference-endpoint network rules.
        uploads: ``(local_path, sandbox_dest)`` pairs copied into the sandbox via
            ``openshell sandbox upload`` right after creation (e.g. the design
            checkout -> ``/workspace/design``).
        downloads: ``(sandbox_path, local_dest)`` pairs copied back out via
            ``openshell sandbox download`` after each ``run`` (e.g. the agent's
            ``/workspace/design/mantis_workspace`` -> the host workspace dir), so
            the harness's deterministic stages see the agent's writes.
    """

    sandbox_from: str | None = None
    policy: str | dict | None = None
    providers: dict[str, str] = field(default_factory=dict)
    gpu: bool = False
    gateway_url: str | None = None
    openshell_bin: str = "openshell"
    reuse_sandbox: bool = True
    extra_create_args: list[str] = field(default_factory=list)
    sandbox_name: str | None = None
    agent_binaries: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    provider_names: list[str] = field(default_factory=list)
    uploads: list[tuple[str, str]] = field(default_factory=list)
    downloads: list[tuple[str, str]] = field(default_factory=list)


class OpenShellRunner(SandboxRunner):
    """Run wrapped agent commands inside an OpenShell sandbox.

    The ``openshell`` CLI itself is executed via an injected *inner*
    :class:`SandboxRunner` (local subprocess by default), so command
    construction is fully unit-testable without a live gateway.
    """

    def __init__(
        self,
        config: OpenShellConfig,
        *,
        inner: SandboxRunner | None = None,
    ) -> None:
        self.config = config
        self._inner = inner or LocalSubprocessRunner()
        self._sandbox_name: str | None = None
        self._created = False
        self._providers_ensured = False
        self._policy_path: str | None = None

    # -- policy ---------------------------------------------------------

    def _write_policy_if_needed(self) -> str | None:
        """Return a filesystem path to the policy YAML, or ``None``.

        If :attr:`OpenShellConfig.policy` is a ``dict`` it is written once to a
        temporary ``.yaml`` file (cached for reuse).  If it is a ``str`` it is
        returned unchanged.  ``None`` yields ``None``.
        """
        policy = self.config.policy
        if policy is None:
            return None
        if isinstance(policy, str):
            return policy
        if self._policy_path is not None:
            return self._policy_path

        fd = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            prefix="openshell-policy-",
            delete=False,
        )
        try:
            try:
                import yaml  # noqa: PLC0415

                yaml.safe_dump(
                    policy, fd, default_flow_style=False, sort_keys=False
                )
            except ImportError:
                # YAML is a superset of JSON; emitting JSON is a valid
                # fallback.
                import json  # noqa: PLC0415

                json.dump(policy, fd, indent=2)
        finally:
            fd.close()
        self._policy_path = fd.name
        return self._policy_path

    # -- providers ------------------------------------------------------

    def _ensure_providers(self) -> None:
        """Create OpenShell providers from existing credentials (best-effort).

        For each ``{env_var: provider_type}`` in
        :attr:`OpenShellConfig.providers` whose env var is present in
        :data:`os.environ`, run ``openshell provider create --type <type>
        --from-existing``.  Nonzero exits are logged and ignored (the provider
        may already exist).  Runs at most once per runner.
        """
        if self._providers_ensured:
            return
        self._providers_ensured = True
        bin_ = self.config.openshell_bin
        for env_var, provider_type in self.config.providers.items():
            if env_var not in os.environ:
                continue
            cmd = [
                bin_,
                "provider",
                "create",
                "--type",
                provider_type,
                "--from-existing",
            ]
            try:
                result = self._inner.run(cmd)
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning(
                    "openshell provider create failed to launch: %s", exc
                )
                continue
            if result.returncode != 0:
                logger.info(
                    "openshell provider create for %s exited %d (may already "
                    "exist): %s",
                    provider_type,
                    result.returncode,
                    result.stderr.strip(),
                )

    # -- sandbox lifecycle ----------------------------------------------

    def _ensure_sandbox(self) -> str:
        """Create (or reuse) the sandbox and return its name."""
        if self.config.reuse_sandbox and self._created and self._sandbox_name:
            return self._sandbox_name

        # Use a known name so exec/teardown address the sandbox reliably rather
        # than scraping it from create output.
        name = self.config.sandbox_name or f"chia-mantis-{os.getpid()}"
        bin_ = self.config.openshell_bin
        # Persistent, non-interactive sandbox (no initial command; we exec per
        # turn): `openshell sandbox create --name N --no-tty [--from ..]
        # [--policy ..]`.
        cmd: list[str] = [bin_, "sandbox", "create", "--name", name, "--no-tty"]
        if self.config.sandbox_from:
            cmd += ["--from", self.config.sandbox_from]
        if self.config.gpu:
            cmd.append("--gpu")
        for provider in self.config.provider_names:
            cmd += ["--provider", provider]
        policy_path = self._write_policy_if_needed()
        if policy_path:
            cmd += ["--policy", policy_path]
        cmd += self.config.extra_create_args
        # A trivial initial command makes `sandbox create` run it and RETURN
        # (the sandbox persists because --no-keep is not set). Without a command,
        # `openshell sandbox create` attaches/blocks and never returns, hanging
        # the runner before it can upload/exec.
        cmd += ["--", "true"]

        self._inner.run(cmd)
        self._sandbox_name = name
        self._created = True
        # Copy files (e.g. the design checkout) into the freshly created sandbox.
        for local_path, dest in self.config.uploads:
            self._inner.run(
                [bin_, "sandbox", "upload", name, local_path, dest]
            )
        return name

    def _exec_argv(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: str | None = None,
    ) -> list[str]:
        """Return ``openshell`` argv running *cmd* inside sandbox *name*.

        Invocation form: ``openshell sandbox exec -n <name> --no-tty
        [--workdir <cwd>] [--env K=V ...] -- <argv...>`` (streams stdout, exits
        with the remote command's exit code).  Environment injected into the
        sandbox comes from :attr:`OpenShellConfig.env` -- the host process
        environment is intentionally *not* forwarded (the sandbox has its own).
        """
        argv = [
            self.config.openshell_bin, "sandbox", "exec", "-n", name, "--no-tty",
        ]
        if cwd:
            argv += ["--workdir", cwd]
        for key, value in self.config.env.items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--", *cmd]
        return argv

    # -- SandboxRunner API ----------------------------------------------

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> RunResult:
        """Run *cmd* inside the OpenShell sandbox and return its output.

        *cwd* becomes the sandbox ``--workdir``; the host *env* is intentionally
        ignored (sandbox env injection comes from
        :attr:`OpenShellConfig.env`).  *timeout*/*input* apply to the local
        ``openshell exec`` process.
        """
        self._ensure_providers()
        name = self._ensure_sandbox()
        exec_argv = self._exec_argv(name, cmd, cwd=cwd)
        result = self._inner.run(
            exec_argv,
            timeout=timeout,
            input=input,
        )
        # Sync agent-written files back to the host so downstream (deterministic)
        # stages and reporting can read them. `download` copies the folder's
        # contents into local_dest, so ensure it exists first.
        for sandbox_path, local_dest in self.config.downloads:
            os.makedirs(local_dest, exist_ok=True)
            self._inner.run(
                [self.config.openshell_bin, "sandbox", "download",
                 name, sandbox_path, local_dest]
            )
        return result

    def popen(self, cmd, *args, shell: bool = False, **kwargs):
        """Streaming execution is not supported by this transport."""
        raise NotImplementedError(
            "OpenShellRunner streaming (popen) is not yet supported; use a "
            "non-streaming backend or LocalSubprocessRunner"
        )

    # -- teardown -------------------------------------------------------

    def close(self) -> None:
        """Tear down a created sandbox (best-effort)."""
        if not self._created or not self._sandbox_name:
            return
        cmd = [
            self.config.openshell_bin,
            "sandbox",
            "delete",
            self._sandbox_name,
        ]
        try:
            self._inner.run(cmd)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(
                "openshell sandbox teardown failed to launch: %s", exc
            )
        finally:
            self._created = False
            self._sandbox_name = None

    def __enter__(self) -> OpenShellRunner:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def generate_mcp_egress_policy(
    tools,
    base_policy: dict | None = None,
    binaries: list[str] | None = None,
    rule_name: str = "chia_mcp_tools",
) -> dict:
    """Add an OpenShell ``network_policies`` rule for chia MCP tool endpoints.

    Each tool is expected to expose ``hostname``, ``port``, and ``name``
    attributes; the sandboxed agent reaches it at
    ``http://{hostname}:{port}/{name}/mcp`` (see
    :meth:`chia.models.copilot.CopilotLLM._mcp_config_args`).  This emits a
    single named ``network_policies`` block whose ``endpoints`` list contains
    one ``{host, port}`` entry per tool (L4/TCP passthrough -- the proxy allows
    the stream without inspecting payloads) and whose ``binaries`` list binds
    those endpoints to the agent CLI(s) permitted to reach them.

    OpenShell denies all outbound traffic by default and admits a connection
    only when *both* the destination endpoint and the calling binary match an
    entry in the same block.  A base policy with no ``network_policies`` is
    therefore "no network"; this function adds *only* the loopback/on-node MCP
    tool endpoints the harness needs, leaving all other egress denied.

    Args:
        tools: Iterable of chia MCP tool objects (``.hostname/.port/.name``).
        base_policy: Optional base policy dict (e.g. with ``filesystem_policy``
            and no ``network_policies``); merged onto, never mutated.
        binaries: Sandbox-side paths of the agent binaries allowed to use the
            endpoints.  When omitted, an empty ``binaries`` list is emitted and
            OpenShell will deny the connection until binaries are supplied --
            set :attr:`OpenShellConfig.agent_binaries`.
        rule_name: Key for the emitted ``network_policies`` block.

    Returns:
        A new policy ``dict``.  Neither ``tools`` nor *base_policy* is mutated.

    See https://docs.nvidia.com/openshell/sandboxes/policies for the schema.
    """
    result: dict = copy.deepcopy(base_policy) if base_policy else {}
    result.setdefault("version", 1)
    network_policies = result.setdefault("network_policies", {})
    if not isinstance(network_policies, dict):  # pragma: no cover - defensive
        raise ValueError("base_policy['network_policies'] must be a mapping")

    endpoints = []
    for tool in tools:
        host = getattr(tool, "hostname", None)
        port = getattr(tool, "port", None)
        endpoints.append({"host": host, "port": port})

    network_policies[rule_name] = {
        "name": rule_name.replace("_", "-"),
        "endpoints": endpoints,
        "binaries": [{"path": b} for b in (binaries or [])],
    }
    return result

