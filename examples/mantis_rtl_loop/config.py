"""Configuration for the Mantis RTL design-bug loop.

Project convention (see circt_issue_solver): parameters are module globals here,
not environment variables. The one secret — the coding-agent credential — is
mounted into the design container via cluster.yaml, not set here.

Point ``SKILLS_ROOT`` at a checkout of https://github.com/sw23/dv-mantis and
``TARGET_REPO`` at the open-source RTL you want reviewed. The defaults review
picorv32 (a small, single-file Verilog CPU that simulates cleanly under Icarus
Verilog / Verilator), which keeps the reference loop fast to stand up. The loop
itself is design-agnostic: only these globals change to retarget it.
"""

from __future__ import annotations

import os
from pathlib import Path

FLOW_DIR = Path(__file__).resolve().parent
_CHIA_PKG = FLOW_DIR.parent.parent / "chia"

# --------------------------------------------------------------------------- #
# What to review, and with which skills.
# --------------------------------------------------------------------------- #
# A dv-mantis checkout (the review skills + schema.json). Override with the env var
# if your clone lives elsewhere; otherwise this assumes a sibling of the chia repo.
SKILLS_ROOT = os.environ.get(
    "DV_MANTIS_SKILLS_DIR", str(FLOW_DIR.parent.parent.parent / "dv-mantis")
)

# The design under review, checked out inside the design container at this path.
TARGET_REPO_URL = "https://github.com/YosysHQ/picorv32"
TARGET_REPO_COMMIT = "main"
DESIGN_DIR = "/workspace/design"                 # design checkout on the worker
WORKSPACE_DIR = "/workspace/design/mantis_workspace"  # findings/plan/kb/learnings
SIM_WORK_DIR = "/workspace/design"               # sandbox cwd for the sim tool

# --------------------------------------------------------------------------- #
# Coding-agent backend + model tiering (README "Tiered Efficiency").
# Frontier models for deep reasoning (research/reproduce/patch/critic/chain),
# a cheaper utility model for fast structured sweeps (threat_model/plan/dedupe/
# review/reflect). Swap BACKEND to "claude" (and CREDS_RESOURCE accordingly) to
# drive Claude Code instead.
# --------------------------------------------------------------------------- #
BACKEND = "copilot"
CREDS_RESOURCE = "copilot_creds"
REASONING_EFFORT = "high"
MODELS = {
    "frontier": "claude-opus-4-8",     # deep reasoning stages
    "utility": "claude-haiku-4-5",     # fast utility stages
}

# --------------------------------------------------------------------------- #
# Loop bounds + sandbox policy.
# --------------------------------------------------------------------------- #
MAX_ITERATIONS = 3          # reflect -> architecture next-loop passes
MAX_REVERIFY_ROUNDS = 1     # patch -> reproduce re-verification rounds
PENDING_TIMEOUT_S = 1800    # chia_wait stuck-task detection / retry threshold

# Only these binaries may run through the SimTool (executes AI-generated harnesses).
SIM_ALLOWLIST = ["iverilog", "vvp", "verilator", "verilator_bin", "sby", "yosys", "make"]
SIM_TIMEOUT_S = 1800

ARTIFACT_DIR = FLOW_DIR / "runs"

# Per-stage agent timeouts (seconds). Keyed by stage name.
TIMEOUTS = {
    "architecture": 2400, "threat_model": 1800, "plan": 1800, "researcher": 3600,
    "dedupe": 1200, "review": 1200, "critic": 1800, "reproduce": 3600,
    "chain": 2400, "patch": 5400, "calibrate": 600, "reflect": 1200,
}


def build_cfg() -> dict:
    """Assemble the Ray-serializable cfg dict handed to every stage node."""
    return {
        "backend": BACKEND,
        "creds_resource": CREDS_RESOURCE,
        "reasoning_effort": REASONING_EFFORT,
        "models": MODELS,
        "skills_root": SKILLS_ROOT,
        "design_dir": DESIGN_DIR,
        "workspace_dir": WORKSPACE_DIR,
        "sim_work_dir": SIM_WORK_DIR,
        "sim_allowlist": SIM_ALLOWLIST,
        "sim_timeout_s": SIM_TIMEOUT_S,
        "timeouts": TIMEOUTS,
        "system_prompt": (
            "You are a meticulous hardware design-verification engineer hunting "
            "for RTL design bugs and hardware-security weaknesses. Be rigorous, "
            "avoid false positives, and follow the stage instructions exactly."
        ),
    }


# Modules shipped to workers via Ray runtime_env py_modules, so head-side edits
# reach workers on the next submit with no image rebuild (as in circt_issue_solver).
PY_MODULES = [str(_CHIA_PKG), str(FLOW_DIR / "report.py")]
RUNTIME_ENV_EXCLUDES = ["**/__pycache__", "**/*.pyc", "**/runs/**"]
