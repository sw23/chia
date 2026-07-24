"""The Mantis pipeline as data.

The user-facing tension the loop resolves: CHIA prefers each unit of work to be
its own ``@ChiaFunction`` node, yet we want the *pipeline* to be data-driven so
its topology, model tiering, fan-out shape, and iteration bounds are all editable
without touching control-flow code. This module is the data half — an ordered
list of :class:`StageSpec` records describing the 12 stages. The node half lives
in :mod:`chia.analysis.mantis.stages`, where each stage is a discrete decorated
function. The driver (:mod:`examples.mantis_rtl_loop.mantis_loop`) walks this
list and dispatches the matching node, so adding/reordering/retiering a stage is
a data edit here.

No Ray import — this is plain data, importable and testable anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from chia.analysis.mantis import schema

# --------------------------------------------------------------------------- #
# Fan-out modes: how the driver expands a stage into concurrent tasks.
# --------------------------------------------------------------------------- #
FANOUT_NONE = "none"                    # one task for the whole workspace
FANOUT_INVESTIGATIONS = "investigations"  # one task per plan.json investigation
FANOUT_FINDINGS = "findings"            # one task per finding on disk

# --------------------------------------------------------------------------- #
# Model tiers (README "Tiered Efficiency"): frontier for deep reasoning, utility
# for fast structured sweeps. The example config maps these to concrete model
# names for the chosen backend.
# --------------------------------------------------------------------------- #
TIER_FRONTIER = "frontier"
TIER_UTILITY = "utility"


@dataclass(frozen=True)
class StageSpec:
    """Declarative description of one pipeline stage.

    :param name: stage id (one of :data:`schema.ALL_STAGES`)
    :param uses_agent: whether the stage dispatches a coding-agent prompt. When
        ``False`` the stage is fully deterministic (pure-Python node) — e.g.
        calibration's scoring math.
    :param tier: model tier to use when ``uses_agent`` is True.
    :param fanout: how the driver expands the stage into concurrent tasks.
    :param needs_sim: whether the stage requires the sandboxed simulator worker
        (it executes AI-generated testbenches / formal harnesses). These stages
        MUST run only inside the isolated sim container.
    :param timeout_s: per-task agent timeout.
    :param summary: one-line human description.
    """

    name: str
    uses_agent: bool
    tier: str
    fanout: str
    needs_sim: bool
    timeout_s: int
    summary: str


# The ordered pipeline. This IS the graph: sequential unless a stage fans out,
# in which case the driver runs its tasks concurrently and barriers before the
# next stage. The reproduce->patch->reproduce re-verification and reflect->architecture
# next-iteration edges are control flow the driver adds around this backbone.
PIPELINE: Tuple[StageSpec, ...] = (
    StageSpec(schema.STAGE_ARCHITECTURE, True, TIER_FRONTIER, FANOUT_NONE, False, 2400,
              "Synthesize design structure + learnings into the markdown KB."),
    StageSpec(schema.STAGE_THREAT_MODEL, True, TIER_UTILITY, FANOUT_NONE, False, 1800,
              "Refine the living THREAT_MODEL.md from the KB."),
    StageSpec(schema.STAGE_PLAN, True, TIER_UTILITY, FANOUT_NONE, False, 1800,
              "Scan boundaries + KB into targeted investigations (plan.json)."),
    StageSpec(schema.STAGE_RESEARCHER, True, TIER_FRONTIER, FANOUT_INVESTIGATIONS, False, 3600,
              "Audit targeted RTL; write findings/<uuid>.json. Fans out per investigation."),
    StageSpec(schema.STAGE_DEDUPE, True, TIER_UTILITY, FANOUT_NONE, False, 1200,
              "Group duplicates (LLM) then merge deterministically."),
    StageSpec(schema.STAGE_REVIEW, True, TIER_UTILITY, FANOUT_FINDINGS, False, 1200,
              "Filter false positives per finding."),
    StageSpec(schema.STAGE_CRITIC, True, TIER_FRONTIER, FANOUT_FINDINGS, False, 1800,
              "Check silicon viability per finding; append learnings."),
    StageSpec(schema.STAGE_REPRODUCE, True, TIER_FRONTIER, FANOUT_FINDINGS, True, 3600,
              "Write + run a testbench/SVA/formal property in the sim sandbox."),
    StageSpec(schema.STAGE_CHAIN, True, TIER_FRONTIER, FANOUT_NONE, False, 2400,
              "Combine validated findings into multi-step bug chains."),
    StageSpec(schema.STAGE_PATCH, True, TIER_FRONTIER, FANOUT_FINDINGS, True, 5400,
              "Generate + verify an RTL fix in the sim sandbox; re-verify."),
    StageSpec(schema.STAGE_CALIBRATE, False, TIER_UTILITY, FANOUT_NONE, False, 600,
              "Deterministically score impact/likelihood/risk/priority."),
    StageSpec(schema.STAGE_REFLECT, True, TIER_UTILITY, FANOUT_NONE, False, 1200,
              "Extract trajectory insights into the learnings inbox."),
)

STAGE_BY_NAME: Dict[str, StageSpec] = {s.name: s for s in PIPELINE}


def validate_pipeline() -> None:
    """Sanity-check the registry (called by tests and at driver start-up)."""
    names = [s.name for s in PIPELINE]
    if len(names) != len(set(names)):
        raise ValueError("duplicate stage in PIPELINE")
    for s in PIPELINE:
        if s.name not in schema.ALL_STAGES:
            raise ValueError(f"unknown stage {s.name!r}")
        if s.fanout not in (FANOUT_NONE, FANOUT_INVESTIGATIONS, FANOUT_FINDINGS):
            raise ValueError(f"bad fanout {s.fanout!r} for {s.name}")
        if s.tier not in (TIER_FRONTIER, TIER_UTILITY):
            raise ValueError(f"bad tier {s.tier!r} for {s.name}")
    # Every stage in the canonical Mantis flow must be represented exactly once.
    if set(names) != set(schema.ALL_STAGES):
        missing = set(schema.ALL_STAGES) - set(names)
        raise ValueError(f"PIPELINE missing stages: {sorted(missing)}")
