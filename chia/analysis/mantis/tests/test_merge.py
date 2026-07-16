"""Unit tests for deterministic dedupe-merge and calibration."""

from __future__ import annotations

from chia.analysis.mantis import merge, schema


def _f(**kw):
    base = {
        "id": "primary",
        "title": "t",
        "description": "d",
        "code_paths": ["rtl/a.sv:1"],
        "severity": "MEDIUM",
        "privileges_required": "NONE",
        "user_interaction": "NONE",
        "history": [{"stage": "researcher", "action": "created", "details": ""}],
    }
    base.update(kw)
    return base


def test_merge_unions_paths_takes_max_severity_concats_history():
    primary = _f(id="p", code_paths=["rtl/a.sv:1"], severity="LOW")
    dup1 = _f(id="d1", code_paths=["rtl/a.sv:1", "rtl/b.sv:2"], severity="CRITICAL")
    dup2 = _f(id="d2", code_paths=["rtl/c.sv:3"], severity="HIGH")

    merged = merge.merge_findings(primary, [dup1, dup2])

    assert merged["code_paths"] == ["rtl/a.sv:1", "rtl/b.sv:2", "rtl/c.sv:3"]
    assert merged["severity"] == "CRITICAL"          # most severe wins
    # primary history + both dups' history + the merge record
    assert merged["history"][-1]["action"] == "merged"
    assert "d1" in merged["history"][-1]["details"] and "d2" in merged["history"][-1]["details"]
    assert len(merged["history"]) == 1 + 1 + 1 + 1


def test_merge_does_not_mutate_inputs():
    primary = _f(id="p", code_paths=["rtl/a.sv:1"])
    before = list(primary["code_paths"])
    merge.merge_findings(primary, [_f(id="d", code_paths=["rtl/z.sv:9"])])
    assert primary["code_paths"] == before


def test_calibrate_uses_canonical_formula_and_bands():
    # Hazard = (Impact + Likelihood) * Multiplier; reproduced -> likelihood 5.
    crit = merge.calibrate(_f(severity="CRITICAL", privileges_required="NONE",
                              user_interaction="NONE", repro_status="reproduced"))
    assert crit["impact_score"] == 5 and crit["likelihood_score"] == 5
    assert crit["mantis_risk_score"] == 10.0          # (5+5)*1.0
    assert crit["priority"] == "CRITICAL"

    low = merge.calibrate(_f(severity="LOW", privileges_required="HIGH",
                             user_interaction="REQUIRED"))
    # impact capped to 2 (HIGH priv); likelihood 3-1(ui)-1(priv)=1; mult 0.7
    assert low["impact_score"] == 2 and low["likelihood_score"] == 1
    assert low["mantis_risk_score"] == round((2 + 1) * 0.7, 1)  # 2.1
    assert low["priority"] == "LOW"


def test_calibrate_privilege_caps_impact():
    assert merge.calibrate(_f(severity="CRITICAL", privileges_required="HIGH"))["impact_score"] == 2
    assert merge.calibrate(_f(severity="CRITICAL", privileges_required="LOW"))["impact_score"] == 3
    assert merge.calibrate(_f(severity="CRITICAL", privileges_required="NONE"))["impact_score"] == 5


def test_calibrate_evidence_caps():
    hi = _f(severity="CRITICAL", privileges_required="NONE", user_interaction="NONE")
    # No reproducer -> hazard forced to <= 2.0 (LOW).
    assert merge.calibrate(dict(hi, repro_status="failed_to_reproduce"))["mantis_risk_score"] <= 2.0
    assert merge.calibrate(dict(hi, repro_status="not_attempted"))["priority"] == "LOW"
    # Statically confirmed -> capped at 7.9, never CRITICAL.
    stat = merge.calibrate(dict(hi, repro_status="statically_confirmed"))
    assert stat["mantis_risk_score"] <= 7.9 and stat["priority"] != "CRITICAL"


def test_calibrate_reproduced_bumps_likelihood():
    base = _f(severity="HIGH", privileges_required="LOW", user_interaction="REQUIRED")
    repro = dict(base, repro_status="reproduced")
    assert merge.calibrate(repro)["likelihood_score"] > merge.calibrate(base)["likelihood_score"]


def test_calibrate_floors_false_positive_and_non_viable():
    fp = merge.calibrate(_f(severity="CRITICAL", status=schema.STATUS_FALSE_POSITIVE))
    nv = merge.calibrate(_f(severity="CRITICAL", production_viability="NON_VIABLE"))
    assert fp["mantis_risk_score"] == 0.1 and fp["priority"] == "LOW"
    assert nv["mantis_risk_score"] == 0.1


def test_calibrate_sample_or_test_multiplier_lowers_score():
    prod = merge.calibrate(_f(severity="CRITICAL", privileges_required="NONE",
                              repro_status="reproduced"))
    sample = merge.calibrate(_f(severity="CRITICAL", privileges_required="NONE",
                                repro_status="reproduced",
                                production_viability="SAMPLE_OR_TEST"))
    assert sample["mantis_risk_score"] < prod["mantis_risk_score"]


def test_calibrate_availability_tier_passthrough():
    out = merge.calibrate(_f(), availability_tier="CRITICAL")
    assert out["availability_tier"] == "CRITICAL"
    assert "availability_tier" not in merge.calibrate(_f(), availability_tier=None)


def test_risk_score_within_schema_band():
    for sev in schema.SEVERITY_ORDER:
        for priv in schema.PRIVILEGES:
            for rs in (None, "reproduced", "statically_confirmed", "not_attempted"):
                score = merge.calibrate(_f(severity=sev, privileges_required=priv,
                                           repro_status=rs))["mantis_risk_score"]
                assert 0.1 <= score <= 10.0
