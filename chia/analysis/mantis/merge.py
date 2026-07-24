"""Deterministic finding transforms: dedupe-merge and risk calibration.

These are the "harness owns the mutation" half of the ``mantis-dedupe`` and
``mantis-calibrate`` stages. The LLM decides *which* findings are duplicates and
supplies impact/likelihood judgement; the mechanical work — union of code paths,
taking the highest severity, concatenating history, computing a risk score and
priority bucket — happens here in plain Python so it is reproducible and cannot
be corrupted by an agent mangling JSON.

Pure functions, no I/O, no Ray: unit-testable in isolation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from chia.analysis.mantis import schema


def merge_findings(primary: Dict[str, Any],
                   duplicates: Sequence[Dict[str, Any]],
                   stage: str = schema.STAGE_DEDUPE) -> Dict[str, Any]:
    """Merge *duplicates* into *primary*, returning a new merged finding.

    Rules (from the dv-mantis deduplicator contract):
      * ``code_paths``  -> order-preserving union across all findings
      * ``severity``    -> the most severe among all findings
      * ``history``     -> primary's history followed by each duplicate's, plus
                           a merge record naming the absorbed ids
      * other scalar fields keep the primary's value (it is the survivor)
    """
    merged = schema.normalize_finding(primary)

    paths: List[str] = list(merged["code_paths"])
    severity = merged["severity"]
    history: List[Dict[str, str]] = list(merged["history"])
    absorbed: List[str] = []

    for dup in duplicates:
        d = schema.normalize_finding(dup)
        for p in d["code_paths"]:
            if p not in paths:
                paths.append(p)
        severity = schema.more_severe(severity, d["severity"])
        history.extend(d["history"])
        if d.get("id"):
            absorbed.append(d["id"])

    history.append(schema.history_entry(
        stage, "merged",
        f"absorbed duplicate finding(s): {', '.join(absorbed) or '(none)'}",
    ))

    merged["code_paths"] = paths
    merged["severity"] = severity
    merged["history"] = history
    return merged


# --------------------------------------------------------------------------- #
# Calibration. Implements the canonical numeric contract from dv-mantis
# schema.json ("Risk Calibration Formula", in the schema description):
#
#     Hazard = (Impact + Likelihood) * Multiplier,  capped to [0.1, 10.0]
#
# so this deterministic scorer produces the same number the LLM calibrate stage
# would for the mechanical part. The LLM stage additionally applies threat-model
# judgement (blast radius, exposure, the force-downgrade/cap rules) and attaches
# prose (outrage_commentary, executive_summary); those stay with the agent. Here
# we implement the formula, the privilege-based impact caps, the evidence caps,
# and the priority bands — the parts that must be reproducible.
# --------------------------------------------------------------------------- #

# Impact seed by severity (1-5).
_SEVERITY_IMPACT = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2}
# Privileges cap the impact (schema.json / mantis-calibrate): HIGH-priv
# findings cap at 2, LOW-priv at 3, NONE uncapped.
_PRIV_IMPACT_CAP = {"NONE": 5, "LOW": 3, "HIGH": 2}


def impact_score(finding: Dict[str, Any]) -> int:
    """Severity-seeded impact (1-5), capped by the privilege requirement."""
    base = _SEVERITY_IMPACT.get((finding.get("severity") or "").upper(), 1)
    cap = _PRIV_IMPACT_CAP.get((finding.get("privileges_required") or "").upper(), 5)
    return max(1, min(base, cap))


def likelihood_score(finding: Dict[str, Any]) -> int:
    """Likelihood (1-5) driven by reproduction evidence, then trigger ease.

    Mirrors the calibrate rubric: a functional reproducer is 5; a static
    confirmation caps at 3; otherwise use trigger ease as a proxy (trivial for an
    unprivileged, no-interaction bug; lower when a special mode is required).
    """
    rs = finding.get("repro_status")
    if rs == "reproduced":
        return 5
    if rs == "statically_confirmed":
        return 3
    base = 3  # "no functional reproducer but the trigger is trivial to drive"
    if (finding.get("user_interaction") or "").upper() == "REQUIRED":
        base -= 1
    if (finding.get("privileges_required") or "").upper() == "HIGH":
        base -= 1
    return max(1, min(5, base))


def _priority_bucket(risk: float) -> str:
    # Bands per schema.json ("Risk Calibration Formula"): CRITICAL 8.0-10.0, HIGH 6.0-7.9,
    # MEDIUM 3.0-5.9, LOW 0.1-2.9.
    if risk >= 8.0:
        return "CRITICAL"
    if risk >= 6.0:
        return "HIGH"
    if risk >= 3.0:
        return "MEDIUM"
    return "LOW"


def _context_multiplier(finding: Dict[str, Any]) -> float:
    """Deterministic slice of the context multiplier (0.1-1.0).

    Full exposure / asset-criticality weighting needs the threat model and is the
    LLM stage's job; deterministically we apply the two fixed factors from the
    rubric: REQUIRED interaction (*0.7) and SAMPLE_OR_TEST viability (*0.4).
    """
    mult = 1.0
    if (finding.get("user_interaction") or "").upper() == "REQUIRED":
        mult *= 0.7
    if finding.get("production_viability") == "SAMPLE_OR_TEST":
        mult *= 0.4
    return mult


def calibrate(finding: Dict[str, Any],
              availability_tier: Optional[str] = None) -> Dict[str, Any]:
    """Compute impact/likelihood/risk/priority per schema.json ("Risk Calibration Formula").

    Returns a dict of the calibration fields (does not mutate the input).
    FALSE_POSITIVE / NON_VIABLE findings are floored to 0.1 / LOW so they sink in
    a ranked report while remaining auditable.
    """
    imp = impact_score(finding)
    like = likelihood_score(finding)

    invalidated = (
        finding.get("status") == schema.STATUS_FALSE_POSITIVE
        or finding.get("production_viability") == "NON_VIABLE"
    )
    if invalidated:
        risk = 0.1
    else:
        risk = (imp + like) * _context_multiplier(finding)
        rs = finding.get("repro_status")
        # Evidence caps (schema.json, "Risk Calibration Formula").
        if rs == "statically_confirmed":
            risk = min(risk * 0.8, 7.9)   # never CRITICAL on static confirmation
        elif rs in ("failed_to_reproduce", "not_attempted"):
            risk = min(risk, 2.0)         # force LOW without a reproducer
        risk = round(min(10.0, max(0.1, risk)), 1)

    fields = {
        "impact_score": imp,
        "likelihood_score": like,
        "mantis_risk_score": risk,
        "priority": _priority_bucket(risk),
    }
    if availability_tier in schema.AVAILABILITY_TIERS and availability_tier is not None:
        fields["availability_tier"] = availability_tier
    return fields
