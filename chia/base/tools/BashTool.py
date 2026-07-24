from __future__ import annotations

import os
import signal
import subprocess
from typing import Optional, Dict

from chia.base.tools.ChiaTool import ChiaTool, ToolInfo
from chia.base.sandbox_runner import LocalSubprocessRunner, SandboxRunner


class BashTool(ChiaTool):
    """MCP tool that executes bash commands in its deployed container.

    When constructed with ``task_options``, the tools are setup on
    machines using a setup function which is called remotely with those
    task_options.
    """

    def __init__(
        self,
        name: str,
        work_dir: str = "/",
        timeout_seconds: int = 120,
        task_options: Optional[Dict] = None,
        runner: "SandboxRunner | None" = None,
    ):
        super().__init__(name, task_options=task_options)
        self.work_dir = work_dir
        self.timeout_seconds = timeout_seconds
        self.runner = runner or LocalSubprocessRunner()

        # Short tool name: copilot namespaces MCP tools as "{server}-{tool}" and
        # the combined string must stay under its 64-char limit; the server name
        # (this tool's name) already scopes it.
        self.mcp.add_tool(self.run_command, name="run_command")
        super().__post_init__()

    def run_command(self, command: str) -> str:
        """Run a bash command and return combined stdout/stderr."""
        # start_new_session=True puts the shell and all its descendants in a
        # fresh process group, so on timeout we can kill the whole tree with
        # killpg().  Without this, subprocess.run only kills the immediate
        # ``sh`` and grandchildren keep the stdout/stderr pipes open, which
        # stalls communicate() in cleanup and silently drops the response.
        try:
            proc = self.runner.popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.work_dir,
                start_new_session=True,
            )
        except Exception as e:
            return f"Error: {e}"

        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return f"Error: command timed out after {self.timeout_seconds}s"
        except Exception as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return f"Error: {e}"

        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += "\nSTDERR:\n" + stderr
        if proc.returncode != 0:
            output += f"\n[exit code: {proc.returncode}]"
        return output or "(no output)"
