"""Top-level driver for the Mantis RTL design-bug loop (mantis_rtl_loop).

  chia up cluster.yaml
  ./run.sh                       # or: python mantis_loop.py --max-iters 1

Walks the data-driven pipeline (chia.analysis.mantis.config.PIPELINE) over a
design checkout, dispatching one discrete CHIA node per stage. Sequential stages
run on the backbone; fan-out stages (researcher over investigations; review /
critic / reproduce / patch over findings) spread concurrent tasks across the
design workers and barrier before the next stage. Two control-flow edges wrap the
backbone: the patch -> reproduce re-attack loop (re-hunt variants that bypass a
fix) and the reflect -> architecture next-iteration loop (each pass re-reads the
learnings the previous pass wrote). State is the workspace/ directory on the
design worker; the head pulls it back only to render deterministic reports.

No GitHub writes; no host-run of AI-generated harnesses (they execute only inside
the sandboxed SimTool on the design container).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import ray

from chia.base.ChiaFunction import get, chia_wait, TrackedRef
from chia.analysis.mantis import stages
from chia.analysis.mantis.config import (
    PIPELINE, STAGE_BY_NAME, FANOUT_NONE, FANOUT_INVESTIGATIONS, FANOUT_FINDINGS,
    validate_pipeline,
)
from chia.analysis.mantis import schema

import config as flow_cfg
from report import render_markdown

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mantis_loop")


# --------------------------------------------------------------------------- #
# Fan-out: one task per item, collected with chia_wait so a wedged worker is
# detected and its task resubmitted (as in circt_issue_solver).
# --------------------------------------------------------------------------- #
def _fan_out(node, cfg, items, label_of):
    tracked = []
    for i, item in enumerate(items):
        submit = (lambda it=item: node.chia_remote(cfg, it))
        tracked.append(TrackedRef(ref=submit(), submit_fn=submit,
                                  label=label_of(i, item)))
    results, pending = [], tracked
    while pending:
        done, pending = chia_wait(pending, num_returns=1,
                                  pending_timeout=flow_cfg.PENDING_TIMEOUT_S, retry=True)
        for tr in done:
            try:
                results.append(get(tr.ref))
            except Exception:
                logger.exception("fan-out task %s failed", tr.label)
    return results


def _run_stage(cfg, spec):
    """Dispatch one pipeline stage per its fan-out mode; barrier before return."""
    node = stages.STAGE_NODES[spec.name]
    if spec.fanout == FANOUT_NONE:
        logger.info("stage %s (single)", spec.name)
        return [get(node.chia_remote(cfg))]

    if spec.fanout == FANOUT_INVESTIGATIONS:
        items = get(stages.list_investigations.chia_remote(cfg))
        label = lambda i, it: f"{spec.name}:{(it or {}).get('title', i)}"
    else:  # FANOUT_FINDINGS — idempotent: skip findings already done for this stage
        items = get(stages.list_finding_ids_for_stage.chia_remote(cfg, spec.name))
        label = lambda i, it: f"{spec.name}:{it}"

    if not items:
        logger.info("stage %s: no items to fan out over; skipping", spec.name)
        return []
    logger.info("stage %s: fanning out over %d item(s)", spec.name, len(items))
    return _fan_out(node, cfg, items, label)


def _reattack(cfg, rounds):
    """Re-hunt variants for findings whose patch was bypassed, up to `rounds`.

    The patch->reproduce edge: any finding still marked ``bypassed_patch`` gets
    another reproduce+patch pass. Cheap when there's nothing to chase (the set is
    usually empty) and bounded by `rounds`.
    """
    for r in range(rounds):
        snap = get(stages.snapshot_workspace.chia_remote(cfg))
        bypassed = [f["id"] for f in snap["findings"]
                    if f.get("reattack_status") == "bypassed_patch"]
        if not bypassed:
            return
        logger.info("re-attack round %d: %d bypassed finding(s)", r + 1, len(bypassed))
        _fan_out(stages.run_reproduce, cfg, bypassed, lambda i, fid: f"reattack_repro:{fid}")
        _fan_out(stages.run_patch, cfg, bypassed, lambda i, fid: f"reattack_patch:{fid}")


def _persist(iteration, snap, meta, out_dir):
    it_dir = out_dir / f"iter_{iteration}"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "snapshot.json").write_text(json.dumps(snap, indent=2))
    md = render_markdown(snap["findings"], meta)
    (it_dir / "report.md").write_text(md)
    logger.info("iteration %d: %d finding(s) -> %s",
                iteration, len(snap["findings"]), it_dir / "report.md")
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description="Mantis RTL design-bug loop")
    ap.add_argument("--max-iters", type=int, default=flow_cfg.MAX_ITERATIONS)
    ap.add_argument("--stages", type=str, default=None,
                    help="comma-separated subset of stages to run (default: all)")
    ap.add_argument("--no-prepare", action="store_true",
                    help="skip cloning/resetting the design (reuse the checkout)")
    ap.add_argument("--no-archive", action="store_true",
                    help="do not archive+clear findings between iterations")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate config + pipeline and print the plan, then exit")
    args = ap.parse_args()

    validate_pipeline()
    cfg = flow_cfg.build_cfg()

    selected = [s for s in PIPELINE
                if args.stages is None or s.name in set(args.stages.split(","))]

    if args.dry_run:
        print(f"backend={cfg['backend']} models={cfg['models']}")
        print(f"design={cfg['design_dir']} workspace={cfg['workspace_dir']}")
        print(f"skills_root={cfg['skills_root']}")
        print("pipeline:")
        for s in selected:
            print(f"  {s.name:14s} agent={s.uses_agent!s:5s} tier={s.tier:8s} "
                  f"fanout={s.fanout:14s} sim={s.needs_sim}")
        return

    ray.init(address="auto",
             runtime_env={"py_modules": flow_cfg.PY_MODULES,
                          "excludes": flow_cfg.RUNTIME_ENV_EXCLUDES},
             logging_level=logging.WARNING)

    out_dir = Path(flow_cfg.ARTIFACT_DIR)
    meta_base = {"target": flow_cfg.TARGET_REPO_URL, "backend": cfg["backend"]}

    if not args.no_prepare:
        logger.info("preparing design checkout %s@%s",
                    flow_cfg.TARGET_REPO_URL, flow_cfg.TARGET_REPO_COMMIT)
        prep = get(stages.prepare_design.chia_remote(
            cfg, flow_cfg.TARGET_REPO_URL, flow_cfg.TARGET_REPO_COMMIT))
        if not prep.get("success"):
            logger.error("design prepare failed:\n%s", prep.get("log", ""))
            return

    final_md = ""
    for iteration in range(1, args.max_iters + 1):
        logger.info("===== iteration %d/%d =====", iteration, args.max_iters)
        for spec in selected:
            _run_stage(cfg, spec)
            if spec.name == schema.STAGE_PATCH and flow_cfg.MAX_REATTACK_ROUNDS > 0:
                _reattack(cfg, flow_cfg.MAX_REATTACK_ROUNDS)

        snap = get(stages.snapshot_workspace.chia_remote(cfg))
        final_md = _persist(iteration, snap, {**meta_base, "iterations": iteration}, out_dir)

        if not args.no_archive and iteration < args.max_iters:
            arch = get(stages.archive_iteration.chia_remote(cfg, iteration))
            logger.info("archived iteration %d -> %s", iteration, arch.get("archived_to"))

    (out_dir / "report.md").write_text(final_md)
    logger.info("done. final report: %s", out_dir / "report.md")


if __name__ == "__main__":
    main()
