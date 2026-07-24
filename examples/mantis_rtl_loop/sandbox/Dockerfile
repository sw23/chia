# Custom OpenShell sandbox image for the Mantis RTL loop.
#
# Built on the OpenShell community base (which already ships the copilot CLI and
# node/npm) and adds:
#   1. A current copilot CLI  — the base pins an older build (1.0.16) that does
#      not know newer model slugs (e.g. gpt-5.6-sol); this installs the latest.
#   2. /workspace/design       — pre-created so the locked-down filesystem_policy
#      (which read_writes /workspace/design) passes sandbox readiness. OpenShell
#      locks the policy at creation, before `openshell sandbox upload` runs, so
#      the path must already exist here.
#   3. iverilog + verilator    — the open-source RTL simulators the reproduce and
#      patch stages drive through the allow-listed SimTool.
#
# Build + use is handled by OpenShell's --from (it builds the Dockerfile into the
# local Docker daemon), wired via OPENSHELL["sandbox_from"] in config.py:
#   OPENSHELL = { ..., "sandbox_from": "<abs path to this directory>" }
FROM ghcr.io/nvidia/openshell-community/sandboxes/base:latest

USER root

# 1. Current copilot CLI (npm global prefix is /usr in the base image).
RUN npm install -g @github/copilot@latest

# 2. Design checkout mount point (must exist before the filesystem policy locks).
RUN mkdir -p /workspace/design && chown -R sandbox:sandbox /workspace

# 3. Open-source RTL simulators for the sim stages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iverilog verilator \
 && rm -rf /var/lib/apt/lists/*

USER sandbox
