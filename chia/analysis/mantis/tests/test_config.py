"""Integrity tests for the data-driven pipeline registry."""

from __future__ import annotations

from chia.analysis.mantis import config, schema


def test_pipeline_validates():
    config.validate_pipeline()  # raises on any problem


def test_pipeline_covers_every_stage_once_in_order():
    names = [s.name for s in config.PIPELINE]
    assert names == list(schema.ALL_STAGES)


def test_sim_stages_are_reproduce_and_patch():
    sim_stages = {s.name for s in config.PIPELINE if s.needs_sim}
    assert sim_stages == {schema.STAGE_REPRODUCE, schema.STAGE_PATCH}


def test_calibrate_is_deterministic_no_agent():
    assert config.STAGE_BY_NAME[schema.STAGE_CALIBRATE].uses_agent is False


def test_fanout_findings_stages():
    fan = {s.name for s in config.PIPELINE if s.fanout == config.FANOUT_FINDINGS}
    assert fan == {schema.STAGE_REVIEW, schema.STAGE_CRITIC,
                   schema.STAGE_REPRODUCE, schema.STAGE_PATCH}


def test_researcher_fans_out_over_investigations():
    assert config.STAGE_BY_NAME[schema.STAGE_RESEARCHER].fanout == config.FANOUT_INVESTIGATIONS


def test_deep_stages_are_frontier_tier():
    for name in (schema.STAGE_RESEARCHER, schema.STAGE_REPRODUCE, schema.STAGE_PATCH):
        assert config.STAGE_BY_NAME[name].tier == config.TIER_FRONTIER
