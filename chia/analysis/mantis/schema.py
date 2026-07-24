"""State schema for the Mantis RTL design-bug loop.

This is a Python encoding of the inter-stage data contract defined in the
dv-mantis ``schema.json`` (https://github.com/sw23/dv-mantis). The Mantis skills
pass state through a directory of finding files (``workspace/findings/*.json``);
this module names the fields, enumerates their legal values, and provides small
helpers to validate / normalize a finding dict so the deterministic parts of the
harness (dedupe merge, calibration, reporting) never depend on an LLM to keep the
state well-formed.

Nothing here imports Ray or any CHIA runtime machinery, so it is importable and
unit-testable without a cluster.
"""

from __future__ import annotations

from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# Pipeline stages (the 12 execution stages the loop orchestrates). The optional
# pre/post stages (history, summarize, report) and the meta-agent supervisor are
# intentionally omitted: in a deterministic CHIA harness the driver *is* the
# supervisor, history/summarize are opt-in enrichment, and reporting is done
# deterministically in Python (see examples/mantis_rtl_loop/report.py).
# --------------------------------------------------------------------------- #
STAGE_ARCHITECTURE = "architecture"
STAGE_THREAT_MODEL = "threat_model"
STAGE_PLAN = "plan"
STAGE_RESEARCHER = "researcher"
STAGE_DEDUPE = "dedupe"
STAGE_REVIEW = "review"
STAGE_CRITIC = "critic"
STAGE_REPRODUCE = "reproduce"
STAGE_CHAIN = "chain"
STAGE_PATCH = "patch"
STAGE_CALIBRATE = "calibrate"
STAGE_REFLECT = "reflect"

ALL_STAGES = (
    STAGE_ARCHITECTURE,
    STAGE_THREAT_MODEL,
    STAGE_PLAN,
    STAGE_RESEARCHER,
    STAGE_DEDUPE,
    STAGE_REVIEW,
    STAGE_CRITIC,
    STAGE_REPRODUCE,
    STAGE_CHAIN,
    STAGE_PATCH,
    STAGE_CALIBRATE,
    STAGE_REFLECT,
)

# --------------------------------------------------------------------------- #
# Enumerated field values (see schema.json). Ordered most- to least-severe where
# an ordering is meaningful, so ``SEVERITY_ORDER.index(...)`` gives a rank the
# merge step can compare.
# --------------------------------------------------------------------------- #
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
PRIVILEGES = ("NONE", "LOW", "HIGH")
USER_INTERACTION = ("NONE", "REQUIRED")

# Per-finding, per-stage idempotency markers (schema.json, finding.stage_status).
# A harness writes finding["stage_status"][<stage>] = {"state": ..., "ts": ...} so
# a resumed / cached run can skip (stage, finding) pairs already handled.
STAGE_STATE_DONE = "done"
STAGE_STATE_SKIPPED = "skipped"
STAGE_STATE_ERROR = "error"
STAGE_STATES = (STAGE_STATE_DONE, STAGE_STATE_SKIPPED, STAGE_STATE_ERROR)

STATUS_VALID = "VALID"
STATUS_FALSE_POSITIVE = "FALSE_POSITIVE"
STATUS_PROVISIONALLY_VALID = "PROVISIONALLY_VALID"
STATUS_NEEDS_RESEARCH = "NEEDS_RESEARCH"
STATUS_DUPLICATE = "DUPLICATE"
REVIEW_STATUSES = (
    STATUS_VALID,
    STATUS_FALSE_POSITIVE,
    STATUS_PROVISIONALLY_VALID,
    STATUS_NEEDS_RESEARCH,
    STATUS_DUPLICATE,
)

VIABILITY = ("VIABLE", "NON_VIABLE", "SAMPLE_OR_TEST", "CONDITIONAL_VIABLE")

# Access starting position and resolved exposure tier (schema.json). New in the
# upstream risk-calibration model; the deterministic scorer leaves the exposure
# weighting to the LLM stage, but the harness carries the fields through.
ACCESS_POSITIONS = (
    "EXTERNAL",
    "INTERNAL_NETWORK",
    "IN_CLUSTER",
    "LOCAL",
    "HOST_SYSTEM",
    "SUPPLY_CHAIN",
    "PHYSICAL_TEMPORARY",
    "PHYSICAL_LONG_TERM",
)
INFERRED_EXPOSURE = ("EXPOSED", "INTERNAL", "PRIVILEGED")
REPRO_STATUSES = (
    "reproduced",
    "statically_confirmed",
    "not_attempted",
    "failed_to_reproduce",
)
PATCH_STATUSES = ("VERIFIED_SECURE", "MITIGATION_PROPOSED", "VERIFICATION_INCOMPLETE", "VERIFICATION_FAILED", "ERROR")
REVERIFY_STATUSES = ("bug_persists", "bug_resolved")
PRIORITY_BUCKETS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
AVAILABILITY_TIERS = ("CRITICAL", "STANDARD", "LOW_CRITICALITY", None)

# Base fields every finding written by the researcher must carry.
BASE_FIELDS = (
    "id",
    "title",
    "description",
    "code_paths",
    "impact",
    "severity",
    "privileges_required",
    "user_interaction",
    "mitigation",
    "history",
)


def default_severity_rank(severity: str) -> int:
    """Rank a severity string; unknown values sort as least-severe."""
    try:
        return SEVERITY_ORDER.index((severity or "").upper())
    except ValueError:
        return len(SEVERITY_ORDER)


def more_severe(a: str, b: str) -> str:
    """Return whichever of two severity strings is the more severe."""
    return a if default_severity_rank(a) <= default_severity_rank(b) else b


def history_entry(stage: str, action: str, details: str = "") -> Dict[str, str]:
    """Build a well-formed ``history`` log entry."""
    return {"stage": stage, "action": action, "details": details}


def normalize_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *finding* with base fields present and typed.

    Missing scalars become empty strings, missing lists become ``[]``. This is
    intentionally permissive: the researcher LLM sometimes omits a field, and we
    would rather carry a normalized record through the deterministic stages than
    crash the whole loop on one malformed file.
    """
    f = dict(finding)
    f.setdefault("id", "")
    f.setdefault("title", "")
    f.setdefault("description", "")
    f.setdefault("impact", "")
    f.setdefault("severity", "LOW")
    f.setdefault("privileges_required", "NONE")
    f.setdefault("user_interaction", "NONE")
    f.setdefault("mitigation", "")
    cp = f.get("code_paths")
    f["code_paths"] = list(cp) if isinstance(cp, (list, tuple)) else ([cp] if cp else [])
    hist = f.get("history")
    f["history"] = list(hist) if isinstance(hist, (list, tuple)) else []
    return f


def validate_finding(finding: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable schema problems (empty == valid).

    Used by tests and by an optional strict mode in the harness; the loop itself
    normalizes rather than rejects, so a single bad finding never wedges a run.
    """
    problems: List[str] = []
    for field in ("id", "title", "description"):
        if not finding.get(field):
            problems.append(f"missing required field: {field}")
    if not finding.get("code_paths"):
        problems.append("code_paths must be a non-empty list")
    sev = (finding.get("severity") or "").upper()
    if sev and sev not in SEVERITY_ORDER:
        problems.append(f"invalid severity: {finding.get('severity')!r}")
    priv = (finding.get("privileges_required") or "").upper()
    if priv and priv not in PRIVILEGES:
        problems.append(f"invalid privileges_required: {finding.get('privileges_required')!r}")
    ui = (finding.get("user_interaction") or "").upper()
    if ui and ui not in USER_INTERACTION:
        problems.append(f"invalid user_interaction: {finding.get('user_interaction')!r}")
    return problems
