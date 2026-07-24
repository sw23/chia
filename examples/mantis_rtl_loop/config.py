"""Configuration for the Mantis RTL design-bug loop.

Project convention (see circt_issue_solver): parameters are module globals here,
not environment variables. The one secret — the coding-agent credential — is
mounted into the design container via cluster.yaml, not set here.

Point ``SKILLS_ROOT`` at a checkout of https://github.com/sw23/dv-mantis and
``TARGET_REPO`` at the open-source RTL you want reviewed. The defaults review
picorv32 (a small, single-file Verilog CPU that simulates cleanly under Icarus
Verilog / Verilator), which keeps the reference loop fast to stand up. The loop
itself is design-agnostic: only these globals (or their env overrides) change to
retarget it.
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
# Override via env vars to retarget without editing this file (e.g. from a
# design-specific runner shipped alongside the RTL); defaults review picorv32.
TARGET_REPO_URL = os.environ.get("MANTIS_TARGET_REPO", "https://github.com/YosysHQ/picorv32")
TARGET_REPO_COMMIT = os.environ.get("MANTIS_TARGET_COMMIT", "main")
# Paths default to the design-container layout (/workspace/...). For a local
# single-node run (no container), override via MANTIS_DESIGN_DIR etc.
DESIGN_DIR = os.environ.get("MANTIS_DESIGN_DIR", "/workspace/design")
WORKSPACE_DIR = os.environ.get("MANTIS_WORKSPACE_DIR", DESIGN_DIR + "/mantis_workspace")
SIM_WORK_DIR = os.environ.get("MANTIS_SIM_WORK_DIR", DESIGN_DIR)

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
    "frontier": "gpt-5.6-sol",     # deep reasoning stages
    "utility": "gpt-5.6-sol",      # fast utility stages
}

# --------------------------------------------------------------------------- #
# Loop bounds + sandbox policy.
# --------------------------------------------------------------------------- #
MAX_ITERATIONS = 3          # reflect -> architecture next-loop passes
MAX_REVERIFY_ROUNDS = 1     # patch -> reproduce re-verification rounds
PENDING_TIMEOUT_S = 1800    # chia_wait stuck-task detection / retry threshold

# Agent execution sandbox: "openshell" (default; each agent turn runs inside a
# locked-down NVIDIA OpenShell sandbox) or "local" (agent CLI as a plain worker
# subprocess). OpenShell is the default; opt out with MANTIS_SANDBOX=local. See
# docs/user_guides/openshell_agent_nodes.rst.
SANDBOX = os.environ.get("MANTIS_SANDBOX", "openshell")

# OpenShell sandbox configuration (used when SANDBOX == "openshell").
#   * sandbox_from: a custom image (sandbox/Dockerfile) that bakes in a current
#     copilot CLI (the base image's copilot is too old to know newer model slugs
#     like gpt-5.6-sol), pre-creates /workspace/design (so the locked-down
#     filesystem policy passes readiness), and installs iverilog/verilator.
#   * policy: a locked-down base policy (deny-all network except copilot's model
#     endpoints; non-root). chia injects the on-node MCP tool endpoints on top,
#     bound to `agent_binaries`.
#   * providers: map credential env vars to OpenShell provider types so the
#     credential is injected into the sandbox as an env var (never onto disk).
#     Proven: `COPILOT_GITHUB_TOKEN` from `gh auth token` + a current copilot CLI
#     unlocks gpt-5.6-sol in the sandbox.
#   * agent_binaries: sandbox-side path(s) allowed to reach the MCP tools. copilot
#     is a node app, so node makes the network calls -- bind node too.
OPENSHELL = {
    "sandbox_from": str(FLOW_DIR / "sandbox"),
    # copilot authenticates from GITHUB_TOKEN / COPILOT_GITHUB_TOKEN.
    "providers": {"GITHUB_TOKEN": "github"},
    # Native OpenShell provider(s) attached at creation (--provider). The
    # pre-created `copilot` provider injects the credential + copilot's inference
    # network rules. Create it once with:
    #   COPILOT_GITHUB_TOKEN="$(gh auth token)" \
    #     openshell provider create --name copilot --type copilot \
    #     --credential COPILOT_GITHUB_TOKEN
    "provider_names": ["copilot"],
    "policy": str(FLOW_DIR / "lockdown-policy.yaml"),
    # Verified: copilot lives at /usr/bin/copilot and node at /usr/bin/node in the
    # image; node makes the HTTPS calls (including MCP).
    "agent_binaries": ["/usr/bin/node", "/usr/bin/copilot"],
    "gpu": False,
    "reuse_sandbox": True,
}

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
        "sandbox": SANDBOX,
        "openshell": OPENSHELL,
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
