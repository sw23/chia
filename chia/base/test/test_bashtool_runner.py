"""Tests for :class:`chia.base.tools.BashTool` routed through a SandboxRunner.

BashTool is intentionally a general shell tool: shell operators (``&&``, ``|``)
still work with the default :class:`LocalSubprocessRunner`. Its isolation comes
from the sandbox/container, not an allow-list. These tests avoid deploying a Ray
actor by building a bare instance with ``BashTool.__new__`` and setting only the
attributes ``run_command`` reads.
"""

from __future__ import annotations

from chia.base.tools.BashTool import BashTool
from chia.base.sandbox_runner import LocalSubprocessRunner


def _make_bash(tmp_path, timeout_seconds=30):
    obj = BashTool.__new__(BashTool)
    obj.work_dir = str(tmp_path)
    obj.timeout_seconds = timeout_seconds
    obj.runner = LocalSubprocessRunner()
    return obj


def test_shell_chaining_still_works(tmp_path):
    bash = _make_bash(tmp_path)
    output = bash.run_command("echo hi && echo bye")
    assert "hi" in output
    assert "bye" in output


def test_nonzero_exit_reports_exit_code(tmp_path):
    bash = _make_bash(tmp_path)
    output = bash.run_command("exit 7")
    assert "[exit code: 7]" in output
