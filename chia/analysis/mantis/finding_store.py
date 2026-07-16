"""Deterministic state store for the Mantis RTL loop.

``FindingStore`` is the single source of truth for a run: the ``workspace/``
directory that every stage reads from and writes to. Per the dv-mantis
``mantis-pipeline-adapter`` guidance, the *harness* owns all state mutation with
deterministic code — the LLM never writes one-off scripts to append a JSON field
or merge two findings. The same operations are also surfaced to agents as MCP
tools (see :mod:`chia.analysis.mantis.tools`) so a stage that legitimately needs
to mutate state does so through a typed tool call rather than free-form file I/O.

Layout (matches schema.json)::

    workspace/
      findings/<uuid>.json          # one finding per file
      plan.json                     # written by plan, read by researcher
      learnings.jsonl               # ephemeral inbox (reflect/critic/patch -> architecture)
      kb/                           # markdown knowledge base + THREAT_MODEL.md
      archive/iter_<n>/             # findings snapshot per completed iteration

Pure Python + stdlib only, so it is fully unit-testable without a cluster.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional

from chia.analysis.mantis import schema


class FindingStore:
    """File-backed store over a Mantis ``workspace/`` directory."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.findings_dir = os.path.join(self.workspace_dir, "findings")
        self.kb_dir = os.path.join(self.workspace_dir, "kb")
        self.plan_path = os.path.join(self.workspace_dir, "plan.json")
        self.learnings_path = os.path.join(self.workspace_dir, "learnings.jsonl")
        self.archive_dir = os.path.join(self.workspace_dir, "archive")

    # ------------------------------------------------------------------ setup
    def ensure_dirs(self) -> None:
        for d in (self.findings_dir, self.kb_dir, self.archive_dir):
            os.makedirs(d, exist_ok=True)

    # --------------------------------------------------------------- findings
    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def _path(self, finding_id: str) -> str:
        # Guard against path traversal from an LLM-supplied id.
        safe = os.path.basename(finding_id)
        if not safe or safe != finding_id:
            raise ValueError(f"unsafe finding id: {finding_id!r}")
        return os.path.join(self.findings_dir, f"{safe}.json")

    def list_ids(self) -> List[str]:
        if not os.path.isdir(self.findings_dir):
            return []
        return sorted(
            os.path.splitext(n)[0]
            for n in os.listdir(self.findings_dir)
            if n.endswith(".json")
        )

    def read(self, finding_id: str) -> Dict[str, Any]:
        with open(self._path(finding_id), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def read_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for fid in self.list_ids():
            try:
                out.append(self.read(fid))
            except (OSError, json.JSONDecodeError):
                continue  # skip a corrupt file rather than wedge the stage
        return out

    def write(self, finding: Dict[str, Any]) -> str:
        """Write a finding, assigning an id/filename if it lacks one."""
        self.ensure_dirs()
        fid = finding.get("id") or self.new_id()
        finding = dict(finding, id=fid)
        tmp = self._path(fid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(finding, fh, indent=2)
        os.replace(tmp, self._path(fid))  # atomic: no half-written finding
        return fid

    def set_fields(self, finding_id: str, **fields: Any) -> Dict[str, Any]:
        finding = self.read(finding_id)
        finding.update(fields)
        self.write(finding)
        return finding

    def append_history(self, finding_id: str, stage: str, action: str,
                       details: str = "") -> None:
        finding = self.read(finding_id)
        finding.setdefault("history", []).append(
            schema.history_entry(stage, action, details)
        )
        self.write(finding)

    def mark_stage(self, finding_id: str, stage: str,
                   state: str = schema.STAGE_STATE_DONE,
                   ts: Optional[str] = None) -> None:
        """Record that ``stage`` handled ``finding_id`` (schema.json, finding.stage_status).

        Lets a resumed or cached run skip already-processed (stage, finding)
        pairs. ``ts`` is an optional run-scoped counter or timestamp the harness
        supplies; omitted here to keep the store deterministic and clock-free.
        """
        finding = self.read(finding_id)
        entry: Dict[str, Any] = {"state": state}
        if ts is not None:
            entry["ts"] = ts
        finding.setdefault("stage_status", {})[stage] = entry
        self.write(finding)

    def stage_state(self, finding_id: str, stage: str) -> Optional[str]:
        """Return the recorded state of ``stage`` for ``finding_id``, or None."""
        try:
            finding = self.read(finding_id)
        except (OSError, ValueError):
            return None
        entry = (finding.get("stage_status") or {}).get(stage)
        return entry.get("state") if isinstance(entry, dict) else None

    def ids_needing_stage(self, stage: str) -> List[str]:
        """Finding ids not yet marked ``done`` for ``stage`` (fan-out work list).

        Enables idempotent fan-out: a re-run only dispatches the (stage, finding)
        pairs still outstanding.
        """
        return [fid for fid in self.list_ids()
                if self.stage_state(fid, stage) != schema.STAGE_STATE_DONE]

    def delete(self, finding_id: str) -> None:
        try:
            os.remove(self._path(finding_id))
        except FileNotFoundError:
            pass

    def summaries(self) -> List[Dict[str, Any]]:
        """Compact index for the dedupe stage (UUID-referencing pattern).

        Returns ``[{"id", "title", "severity", "file", "line", "snippet"}]`` so
        the LLM reasons over identifiers instead of re-reading every finding.
        """
        out: List[Dict[str, Any]] = []
        for f in self.read_all():
            paths = f.get("code_paths") or []
            first = paths[0] if paths else ""
            file, _, line = str(first).partition(":")
            out.append({
                "id": f.get("id", ""),
                "title": f.get("title", ""),
                "severity": f.get("severity", ""),
                "file": file,
                "line": line,
                "snippet": (f.get("description", "") or "")[:200],
            })
        return out

    # ------------------------------------------------------------------- plan
    def read_plan(self) -> Dict[str, Any]:
        if not os.path.isfile(self.plan_path):
            return {"investigations": []}
        with open(self.plan_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def write_plan(self, plan: Dict[str, Any]) -> None:
        os.makedirs(self.workspace_dir, exist_ok=True)
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)

    def investigations(self) -> List[Dict[str, Any]]:
        inv = self.read_plan().get("investigations")
        return list(inv) if isinstance(inv, list) else []

    # -------------------------------------------------------------- learnings
    def append_learning(self, entry: Dict[str, Any]) -> None:
        os.makedirs(self.workspace_dir, exist_ok=True)
        with open(self.learnings_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def read_learnings(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.learnings_path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.learnings_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def clear_learnings(self) -> None:
        """Cleared by the architecture stage after synthesizing into the KB."""
        try:
            os.remove(self.learnings_path)
        except FileNotFoundError:
            pass

    # -------------------------------------------------------------- archiving
    def archive_iteration(self, iteration: int) -> Optional[str]:
        """Snapshot the current findings into ``archive/iter_<n>/`` and clear.

        Returns the archive path, or ``None`` if there was nothing to archive.
        The loop calls this between iterations so each pass starts clean while
        prior findings are preserved (matches the manual "move workspace/findings
        to an archive directory" step in the dv-mantis README).
        """
        ids = self.list_ids()
        if not ids:
            return None
        dest = os.path.join(self.archive_dir, f"iter_{iteration}")
        os.makedirs(dest, exist_ok=True)
        for fid in ids:
            shutil.move(self._path(fid), os.path.join(dest, f"{fid}.json"))
        return dest
