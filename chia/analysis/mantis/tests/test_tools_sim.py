"""Tests for :class:`chia.analysis.mantis.tools.SimTool.run_simulation`.

These verify that the allow-list is enforced because commands run **without a
shell** (``shell=False``), which closes the ``"make && curl ... | sh"`` chaining
bypass.

The tests avoid deploying a Ray actor. ``SimTool.__init__`` (via ChiaTool)
stands up an actor, so we build a bare, uninitialized instance with
``SimTool.__new__`` and set only the attributes ``run_simulation`` reads.
"""

from __future__ import annotations

import json

from chia.analysis.mantis.tools import SimTool
from chia.base.sandbox_runner import LocalSubprocessRunner


def _make_sim(tmp_path, allowlist, timeout_seconds=30):
    """Return a bare SimTool wired only with the attrs run_simulation reads."""
    obj = SimTool.__new__(SimTool)
    obj.name = "t"
    obj.work_dir = str(tmp_path)
    obj.timeout_seconds = timeout_seconds
    obj.allowlist = tuple(allowlist)
    obj.runner = LocalSubprocessRunner()
    return obj


def test_chained_bypass_is_closed(tmp_path):
    """`make && echo pwned` must never execute the chained `echo pwned`."""
    sim = _make_sim(tmp_path, allowlist=["make"])
    result = json.loads(sim.run_simulation("make && echo pwned"))
    # With shell=False, `&&`, `echo`, `pwned` are literal args to `make`, which
    # errors; the chained command never runs, so "pwned" is never printed.
    assert "pwned" not in result["stdout"]


def test_disallowed_binary_denied(tmp_path):
    sim = _make_sim(tmp_path, allowlist=["make"])
    result = json.loads(sim.run_simulation("curl http://x"))
    assert result["exit_code"] == -1
    assert "not an allowed" in result["stderr"]


def test_allowed_binary_runs(tmp_path):
    sim = _make_sim(tmp_path, allowlist=["echo"])
    result = json.loads(sim.run_simulation("echo hello"))
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_var_assignment_prefix_stripped_from_argv(tmp_path):
    """A leading VAR=val is applied as env and stripped from the argv."""
    sim = _make_sim(tmp_path, allowlist=["echo"])
    # If the VAR= prefix leaked into argv, `echo` would print it too; with the
    # prefix stripped, `echo hi` runs and stdout is just "hi".
    result = json.loads(sim.run_simulation("FOO=bar echo hi"))
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hi"


def test_timeout_returns_timeout_json(tmp_path):
    sim = _make_sim(tmp_path, allowlist=["sleep"])
    result = json.loads(sim.run_simulation("sleep 5", timeout_seconds=1))
    assert result["exit_code"] == -1
    assert "timed out" in result["stderr"]
