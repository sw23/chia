"""Unit tests for the SKILL.md -> stage-prompt loader."""

from __future__ import annotations

import os

import pytest

from chia.analysis.mantis import schema, skill_loader


@pytest.fixture()
def skills_root(tmp_path):
    """A minimal fixture dv-mantis checkout with one skill dir."""
    d = tmp_path / "dv-mantis" / "mantis-researcher"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "name: mantis-researcher\n"
        "description: >-\n  Audits synthesizable HDL.\n"
        "---\n\n"
        "# Mantis Researcher\n\nPerform a thorough review of the RTL.\n"
    )
    return str(tmp_path / "dv-mantis")


def test_skill_path_uses_mantis_prefix(skills_root):
    p = skill_loader.skill_path(skills_root, schema.STAGE_RESEARCHER)
    assert p.endswith(os.path.join("mantis-researcher", "SKILL.md"))


def test_unknown_stage_raises():
    with pytest.raises(KeyError):
        skill_loader.skill_path("/x", "not_a_stage")


def test_load_strips_frontmatter(skills_root):
    body = skill_loader.load_skill_body(skills_root, schema.STAGE_RESEARCHER)
    assert body.startswith("# Mantis Researcher")
    assert "name: mantis-researcher" not in body
    assert "Perform a thorough review" in body


def test_missing_skill_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="dv-mantis"):
        skill_loader.load_skill_body(str(tmp_path), schema.STAGE_RESEARCHER)


def test_render_stage_prompt_embeds_paths_and_body(skills_root):
    prompt = skill_loader.render_stage_prompt(
        skills_root, schema.STAGE_RESEARCHER,
        workspace_dir="/ws", design_dir="/design",
        extra="Investigate rtl/dma.sv",
    )
    assert "/design" in prompt and "/ws/findings" in prompt
    assert "BEGIN SKILL: mantis-researcher" in prompt
    assert "Perform a thorough review" in prompt
    assert "Investigate rtl/dma.sv" in prompt
    # harness state contract must be present
    assert "finding-store tools" in prompt


# Opt-in check against a real dv-mantis checkout (mirrors the repo's *_LIVE_TEST
# convention). Set DV_MANTIS_SKILLS_DIR to a real clone to exercise all 12.
@pytest.mark.skipif(
    not os.environ.get("DV_MANTIS_SKILLS_DIR"),
    reason="set DV_MANTIS_SKILLS_DIR to a dv-mantis checkout to run",
)
def test_all_real_skills_loadable():
    root = os.environ["DV_MANTIS_SKILLS_DIR"]
    for stage in schema.ALL_STAGES:
        body = skill_loader.load_skill_body(root, stage)
        assert body and "---" not in body.splitlines()[0]
