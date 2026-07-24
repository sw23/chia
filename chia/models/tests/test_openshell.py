"""Unit tests for the OpenShell sandbox execution transport.

These tests inject a :class:`FakeRunner` as the *inner* runner so no real
``openshell`` binary or live gateway is required.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from chia.base.sandbox_runner import RunResult, SandboxRunner
from chia.models.openshell import (
    OpenShellConfig,
    OpenShellRunner,
    generate_mcp_egress_policy,
)


class FakeRunner(SandboxRunner):
    """Records every ``run`` call and returns a canned result."""

    def __init__(self, stdout: str = "sandbox-abc") -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def run(
        self,
        cmd,
        *,
        cwd=None,
        env=None,
        timeout=None,
        input=None,
    ) -> RunResult:
        self.calls.append(list(cmd))
        return RunResult(returncode=0, stdout=self._stdout, stderr="")

    def popen(self, cmd, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


def _find_call(calls, needle):
    """Return the first recorded call containing *needle* as a token."""
    for call in calls:
        if needle in call:
            return call
    return None


def test_config_builds_with_defaults():
    cfg = OpenShellConfig()
    assert cfg.sandbox_from is None
    assert cfg.policy is None
    assert cfg.providers == {}
    assert cfg.gpu is False
    assert cfg.gateway_url is None
    assert cfg.openshell_bin == "openshell"
    assert cfg.reuse_sandbox is True
    assert cfg.extra_create_args == []
    assert cfg.sandbox_name is None


def test_inline_dict_policy_written_to_tempfile():
    policy = {"network": {"allow": [{"host": "localhost", "port": 9001}]}}
    runner = OpenShellRunner(
        OpenShellConfig(policy=policy), inner=FakeRunner()
    )
    path = runner._write_policy_if_needed()
    assert path is not None
    assert os.path.exists(path)
    contents = open(path).read()
    assert "localhost" in contents
    assert "9001" in contents
    # Cached: same path returned on a second call.
    assert runner._write_policy_if_needed() == path


def test_str_policy_returned_as_is():
    runner = OpenShellRunner(
        OpenShellConfig(policy="/tmp/my-policy.yaml"), inner=FakeRunner()
    )
    assert runner._write_policy_if_needed() == "/tmp/my-policy.yaml"


def test_run_creates_then_execs_and_reuses_sandbox():
    fake = FakeRunner(stdout="my-sandbox")
    runner = OpenShellRunner(
        OpenShellConfig(reuse_sandbox=True, sandbox_name="my-sandbox"),
        inner=fake,
    )
    agent_cmd = ["copilot", "--prompt", "say pong"]

    r1 = runner.run(agent_cmd)
    assert r1.returncode == 0

    # First call: a create then an exec.
    create = _find_call(fake.calls, "create")
    assert create is not None
    assert create[:5] == [
        "openshell", "sandbox", "create", "--name", "my-sandbox",
    ]
    assert "--no-tty" in create

    exec_call = _find_call(fake.calls, "exec")
    assert exec_call is not None
    assert exec_call[:5] == [
        "openshell", "sandbox", "exec", "-n", "my-sandbox",
    ]
    # Wrapped agent argv appears verbatim after "--".
    dash = exec_call.index("--")
    assert exec_call[dash + 1:] == agent_cmd

    # Second run reuses the sandbox: no second create.
    runner.run(agent_cmd)
    creates = [c for c in fake.calls if "create" in c]
    assert len(creates) == 1


def test_create_command_includes_from_gpu_policy():
    fake = FakeRunner()
    runner = OpenShellRunner(
        OpenShellConfig(
            sandbox_from="ubuntu:22.04",
            gpu=True,
            policy={"network": {"allow": []}},
        ),
        inner=fake,
    )
    runner.run(["copilot"])
    create = _find_call(fake.calls, "create")
    assert create is not None
    assert "--from" in create
    assert create[create.index("--from") + 1] == "ubuntu:22.04"
    assert "--gpu" in create
    assert "--policy" in create
    policy_path = create[create.index("--policy") + 1]
    assert os.path.exists(policy_path)


def test_providers_created_when_env_set(monkeypatch):
    monkeypatch.setenv("FOO_KEY", "x")
    fake = FakeRunner()
    runner = OpenShellRunner(
        OpenShellConfig(providers={"FOO_KEY": "footype"}), inner=fake
    )
    runner.run(["copilot"])
    prov = _find_call(fake.calls, "provider")
    assert prov is not None
    assert prov[:3] == ["openshell", "provider", "create"]
    assert prov[prov.index("--type") + 1] == "footype"
    assert "--from-existing" in prov


def test_providers_skipped_when_env_unset(monkeypatch):
    monkeypatch.delenv("FOO_KEY", raising=False)
    fake = FakeRunner()
    runner = OpenShellRunner(
        OpenShellConfig(providers={"FOO_KEY": "footype"}), inner=fake
    )
    runner.run(["copilot"])
    assert _find_call(fake.calls, "provider") is None


def test_generate_mcp_egress_policy_one_entry_per_tool():
    t1 = SimpleNamespace(name="calc", hostname="localhost", port=9001)
    t2 = SimpleNamespace(name="sim", hostname="10.0.0.5", port=9002)
    policy = generate_mcp_egress_policy(
        [t1, t2], binaries=["/usr/local/bin/copilot"]
    )
    block = policy["network_policies"]["chia_mcp_tools"]
    endpoints = block["endpoints"]
    assert endpoints == [
        {"host": "localhost", "port": 9001},
        {"host": "10.0.0.5", "port": 9002},
    ]
    # Agent binaries are bound to the endpoints (OpenShell requires both to match).
    assert block["binaries"] == [{"path": "/usr/local/bin/copilot"}]
    assert policy["version"] == 1


def test_generate_mcp_egress_policy_merges_base_without_mutation():
    base = {
        "version": 1,
        "filesystem_policy": {"read_write": ["/workspace/design"]},
    }
    t1 = SimpleNamespace(name="calc", hostname="localhost", port=9001)
    policy = generate_mcp_egress_policy([t1], base_policy=base)
    # Base filesystem section preserved, network rule added.
    assert policy["filesystem_policy"] == {"read_write": ["/workspace/design"]}
    block = policy["network_policies"]["chia_mcp_tools"]
    assert block["endpoints"] == [{"host": "localhost", "port": 9001}]
    # No binaries supplied -> empty list (OpenShell will deny until set).
    assert block["binaries"] == []
    # Inputs not mutated.
    assert "network_policies" not in base


def test_popen_raises_not_implemented():
    runner = OpenShellRunner(OpenShellConfig(), inner=FakeRunner())
    with pytest.raises(NotImplementedError):
        runner.popen(["copilot"])
