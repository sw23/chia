"""MCP tools for Mantis stages.

Two ChiaTools, each stood up on the worker that holds the Mantis workspace /
design checkout (pin with ``task_options`` when constructing):

* :class:`FindingStoreTool` — typed state mutations over ``workspace/findings``.
  Giving the agent these instead of free-form file edits is the dv-mantis
  ``mantis-pipeline-adapter`` recommendation: the harness owns the schema, the
  LLM just calls ``write_finding`` / ``update_finding`` / ``delete_finding`` and
  returns ids, never re-emitting large JSON blobs.

* :class:`SimTool` — runs an allow-listed simulator / formal binary in the
  sandbox. The reproduce and patch stages execute AI-generated testbenches; per
  both project READMEs those MUST run only inside the isolated sim container.
  The allow-list keeps a hallucinated ``$system("rm -rf ...")``-style command
  from reaching arbitrary host binaries through this tool.

This module imports Ray/MCP (via ChiaTool) and is therefore imported on demand by
:mod:`chia.analysis.mantis.stages`, not at package import time.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from chia.base.tools.ChiaTool import ChiaTool
from chia.base.sandbox_runner import LocalSubprocessRunner, SandboxRunner
from chia.analysis.mantis.finding_store import FindingStore


class FindingStoreTool(ChiaTool):
    """Expose FindingStore operations to an agent as MCP tools."""

    def setup(self, workspace_dir: str):
        self.store = FindingStore(workspace_dir)
        self.store.ensure_dirs()
        # Short tool names: copilot namespaces MCP tools as "{server}-{tool}",
        # and the combined string must stay under its 64-char limit. The server
        # name (this ChiaTool's name) already scopes them, so don't re-prefix.
        self.mcp.add_tool(self.list_findings, name="list_findings")
        self.mcp.add_tool(self.read_finding, name="read_finding")
        self.mcp.add_tool(self.write_finding, name="write_finding")
        self.mcp.add_tool(self.update_finding, name="update_finding")
        self.mcp.add_tool(self.append_finding_history, name="append_finding_history")
        self.mcp.add_tool(self.delete_finding, name="delete_finding")
        self.mcp.add_tool(self.finding_summaries, name="finding_summaries")
        self.mcp.add_tool(self.append_learning, name="append_learning")

    def list_findings(self) -> List[str]:
        """Return the ids of all findings currently in the workspace."""
        return self.store.list_ids()

    def read_finding(self, finding_id: str) -> Dict[str, Any]:
        """Return the full JSON of one finding by id."""
        return self.store.read(finding_id)

    def write_finding(self, finding_json: str) -> str:
        """Create or overwrite a finding.

        Args:
            finding_json: the finding as a JSON object string (schema.json fields).
                If it has no ``id`` a new UUID is assigned.
        Returns the finding id that was written.
        """
        finding = json.loads(finding_json)
        return self.store.write(finding)

    def update_finding(self, finding_id: str, fields_json: str) -> str:
        """Merge ``fields_json`` (a JSON object) into an existing finding.

        Use this for stage outputs such as ``{"status": "VALID", "reasoning":
        "..."}`` rather than rewriting the whole finding. Returns the id.
        """
        self.store.set_fields(finding_id, **json.loads(fields_json))
        return finding_id

    def append_finding_history(self, finding_id: str, stage: str, action: str,
                               details: str = "") -> str:
        """Append one ``{stage, action, details}`` entry to a finding's history."""
        self.store.append_history(finding_id, stage, action, details)
        return finding_id

    def delete_finding(self, finding_id: str) -> str:
        """Delete a finding file (e.g. a duplicate absorbed into another)."""
        self.store.delete(finding_id)
        return finding_id

    def finding_summaries(self) -> List[Dict[str, Any]]:
        """Return a compact ``{id,title,severity,file,line,snippet}`` index.

        Use this in the dedupe stage to reason over identifiers instead of
        re-reading every finding.
        """
        return self.store.summaries()

    def append_learning(self, entry_json: str) -> str:
        """Append one JSON object to learnings.jsonl (the reflect/critic inbox)."""
        self.store.append_learning(json.loads(entry_json))
        return "ok"


# Binaries the SimTool will execute. Covers the common open-source RTL sim /
# formal / Chisel-elaboration flows named in the dv-mantis prerequisites.
DEFAULT_SIM_ALLOWLIST = (
    "iverilog", "vvp", "verilator", "verilator_bin",
    "sby", "yosys", "z3",
    "sbt", "mill", "firtool",
    "make",  # many designs wrap their sim in a Makefile target
)


class SimTool(ChiaTool):
    """Run an allow-listed simulator / formal tool in the sandbox workspace."""

    def setup(self, work_dir: str, timeout_seconds: int = 1800,
              allowlist: Optional[List[str]] = None,
              runner: "SandboxRunner | None" = None):
        self.work_dir = work_dir
        self.timeout_seconds = timeout_seconds
        self.allowlist = tuple(allowlist) if allowlist else DEFAULT_SIM_ALLOWLIST
        self.runner = runner or LocalSubprocessRunner()
        self.mcp.add_tool(self.run_simulation, name="run_simulation")
        self.mcp.add_tool(self.allowed_tools, name="allowed_tools")

    def allowed_tools(self) -> List[str]:
        """List the simulator/formal binaries this tool is permitted to run."""
        return list(self.allowlist)

    def run_simulation(self, command: str, timeout_seconds: Optional[int] = None) -> str:
        """Run a single simulator/formal command in the sandbox and return output.

        The command's program (argv[0], after any leading VAR=val assignments)
        must be in the allow-list — call ``allowed_tools`` to see it. The command
        is executed **without a shell** (``shell=False``): shell operators such
        as ``&&``, ``|``, ``;``, backticks, ``$(...)`` and redirections are NOT
        interpreted and are passed as literal argv tokens. To chain steps, use a
        Makefile target (``make <target>``) or a script the design provides
        rather than shell operators. Leading ``NAME=value`` tokens are applied
        as environment variables (merged onto the current environment). Returns
        a JSON string ``{exit_code, stdout, stderr}`` (output truncated).
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return json.dumps({"exit_code": -1, "stdout": "",
                               "stderr": f"unparseable command: {exc}"})
        # Skip leading NAME=value env assignments to find the real program.
        prog_idx = 0
        while prog_idx < len(argv) and "=" in argv[prog_idx] and "/" not in argv[prog_idx].split("=")[0]:
            prog_idx += 1
        prog = argv[prog_idx].rsplit("/", 1)[-1] if prog_idx < len(argv) else ""
        if prog not in self.allowlist:
            return json.dumps({
                "exit_code": -1, "stdout": "",
                "stderr": f"'{prog}' is not an allowed simulator/formal tool. "
                          f"Allowed: {', '.join(self.allowlist)}",
            })
        # Collect the leading NAME=value tokens as env overrides and use the
        # remaining tokens as the argv executed directly (no shell).
        env = os.environ.copy()
        for tok in argv[:prog_idx]:
            name, _, value = tok.partition("=")
            env[name] = value
        exec_argv = argv[prog_idx:]
        try:
            result = self.runner.run(
                exec_argv, cwd=self.work_dir,
                timeout=timeout_seconds or self.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return json.dumps({"exit_code": -1, "stdout": "",
                               "stderr": "simulation timed out"})
        return json.dumps({
            "exit_code": result.returncode,
            "stdout": result.stdout[-20000:],
            "stderr": result.stderr[-8000:],
        })
