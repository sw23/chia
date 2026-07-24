"""Tests for stage helpers that don't require a cluster (footer parsing)."""

from __future__ import annotations

import types

from chia.analysis.mantis import stages
from chia.models.openshell import OpenShellRunner


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


# --------------------------------------------------------------------------- #
# OpenShell sandbox wiring (constructed, never invoked -- no live gateway).
# --------------------------------------------------------------------------- #
def _fake_tools():
    return [
        types.SimpleNamespace(name="t1", hostname="10.0.0.1", port=8001),
        types.SimpleNamespace(name="t2", hostname="10.0.0.2", port=8002),
    ]


def test_openshell_runner_for_local_returns_none():
    assert stages._openshell_runner_for({"sandbox": "local"}, []) is None
    # None when the key is absent entirely.
    assert stages._openshell_runner_for({}, []) is None


def test_openshell_runner_for_builds_runner_with_egress():
    cfg = {
        "sandbox": "openshell",
        "openshell": {"providers": {"GITHUB_TOKEN": "github"}},
    }
    runner = stages._openshell_runner_for(cfg, _fake_tools())
    assert isinstance(runner, OpenShellRunner)
    assert runner.config.providers == {"GITHUB_TOKEN": "github"}

    policy = runner.config.policy
    assert isinstance(policy, dict)
    block = policy["network_policies"]["chia_mcp_tools"]
    endpoints = {(e["host"], e["port"]) for e in block["endpoints"]}
    assert ("10.0.0.1", 8001) in endpoints
    assert ("10.0.0.2", 8002) in endpoints


def test_openshell_config_ignores_unknown_keys():
    cfg = {
        "sandbox": "openshell",
        "openshell": {"providers": {}, "bogus_key": 1},
    }
    # Unknown keys must be filtered out, not raise a TypeError.
    runner = stages._openshell_runner_for(cfg, _fake_tools())
    assert isinstance(runner, OpenShellRunner)
    assert not hasattr(runner.config, "bogus_key")


def test_openshell_runner_for_loads_file_policy_and_binds_binaries(tmp_path):
    # A base policy given as a YAML file path is loaded and the MCP egress rule
    # is merged into it, bound to the configured agent binaries.
    policy_file = tmp_path / "lockdown.yaml"
    policy_file.write_text(
        "version: 1\n"
        "filesystem_policy:\n"
        "  read_write: [/workspace/design]\n"
        "network_policies: {}\n"
    )
    cfg = {
        "sandbox": "openshell",
        "openshell": {
            "policy": str(policy_file),
            "agent_binaries": ["/usr/local/bin/copilot"],
        },
    }
    runner = stages._openshell_runner_for(cfg, _fake_tools())
    policy = runner.config.policy
    # Base filesystem lockdown preserved.
    assert policy["filesystem_policy"] == {"read_write": ["/workspace/design"]}
    block = policy["network_policies"]["chia_mcp_tools"]
    endpoints = {(e["host"], e["port"]) for e in block["endpoints"]}
    assert ("10.0.0.1", 8001) in endpoints
    # Agent binary bound so OpenShell admits the tool connection.
    assert block["binaries"] == [{"path": "/usr/local/bin/copilot"}]


def _min_llm_cfg(sandbox: str) -> dict:
    """Minimal cfg exposing every key `_build_llm` reads."""
    return {
        "sandbox": sandbox,
        "openshell": {},
        "backend": "copilot",
        "models": {"frontier": "m"},
        "design_dir": "/tmp",
        "system_prompt": "",
        "reasoning_effort": None,
    }


def test_build_llm_forwards_runner(monkeypatch):
    captured = {}

    class FakeCopilotLLM:
        def __init__(self, *args, **kwargs):
            captured["runner"] = kwargs.get("runner")

    import chia.models.copilot as copilot_mod

    monkeypatch.setattr(copilot_mod, "CopilotLLM", FakeCopilotLLM)

    tools = [types.SimpleNamespace(name="t", hostname="h", port=1)]

    # openshell -> a live OpenShellRunner is threaded into the backend.
    stages._build_llm(_min_llm_cfg("openshell"), "frontier", 60, tools=tools)
    assert isinstance(captured["runner"], OpenShellRunner)

    # local -> runner is None (unchanged default local-subprocess behavior).
    stages._build_llm(_min_llm_cfg("local"), "frontier", 60, tools=tools)
    assert captured["runner"] is None
