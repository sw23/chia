"""Tests for :mod:`chia.base.sandbox_runner`.

These exercise :class:`LocalSubprocessRunner` against real trivial commands.
They are fast and hermetic: every command uses ``sys.executable`` (the running
interpreter) rather than a bare ``"python"`` for portability, and nothing
touches the network or a live Ray cluster.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from chia.base.sandbox_runner import (
    LocalSubprocessRunner,
    RunResult,
    SandboxRunner,
)


def test_run_returns_runresult_with_streams_and_exit_code():
    runner = LocalSubprocessRunner()
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('hi'); sys.stderr.write('err'); sys.exit(3)",
        ]
    )
    assert isinstance(result, RunResult)
    assert result.returncode == 3
    assert result.stdout == "hi"
    assert "err" in result.stderr


def test_run_honors_cwd(tmp_path):
    runner = LocalSubprocessRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    # Resolve to handle platform path normalization (e.g. /private on macOS).
    import os

    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(tmp_path))


def test_run_honors_env():
    runner = LocalSubprocessRunner()
    env = {"CHIA_SANDBOX_TEST_VAR": "sentinel-value"}
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['CHIA_SANDBOX_TEST_VAR'])",
        ],
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "sentinel-value"


def test_run_raises_on_timeout():
    runner = LocalSubprocessRunner()
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.1,
        )


def test_run_honors_input():
    runner = LocalSubprocessRunner()
    result = runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input="piped-stdin",
    )
    assert result.returncode == 0
    assert result.stdout == "piped-stdin"


def test_popen_returns_usable_process():
    runner = LocalSubprocessRunner()
    proc = runner.popen(
        [sys.executable, "-c", "print('from-popen')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, _stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert stdout.strip() == "from-popen"


def test_local_runner_is_sandbox_runner():
    assert isinstance(LocalSubprocessRunner(), SandboxRunner)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
