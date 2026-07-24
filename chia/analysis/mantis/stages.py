"""Discrete CHIA nodes for the Mantis pipeline — one ``@ChiaFunction`` per stage.

This is the "node" half of the data/node split (see
:mod:`chia.analysis.mantis.config` for the data half). CHIA prefers each unit of
work to carry its own decorator, so every stage is its own dispatchable node
rather than one generic ``run_stage(name)``. The driver walks the data-driven
``PIPELINE`` and dispatches the matching node here; fan-out stages are dispatched
once per investigation / per finding.

Execution model (mirrors ``examples/circt_issue_solver``): a stage node runs on a
``mantis_design`` worker that holds the design checkout + Mantis workspace and has
the simulators installed. The node stands up MCP tools **pinned to its own node**
(so they touch the real checkout) and dispatches the coding-agent prompt **onto
the same node** (NodeAffinity) so the agent's native file tools and sub-agent
swarms operate on those same files. AI-generated testbenches run only through the
allow-listed :class:`~chia.analysis.mantis.tools.SimTool` in that sandbox.

Heavy imports (Ray, models, tools) are done inside the function bodies so this
module — and the whole package — imports on a plain machine without a cluster.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from chia.base.ChiaFunction import ChiaFunction, get
from chia.analysis.mantis import merge, schema, skill_loader

logger = logging.getLogger("mantis.stages")

# The single resource every stage node needs: a design/sim worker. The reproduce
# and patch stages additionally attach a SimTool but run on the same worker type
# (which has iverilog/verilator/sby installed), so one resource label suffices.
DESIGN_RESOURCE = "mantis_design"

# Path the design checkout is uploaded to inside the OpenShell sandbox, where the
# agent runs entirely over this copy. It lives under /sandbox -- a baseline
# read-write path OpenShell always grants -- so the agent reaches it without the
# policy's filesystem_policy having to enumerate it.
SANDBOX_DESIGN_DIR = "/sandbox/design"


# --------------------------------------------------------------------------- #
# Shared helpers (module-level, not nodes).
# --------------------------------------------------------------------------- #
def _here():
    """NodeAffinity scheduling options pinning work to the current node."""
    import ray
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    node = ray.get_runtime_context().get_node_id()
    return {"scheduling_strategy": NodeAffinitySchedulingStrategy(node_id=node, soft=False)}


def _openshell_runner_for(cfg, tools):
    """Return an OpenShellRunner when cfg selects the openshell sandbox, else None.

    The agent runs inside the sandbox with a default-deny network policy, so the
    only egress opened is to the live MCP tool endpoints the harness stands up.
    That rule is injected onto the base policy in ``cfg['openshell']['policy']``
    (a dict, or a YAML file path which is loaded and merged) and bound to the
    agent binaries in ``cfg['openshell']['agent_binaries']`` -- OpenShell admits
    a connection only when both the endpoint and calling binary match.
    """
    if cfg.get("sandbox") != "openshell":
        return None
    import dataclasses

    from chia.models.openshell import (
        OpenShellConfig, OpenShellRunner, generate_mcp_egress_policy,
    )
    osh = dict(cfg.get("openshell") or {})
    base_policy = osh.pop("policy", None)
    binaries = osh.get("agent_binaries") or []

    # A base policy may be an inline dict or a YAML file path. Load a path so we
    # can merge the MCP egress rule into it (otherwise the sandboxed agent would
    # have no route to its own tools under the default-deny network policy).
    if isinstance(base_policy, str):
        import yaml
        with open(base_policy) as fh:
            base_policy = yaml.safe_load(fh) or {}
    # Only open MCP egress when the stage stands up host-MCP tools. When the agent
    # instead uses copilot's built-in tools inside the sandbox (no host MCP), no
    # egress rule is added and the base network lockdown applies as-is.
    if tools:
        policy = generate_mcp_egress_policy(
            tools, base_policy=base_policy, binaries=binaries
        )
    else:
        policy = base_policy

    # Upload the design checkout (incl. its mantis_workspace) into the sandbox,
    # and download the agent's workspace writes back afterward so the harness's
    # host-side (deterministic) stages and reporting see them.
    design_dir = cfg.get("design_dir")

    # Filter to recognized OpenShellConfig fields so unknown cfg keys don't crash.
    field_names = {f.name for f in dataclasses.fields(OpenShellConfig)}
    kwargs = {k: v for k, v in osh.items() if k in field_names}
    if design_dir:
        # Upload the checkout to /sandbox so it lands at /sandbox/design
        # (= SANDBOX_DESIGN_DIR), a baseline read-write path.
        kwargs.setdefault("uploads", [(design_dir, "/sandbox")])
        # `download` copies the folder's CONTENTS into DEST, so target the host
        # mantis_workspace dir directly (not its parent).
        kwargs.setdefault(
            "downloads",
            [(SANDBOX_DESIGN_DIR + "/mantis_workspace",
              design_dir + "/mantis_workspace")],
        )
    config = OpenShellConfig(policy=policy, **kwargs)
    return OpenShellRunner(config)


def _build_llm(cfg: Dict[str, Any], tier: str, timeout_s: int, tools=None):
    """Construct the coding-agent backend for a stage.

    Backend and per-tier model names come from ``cfg`` so the loop is
    config-swappable (Copilot by default; Claude and others supported).

    When ``cfg['sandbox'] == 'openshell'`` an :class:`OpenShellRunner` (whose
    egress policy is derived from ``tools``) is injected so the agent CLI runs
    inside the sandbox; otherwise ``runner`` is ``None`` and the backends fall
    back to their default local-subprocess behavior.
    """
    model = cfg["models"][tier]
    system_message = cfg.get("system_prompt", "")
    backend = cfg.get("backend", "copilot")
    design_dir = cfg["design_dir"]
    effort = cfg.get("reasoning_effort")
    runner = _openshell_runner_for(cfg, tools)
    # In sandbox mode the design is uploaded to a fixed in-sandbox path; the
    # agent's work_dir must be that path, not the host checkout.
    work_dir = SANDBOX_DESIGN_DIR if cfg.get("sandbox") == "openshell" else design_dir

    if backend == "copilot":
        from chia.models.copilot import CopilotLLM
        return CopilotLLM(
            model=model, system_message=system_message,
            timeout_seconds=timeout_s, work_dir=work_dir,
            allow_all=True, reasoning_effort=effort, resume_session=False,
            runner=runner,
            # Disable copilot's built-in remote GitHub MCP server: the harness
            # provides its own tools, and an auto-attached network GitHub tool
            # would breach the review's isolation (the sandbox network policy
            # blocks it too, but this stops it even in local mode).
            extra_cli_args=["--disable-builtin-mcps"],
        )
    if backend == "claude":
        from chia.models.claude import ClaudeCodeLLM
        extra = ["--effort", effort] if effort else None
        return ClaudeCodeLLM(
            model=model, system_message=system_message,
            timeout_seconds=timeout_s, extra_cli_args=extra,
            resume_session=False, projects_cwd=None,
            runner=runner,
        )
    raise ValueError(f"unsupported backend {backend!r} (use 'copilot' or 'claude')")


def _stage_tools(cfg: Dict[str, Any], stage_name: str, needs_sim: bool, here: Dict):
    """Stand up the MCP tools a stage's agent can call, pinned to this node.

    Always provides a bash tool over the design checkout and a finding-store tool
    over the workspace. Sim stages additionally get the allow-listed SimTool.
    Returns ``(tools, cleanup_fn)``.
    """
    from chia.base.tools.BashTool import BashTool
    from chia.analysis.mantis.tools import FindingStoreTool, SimTool

    if cfg.get("sandbox") == "openshell":
        # The agent runs entirely inside the OpenShell sandbox and uses copilot's
        # built-in tools over the uploaded design checkout (sims run on the
        # sandbox image's iverilog/verilator). No host-MCP tools are stood up, so
        # untrusted AI-generated code never executes on the host.
        return [], (lambda: None)

    suffix = f"{stage_name}"
    bash = BashTool(name=f"mantis_bash_{suffix}", work_dir=cfg["design_dir"],
                    timeout_seconds=300, task_options=here)
    store = FindingStoreTool(f"mantis_store_{suffix}", cfg["workspace_dir"],
                             task_options=here)
    tools = [bash, store]
    if needs_sim:
        # SimTool executes AI-generated sim commands through the hardened,
        # shell-free LocalSubprocessRunner: shell operators are not interpreted
        # and the argv[0] allow-list is enforced, making it the isolation
        # boundary for untrusted sim code in local (non-sandbox) mode.
        tools.append(SimTool(f"mantis_sim_{suffix}", cfg["sim_work_dir"],
                             timeout_seconds=cfg.get("sim_timeout_s", 1800),
                             allowlist=cfg.get("sim_allowlist"),
                             task_options=here))

    def cleanup():
        for t in reversed(tools):
            try:
                t.stop()
            except Exception:
                pass

    return tools, cleanup


def parse_result_footer(text: str) -> Optional[Dict[str, Any]]:
    """Extract a stage's machine-readable result footer (schema.json, harness_result).

    Each stage ends its final message with one fenced ```json block. Return the
    last such object as a dict, or None if the agent didn't emit one (older
    skills / a truncated turn) so callers can fall back gracefully.
    """
    if not text:
        return None
    import re
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not blocks:
        # Fallback: the last brace-balanced object in the text.
        start, end = text.rfind("{"), text.rfind("}")
        blocks = [text[start:end + 1]] if 0 <= start < end else []
    for blob in reversed(blocks):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _dispatch(cfg: Dict[str, Any], llm, prompt: str, tools: List, here: Dict) -> Dict[str, Any]:
    """Dispatch one agent turn on this node and return a compact result dict."""
    creds = cfg.get("creds_resource", "copilot_creds")
    cli = get(llm.prompt.options(resources={creds: 1.0}, **here).chia_remote(llm, prompt, tools))
    return {
        "result": getattr(cli, "result", ""),
        "stream": getattr(cli, "stream_result", ""),
        "success": bool(getattr(cli, "success", False)),
        "returncode": getattr(cli, "returncode", -1),
    }


def _agent_stage(cfg: Dict[str, Any], stage_name: str, tier: str, timeout_s: int,
                 needs_sim: bool, extra: Optional[str] = None) -> Dict[str, Any]:
    """Run a generic agent-driven stage: render skill prompt, dispatch, clean up."""
    here = _here()
    # In sandbox mode the agent sees the design at the fixed in-sandbox path, so
    # the prompt must reference those paths, not the host checkout locations.
    if cfg.get("sandbox") == "openshell":
        design_dir = SANDBOX_DESIGN_DIR
        workspace_dir = SANDBOX_DESIGN_DIR + "/mantis_workspace"
    else:
        design_dir = cfg["design_dir"]
        workspace_dir = cfg["workspace_dir"]
    prompt = skill_loader.render_stage_prompt(
        cfg["skills_root"], stage_name, workspace_dir, design_dir, extra
    )
    # Build tools before the LLM: when sandbox="openshell", _build_llm derives
    # the agent's MCP egress policy from these live tool endpoints.
    tools, cleanup = _stage_tools(cfg, stage_name, needs_sim, here)
    try:
        llm = _build_llm(cfg, tier, timeout_s, tools=tools)
        out = _dispatch(cfg, llm, prompt, tools, here)
    finally:
        cleanup()
    out["stage"] = stage_name
    out["footer"] = parse_result_footer(out.get("result", ""))
    return out


# --------------------------------------------------------------------------- #
# Non-fanout agent stages. Each is a discrete node.
# --------------------------------------------------------------------------- #
@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_architecture(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Synthesize the KB from the design + learnings, then clear the inbox."""
    from chia.analysis.mantis.finding_store import FindingStore
    out = _agent_stage(cfg, schema.STAGE_ARCHITECTURE, "frontier",
                       cfg["timeouts"][schema.STAGE_ARCHITECTURE], needs_sim=False)
    # The architecture stage owns clearing the learnings inbox once synthesized.
    FindingStore(cfg["workspace_dir"]).clear_learnings()
    return out


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_threat_model(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Refine THREAT_MODEL.md from the KB."""
    return _agent_stage(cfg, schema.STAGE_THREAT_MODEL, "utility",
                        cfg["timeouts"][schema.STAGE_THREAT_MODEL], needs_sim=False)


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_plan(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Produce plan.json investigations from the KB + boundaries."""
    return _agent_stage(cfg, schema.STAGE_PLAN, "utility",
                        cfg["timeouts"][schema.STAGE_PLAN], needs_sim=False)


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_chain(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Combine validated findings into multi-step bug-chain super-findings."""
    return _agent_stage(cfg, schema.STAGE_CHAIN, "frontier",
                        cfg["timeouts"][schema.STAGE_CHAIN], needs_sim=False)


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_reflect(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Extract trajectory insights into the learnings inbox for the next loop."""
    return _agent_stage(cfg, schema.STAGE_REFLECT, "utility",
                        cfg["timeouts"][schema.STAGE_REFLECT], needs_sim=False)


# --------------------------------------------------------------------------- #
# Fan-out agent stages. Dispatched once per item by the driver.
# --------------------------------------------------------------------------- #
@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_researcher(cfg: Dict[str, Any], investigation: Dict[str, Any]) -> Dict[str, Any]:
    """Audit one investigation's target files; write findings/<uuid>.json."""
    extra = (
        "Execute ONLY this single investigation from the plan (ignore the others; "
        "the harness runs them in parallel):\n" + json.dumps(investigation, indent=2)
    )
    out = _agent_stage(cfg, schema.STAGE_RESEARCHER, "frontier",
                       cfg["timeouts"][schema.STAGE_RESEARCHER], needs_sim=False, extra=extra)
    out["investigation"] = investigation.get("title", "")
    return out


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_review(cfg: Dict[str, Any], finding_id: str) -> Dict[str, Any]:
    """Validate one finding (false-positive filter); update its status."""
    extra = _one_finding_extra(cfg, finding_id,
                               "Apply the reviewer's negative filters and set this "
                               "finding's `status` (VALID/FALSE_POSITIVE), `reasoning`, "
                               "and `repro_hints`.")
    out = _agent_stage(cfg, schema.STAGE_REVIEW, "utility",
                       cfg["timeouts"][schema.STAGE_REVIEW], needs_sim=False, extra=extra)
    out["finding_id"] = finding_id
    _mark_done(cfg, schema.STAGE_REVIEW, finding_id, out)
    return out


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_critic(cfg: Dict[str, Any], finding_id: str) -> Dict[str, Any]:
    """Check one finding's silicon viability; append learnings for non-viable."""
    extra = _one_finding_extra(cfg, finding_id,
                               "Judge whether this bug survives synthesis into real "
                               "silicon and set `production_viability` + "
                               "`critic_reasoning`. Append a learning for any "
                               "false-positive/non-viable path.")
    out = _agent_stage(cfg, schema.STAGE_CRITIC, "frontier",
                       cfg["timeouts"][schema.STAGE_CRITIC], needs_sim=False, extra=extra)
    out["finding_id"] = finding_id
    _mark_done(cfg, schema.STAGE_CRITIC, finding_id, out)
    return out


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_reproduce(cfg: Dict[str, Any], finding_id: str) -> Dict[str, Any]:
    """Write + run a testbench/SVA/formal property for one finding in the sandbox."""
    extra = _one_finding_extra(cfg, finding_id,
                               "Write a testbench / SVA / formal property that triggers "
                               "this bug and RUN it using ONLY the sim tool "
                               "(mantis_sim_*_run_simulation). Set `repro_status`, "
                               "`repro_file_path`, `run_command`, and `repro_output`.")
    out = _agent_stage(cfg, schema.STAGE_REPRODUCE, "frontier",
                       cfg["timeouts"][schema.STAGE_REPRODUCE], needs_sim=True, extra=extra)
    out["finding_id"] = finding_id
    _mark_done(cfg, schema.STAGE_REPRODUCE, finding_id, out)
    return out


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_patch(cfg: Dict[str, Any], finding_id: str) -> Dict[str, Any]:
    """Generate + verify an RTL fix for one finding, then re-verify it."""
    extra = _one_finding_extra(cfg, finding_id,
                               "Produce a minimal RTL fix, verify it blocks the "
                               "reproducer using ONLY the sim tool, then attempt a "
                               "variant re-verification. Set `patch_status`, `patch_diff`, "
                               "and `reverify_status`.")
    out = _agent_stage(cfg, schema.STAGE_PATCH, "frontier",
                       cfg["timeouts"][schema.STAGE_PATCH], needs_sim=True, extra=extra)
    out["finding_id"] = finding_id
    _mark_done(cfg, schema.STAGE_PATCH, finding_id, out)
    return out


def _mark_done(cfg: Dict[str, Any], stage: str, finding_id: str, out: Dict[str, Any]) -> None:
    """Record per-finding stage_status so a resumed run can skip this pair.

    The state follows the agent's footer status when present (skipped/error),
    defaulting to ``done``. Best-effort: a finding the stage deleted is simply
    absent, so a failed mark is ignored.
    """
    from chia.analysis.mantis.finding_store import FindingStore

    footer = out.get("footer") or {}
    status = footer.get("status")
    state = {"skipped": schema.STAGE_STATE_SKIPPED,
             "error": schema.STAGE_STATE_ERROR}.get(status, schema.STAGE_STATE_DONE)
    try:
        FindingStore(cfg["workspace_dir"]).mark_stage(finding_id, stage, state)
    except Exception:
        pass


def _one_finding_extra(cfg: Dict[str, Any], finding_id: str, instruction: str) -> str:
    """Inline one finding's JSON so a per-finding stage has full context."""
    from chia.analysis.mantis.finding_store import FindingStore

    try:
        finding = FindingStore(cfg["workspace_dir"]).read(finding_id)
        blob = json.dumps(finding, indent=2)
    except Exception:
        blob = f"(could not read finding {finding_id})"
    return (f"Operate on exactly ONE finding, id={finding_id}. {instruction}\n\n"
            f"Current finding JSON:\n{blob}")


# --------------------------------------------------------------------------- #
# Hybrid stage: agent decides duplicate groups, harness merges deterministically.
# --------------------------------------------------------------------------- #
@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_dedupe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Ask the agent to group duplicates, then merge them with deterministic code.

    The LLM only returns a mapping ``{primary_id: [dup_id, ...]}`` over the
    compact summary index; :func:`chia.analysis.mantis.merge.merge_findings`
    performs the actual field union so state mutation stays reproducible.
    """
    from chia.analysis.mantis.finding_store import FindingStore

    store = FindingStore(cfg["workspace_dir"])
    summaries = store.summaries()
    if len(summaries) < 2:
        return {"stage": schema.STAGE_DEDUPE, "success": True, "merged_groups": 0,
                "note": "fewer than 2 findings; nothing to dedupe"}

    extra = (
        "Here is the compact finding index. Identify duplicate groups (same "
        "underlying RTL bug). Return ONLY a JSON object mapping each surviving "
        "primary id to a list of duplicate ids to merge into it, e.g. "
        '{"<primary>": ["<dup1>", "<dup2>"], "<other>": []}. Do not merge or edit '
        "files yourself — the harness performs the merge.\n\n"
        + json.dumps(summaries, indent=2)
    )
    out = _agent_stage(cfg, schema.STAGE_DEDUPE, "utility",
                       cfg["timeouts"][schema.STAGE_DEDUPE], needs_sim=False, extra=extra)

    # Prefer the structured `duplicate_groups` from the result footer (schema.json
    # harness_result / dedupe payload); fall back to scraping the reply for older skills.
    footer = out.get("footer") or {}
    dup_map = footer.get("duplicate_groups")
    if not isinstance(dup_map, dict):
        dup_map = _parse_dup_map(out.get("result", ""))
    merged_groups = 0
    for primary_id, dup_ids in dup_map.items():
        dup_ids = [d for d in dup_ids if d and d != primary_id]
        if not dup_ids:
            continue
        try:
            primary = store.read(primary_id)
            dups = [store.read(d) for d in dup_ids]
        except (OSError, ValueError):
            continue
        store.write(merge.merge_findings(primary, dups))
        for d in dup_ids:
            store.delete(d)
        merged_groups += 1

    out["merged_groups"] = merged_groups
    return out


def _parse_dup_map(text: str) -> Dict[str, List[str]]:
    """Extract the ``{primary: [dups]}`` object from the agent's reply."""
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out: Dict[str, List[str]] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                out[str(k)] = [str(x) for x in v]
    return out


# --------------------------------------------------------------------------- #
# Fully deterministic stage: calibration. No agent — a pure-Python node.
# --------------------------------------------------------------------------- #
@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def run_calibrate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministically score every finding (impact/likelihood/risk/priority)."""
    from chia.analysis.mantis.finding_store import FindingStore

    store = FindingStore(cfg["workspace_dir"])
    scored = 0
    for fid in store.list_ids():
        try:
            finding = store.read(fid)
        except (OSError, ValueError):
            continue
        fields = merge.calibrate(finding, availability_tier=finding.get("availability_tier"))
        store.set_fields(fid, **fields)
        store.append_history(fid, schema.STAGE_CALIBRATE, "scored",
                             f"risk={fields['mantis_risk_score']} priority={fields['priority']}")
        scored += 1
    return {"stage": schema.STAGE_CALIBRATE, "success": True, "scored": scored}


# --------------------------------------------------------------------------- #
# Worker-side utility nodes. The design checkout + workspace live on the design
# worker's filesystem, so the head driver reaches them through these nodes: to
# prepare the checkout, to learn the fan-out items (investigations / finding ids)
# a stage should expand into, to snapshot results back for reporting, and to
# archive between iterations. All deterministic (no agent).
# --------------------------------------------------------------------------- #
@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def prepare_design(cfg: Dict[str, Any], repo_url: str, commit: str) -> Dict[str, Any]:
    """Clone (or update) the design repo and ensure the workspace dirs exist.

    Idempotent: a re-run fetches + hard-resets the existing checkout to ``commit``
    rather than failing on a populated directory.
    """
    import os
    import subprocess

    from chia.analysis.mantis.finding_store import FindingStore

    design_dir = cfg["design_dir"]
    os.makedirs(os.path.dirname(design_dir) or "/", exist_ok=True)
    if os.path.isdir(os.path.join(design_dir, ".git")):
        cmds = [["git", "-C", design_dir, "fetch", "--all", "--tags", "--prune"],
                ["git", "-C", design_dir, "checkout", commit],
                ["git", "-C", design_dir, "reset", "--hard", commit]]
    else:
        cmds = [["git", "clone", repo_url, design_dir],
                ["git", "-C", design_dir, "checkout", commit]]
    log = []
    for cmd in cmds:
        p = subprocess.run(cmd, capture_output=True, text=True)
        log.append(f"$ {' '.join(cmd)}\n{p.stdout}{p.stderr}")
        if p.returncode != 0 and cmd[:2] != ["git", "checkout"]:
            return {"success": False, "log": "\n".join(log)}
    FindingStore(cfg["workspace_dir"]).ensure_dirs()
    return {"success": True, "log": "\n".join(log)}


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def list_investigations(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return plan.json's investigations (the researcher fan-out items)."""
    from chia.analysis.mantis.finding_store import FindingStore
    return FindingStore(cfg["workspace_dir"]).investigations()


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def list_finding_ids(cfg: Dict[str, Any]) -> List[str]:
    """Return current finding ids (the per-finding fan-out items)."""
    from chia.analysis.mantis.finding_store import FindingStore
    return FindingStore(cfg["workspace_dir"]).list_ids()


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def list_finding_ids_for_stage(cfg: Dict[str, Any], stage: str) -> List[str]:
    """Finding ids not yet marked ``done`` for ``stage`` (idempotent fan-out)."""
    from chia.analysis.mantis.finding_store import FindingStore
    return FindingStore(cfg["workspace_dir"]).ids_needing_stage(stage)


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def snapshot_workspace(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Pull findings + plan + learnings back to the head for reporting."""
    from chia.analysis.mantis.finding_store import FindingStore
    store = FindingStore(cfg["workspace_dir"])
    return {
        "findings": store.read_all(),
        "plan": store.read_plan(),
        "learnings": store.read_learnings(),
    }


@ChiaFunction(resources={DESIGN_RESOURCE: 1})
def archive_iteration(cfg: Dict[str, Any], iteration: int) -> Dict[str, Any]:
    """Snapshot findings into archive/iter_<n>/ and clear the workspace."""
    from chia.analysis.mantis.finding_store import FindingStore
    dest = FindingStore(cfg["workspace_dir"]).archive_iteration(iteration)
    return {"archived_to": dest}


# Registry the driver uses to resolve a stage name to its node + call shape.
# Fan-out stages take a second positional arg (item); others take just cfg.
STAGE_NODES = {
    schema.STAGE_ARCHITECTURE: run_architecture,
    schema.STAGE_THREAT_MODEL: run_threat_model,
    schema.STAGE_PLAN: run_plan,
    schema.STAGE_RESEARCHER: run_researcher,
    schema.STAGE_DEDUPE: run_dedupe,
    schema.STAGE_REVIEW: run_review,
    schema.STAGE_CRITIC: run_critic,
    schema.STAGE_REPRODUCE: run_reproduce,
    schema.STAGE_CHAIN: run_chain,
    schema.STAGE_PATCH: run_patch,
    schema.STAGE_CALIBRATE: run_calibrate,
    schema.STAGE_REFLECT: run_reflect,
}
