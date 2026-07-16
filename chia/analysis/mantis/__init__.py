"""Mantis RTL design-bug loop for CHIA.

A deterministic CHIA harness around the dv-mantis hardware review *skills*
(https://github.com/sw23/dv-mantis). The dv-mantis ``SKILL.md`` files supply the
per-stage agent instructions and ``schema.json`` supplies the inter-stage state
contract; this package supplies the CHIA nodes, the deterministic state store,
and the deterministic transforms that let the two run together to hunt design
bugs in open-source RTL.

Design split:
  * :mod:`~chia.analysis.mantis.schema`        — state contract (schema.json in code)
  * :mod:`~chia.analysis.mantis.finding_store` — deterministic workspace store
  * :mod:`~chia.analysis.mantis.merge`         — dedupe-merge + risk calibration
  * :mod:`~chia.analysis.mantis.skill_loader`  — SKILL.md -> agent prompt
  * :mod:`~chia.analysis.mantis.config`        — the pipeline as data (StageSpec)
  * :mod:`~chia.analysis.mantis.tools`         — MCP tools (state + simulators)
  * :mod:`~chia.analysis.mantis.stages`        — one @ChiaFunction per stage

The runnable loop lives at ``examples/mantis_rtl_loop/``.

Only the pure-Python modules are imported eagerly. ``tools`` and ``stages`` pull
in Ray / MCP and are imported on demand so this package loads on a plain machine
(for tests and offline inspection) without a cluster.
"""

from chia.analysis.mantis import config, finding_store, merge, schema, skill_loader
from chia.analysis.mantis.config import PIPELINE, StageSpec
from chia.analysis.mantis.finding_store import FindingStore

__all__ = [
    "schema",
    "finding_store",
    "merge",
    "skill_loader",
    "config",
    "FindingStore",
    "PIPELINE",
    "StageSpec",
]
