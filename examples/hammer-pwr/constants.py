
from pathlib import Path
from typing import NamedTuple

# Anchor for everything that must resolve to the REAL repo checkout on the
# machine running the driver (the head). Derived from the imported chia
# package (editable install → the checkout), NOT from __file__: under
# `chia job submit` this file executes from the unpacked working_dir zip in
# /tmp/ray/..., where __file__-relative repo paths do not exist — and Ray's
# packaging honors .gitignore, so gitignored files (tools-priv.yml!) never
# make it into that zip at all.
import chia.base.ChiaFunction as _chia_anchor
_CHIA_PKG = Path(_chia_anchor.__file__).resolve().parents[1]
_REPO_ROOT = _CHIA_PKG.parent
_REPO_EXAMPLE_DIR = _REPO_ROOT / "examples" / "hammer-pwr"

CHIPYARDCONF="TinyRocketConfig"
CHIPYARDPATH="/home/ray/chipyard" # Docker container's chipyard path
WORKDIR="/home/ray/verilator/"
ISA_DIR = _REPO_ROOT / "examples" / "benchmarks" / "riscv-tests" / "isa"

# Runtime env shipped to every worker:
#   * working_dir = this example dir, so its flat modules (constants,
#     hammer_power_node) are importable top-level on workers — the dispatched
#     run_joules_power ChiaFunction deserializes by reference there.
#     (__file__-based is fine here: it is only used in direct runs; under
#     `chia job submit` the job ships working_dir and powerloop drops this key.)
#   * py_modules = the head's (current) chia checkout, overriding each worker's
#     baked/installed chia so local CHIA changes ship with the run.
EXAMPLE_DIR = Path(__file__).resolve().parent
RUNTIME_ENV = {
    "working_dir": str(EXAMPLE_DIR),
    "py_modules": [str(_CHIA_PKG)],
    "excludes": ["power-reports/", "__pycache__", ".mypy_cache"],
}

# --- Joules power estimation ---
# Read from the checkout (not the shipped working_dir)
HAMMER_YMLS = _REPO_EXAMPLE_DIR / "hammer-ymls"      # tech/tools configs shipped to the worker
# Hammer obj_dir root on the vlsi (Joules) worker.
# that is the hammer source checkout; per-bmark obj_dirs must not land inside it.
HAMMER_WORKDIR = "/scratch/hammer-pwr/hammer-pwr-runs"
REPORTS_DIR = _REPO_EXAMPLE_DIR / "power-reports"    # local dir: per-bmark Joules reports land here

# --- CACTI SRAM macro characterization ---
# The cacti binary inside the ghcr.io/ucb-bar/chia-cacti image (cluster.yaml's
# `cacti` node type); see chia/vlsi/sram_cacti/cluster/cacti_local.yaml.
CACTI_PATH = "/scratch/cacti/cacti"

# Design-specific power inputs — Joules maps the VCD's signal hierarchy onto
# the DUT via read_stimulus -dut_instance {TB_NAME}/{TB_DUT} (dots in TB_DUT
# become "/"). Verified against an ACTUAL captured VCD's $scope tree (printed
# by hammer_power_node._check_vcd):
#   TOP -> TestDriver -> testHarness -> chiptop0
# Verilator adds the "TOP" root at trace registration AND keeps the top module
# (TestDriver) as a named scope beneath it — both levels are required.
TOP_MODULE = "ChipTop"                           # top RTL module Joules analyzes
TB_NAME    = "TOP"                               # Verilator's VCD root scope
TB_DUT     = "TestDriver.testHarness.chiptop0"   # DUT instance path under it

# Clock constraint fed to Joules as power.inputs.sdc (a create_clock SDC).
# ChipTop's only clock port is clock_uncore (verified against the elaborated
# ChipTop.sv — there is no port named plain "clock"). Period matches design.yml.
# Set CLOCK_PORT="" to skip the SDC (VCD-driven power still runs).
CLOCK_PORT = "clock_uncore"
CLOCK_PERIOD_NS = 10.0
