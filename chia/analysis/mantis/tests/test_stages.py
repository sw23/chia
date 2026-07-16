"""Tests for stage helpers that don't require a cluster (footer parsing)."""

from __future__ import annotations

from chia.analysis.mantis import stages


def test_parse_result_footer_from_fenced_block():
    text = (
        "I audited the module and wrote findings.\n\n"
        "```json\n"
        '{"stage": "researcher", "status": "ok", "created": ["a", "b"]}\n'
        "```\n"
    )
    footer = stages.parse_result_footer(text)
    assert footer["stage"] == "researcher"
    assert footer["created"] == ["a", "b"]


def test_parse_result_footer_takes_last_block():
    text = (
        "```json\n{\"status\": \"ok\", \"created\": [\"early\"]}\n```\n"
        "then more work\n"
        "```json\n{\"status\": \"ok\", \"created\": [\"final\"]}\n```\n"
    )
    assert stages.parse_result_footer(text)["created"] == ["final"]


def test_parse_result_footer_duplicate_groups():
    text = '```json\n{"stage":"dedupe","status":"ok","duplicate_groups":{"p":["d1","d2"]}}\n```'
    footer = stages.parse_result_footer(text)
    assert footer["duplicate_groups"] == {"p": ["d1", "d2"]}


def test_parse_result_footer_fallback_to_bare_object():
    # No fence, but a trailing JSON object is still recovered.
    assert stages.parse_result_footer('done. {"status": "ok"}')["status"] == "ok"


def test_parse_result_footer_none_when_absent():
    assert stages.parse_result_footer("no json here at all") is None
    assert stages.parse_result_footer("") is None
