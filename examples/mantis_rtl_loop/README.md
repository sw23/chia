# mantis_rtl_loop — deterministic Mantis RTL design-bug loop (CHIA example)

A CHIA loop that runs the [dv-mantis](https://github.com/sw23/dv-mantis) hardware
RTL design-bug review **skills** as a deterministic agent harness, to hunt design
bugs and hardware-security weaknesses in open-source RTL.

dv-mantis ships the review *skills* (`SKILL.md` per stage) and the inter-stage
state contract (`schema.json`), and explicitly recommends wrapping them in a
**deterministic programmatic pipeline** for reliability, sandboxing, and scale.
This example is that pipeline, built on CHIA: the CHIA runtime is the
supervisor/orchestrator, each Mantis stage is a discrete CHIA node, and the
`workspace/findings/*.json` state store is read/written through deterministic,
harness-owned tools.

## How a Mantis skill becomes a CHIA function

The dv-mantis skills are the CHIA functions. For each stage, the harness:

1. loads the stage's `SKILL.md` body from a dv-mantis checkout
   (`chia.analysis.mantis.skill_loader`) — dv-mantis stays the source of truth;
2. wraps it with the concrete workspace paths + the state contract and dispatches
   it to a coding agent running on the design worker
   (`chia.analysis.mantis.stages`), with the design checkout + workspace reached
   through pinned MCP tools;
3. lets deterministic Python own everything that must be reproducible — merging
   duplicates, computing risk scores, running control flow, rendering reports.

## Data-driven graph, discrete nodes

CHIA prefers each unit of work to carry its own `@ChiaFunction` decorator, but we
want the pipeline itself to be editable as data. The split:

- **The graph is data** — `chia/analysis/mantis/config.py` holds an ordered list
  of `StageSpec` records (stage, model tier, fan-out shape, sim requirement,
  timeout). Reordering, re-tiering, or changing a stage's fan-out is a data edit.
- **The nodes are code** — `chia/analysis/mantis/stages.py` defines one discrete
  `@ChiaFunction` per stage. The driver walks the data and dispatches the matching
  node; fan-out stages are dispatched once per investigation / per finding.

## The pipeline (12 stages)

`architecture → threat_model → plan → researcher* → dedupe → review* → critic* →
reproduce*† → chain → patch*† → calibrate → reflect`

`*` fans out (researcher over `plan.json` investigations; review/critic/reproduce/
patch over findings). `†` runs AI-generated testbenches/formal harnesses, and does
so **only** through the allow-listed `SimTool` sandbox. Two control-flow edges
wrap the backbone: **patch → reproduce** (re-attack findings whose fix was
bypassed) and **reflect → architecture** (each iteration re-reads the learnings
the previous pass wrote). `dedupe` and `calibrate` are deterministic: dedupe has
the LLM only pick duplicate *groups* and merges them in Python; calibrate is pure
scoring with no LLM.

## Layout

| File | Where it runs | Purpose |
|---|---|---|
| `config.py` | head | target design, dv-mantis path, backend, model tiers, timeouts, loop bounds |
| `mantis_loop.py` | head | driver: prepare → walk PIPELINE → fan-out → re-attack → archive → report |
| `report.py` | head | deterministic findings-JSON → ranked Markdown report |
| `cluster.yaml` | — | single `mantis_design` worker (agent + simulators + checkout) |
| `run.sh` | head | `chia job submit` wrapper |
| `chia/analysis/mantis/` | package | schema, finding store, merge/calibrate, skill loader, tools, stage nodes |

The reusable library ships in the `chia` package (like `chia.chipyard.circt`), so
head-side edits reach workers on the next submit with no image rebuild.

## Setup

1. Clone dv-mantis and point the loop at it:
   ```bash
   git clone https://github.com/sw23/dv-mantis
   export DV_MANTIS_SKILLS_DIR=$PWD/dv-mantis   # or edit SKILLS_ROOT in config.py
   ```
2. Authenticate the GitHub Copilot CLI so `~/.copilot` holds valid credentials
   (mounted into the container): `copilot login` (or set `COPILOT_GITHUB_TOKEN`).
   To drive Claude Code instead, set `BACKEND="claude"` / `CREDS_RESOURCE=
   "claude_creds"` in `config.py` and mount `~/.claude` in `cluster.yaml`.
3. Provide a design-worker image (`cluster.yaml`) with: `git`, the `copilot` CLI
   on PATH, Icarus Verilog and/or Verilator, and the chia py_worker env. Give it
   several CPUs (each stage task blocks on a nested agent-turn task on the same
   node). Once the design is cloned, run the container with `--network none` to
   sandbox the AI-generated harnesses (per the dv-mantis safety guidance).

The default target is [picorv32](https://github.com/YosysHQ/picorv32) (a small,
single-file Verilog CPU that simulates cleanly under iverilog/verilator). Retarget
by editing `TARGET_REPO_URL` / `TARGET_REPO_COMMIT` in `config.py`.

## Run

```bash
conda env create -f env.yml          # first time
conda activate mantis_loop
export CHIA_HEAD=$(hostname)

chia up cluster.yaml

./run.sh --dry-run                   # print the resolved pipeline plan, no cluster
./run.sh --max-iters 1               # one pass over the whole pipeline
./run.sh --stages architecture,threat_model,plan,researcher,dedupe,calibrate
./run.sh                             # full multi-iteration loop

chia down cluster.yaml
```

Reports land in `runs/iter_<n>/report.md` (+ `snapshot.json`) and a final
`runs/report.md`. Driver flags: `--max-iters`, `--stages` (subset), `--no-prepare`
(reuse the checkout), `--no-archive`, `--dry-run`.

## Safety

**Use only in an isolated environment.** The reproduce and patch stages execute
autonomously generated testbenches / formal harnesses. This loop confines them to
the allow-listed `SimTool` inside the design container; you should additionally
run that container network-isolated (`--network none`) and never point it at
production silicon or shared lab equipment. All findings are AI-generated and
**must be verified by a hardware engineer** before being acted upon — see the
dv-mantis README's responsible-use notes.

## Notes

- **Single machine.** `cluster.yaml` runs head + one design worker on one host.
  Scale by raising `mantis_design`/`copilot_creds` counts and `max_workers`.
- **Model tiering.** Frontier models for deep stages (research/reproduce/patch/
  critic/chain), a cheaper utility model for the fast sweeps — edit `MODELS` in
  `config.py`.
- **Determinism.** State mutation (merge, calibrate), control flow, and reporting
  are Python, not LLM, so a run is reproducible and can't be corrupted by an agent
  mangling JSON. The non-deterministic part is confined to the agent stages.
