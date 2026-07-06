# hammer-pwr — per-benchmark RTL power estimation with Cadence Joules (CHIA example)

A CHIA flow that measures **per-benchmark power** for a Chipyard SoC: build a
waveform-capable Verilator simulator, run RISC-V ISA tests against it
(capturing a VCD each), characterize the design's SRAMs with CACTI, and feed
the RTL + SRAM macros + all waveforms into **one batched Cadence Joules run**
that produces a flat and a per-hierarchy power report for every benchmark.

Self-contained: depends only on the installed `chia` package.

## The flow

`powerloop.py` runs this pipeline:

1. **Chisel build** (`chisel_build` node) — elaborate + compile the
   `CHIPYARDCONF` design as a `VERILATOR_DEBUG` (VCD-capable) simulator, and
   collect the generated Verilog + `.top.mems.conf`
   (`collect_generated_src=True`).
2. **Load benchmarks** — every `rv64ui-p-*` ELF from the riscv-tests
   checkout under `examples/benchmarks/`, shipped to the workers by value.
3. **Verilator sims** (`verilator_run` nodes, fanned out) — one run per
   benchmark with `capture_waveform + dump_all_waveform + keep_waveform`.
   Each VCD stays on its worker's disk; the `RunResult` carries a claim
   ticket (`vcd_path` + `vcd_node_id`).
4. **CACTI SRAM characterization** (`cacti` node, concurrent with step 3) —
   parse `.top.mems.conf`, run CACTI per large SRAM, and generate per-corner
   Liberty + LEF (`chia.vlsi.sram_cacti`).
5. **MacroCompiler remap** (`chipyard` node) — rewrite the synflop
   `.top.mems.v` so the SRAM wrappers instantiate `cacti_*` macros, and swap
   it plus blackbox stubs into the RTL (same pipeline as the `timing_opt`
   example). Without this, Joules models the caches as register arrays and
   the `memory` power category reads zero.
6. **Batched Joules power** (`vlsi` node) — `run_joules_power` first
   **collects every VCD** straight from the verilator workers
   (`collect_waveform`: chunk-streamed through the Ray object store, pinned
   to the holding node — no S3 bucket or shared filesystem required, and the
   worker-side copy is deleted as soon as it lands). It then runs a single
   hammer `power` action: Joules elaborates and internally synthesizes the
   design **once**, then does a `read_stimulus`/`compute_power` pass per
   waveform. Reports are named after each VCD (== benchmark).
7. **Reports** — everything Joules produced is copied back by value and saved
   under `power-reports/` next to this file.

The Joules run holds the cluster's single `joules: 1` license token, so power
work is serialized regardless of how wide the sim fan-out is.

## Files

| file | purpose |
|---|---|
| `powerloop.py` | the driver: build → sims → CACTI/MacroCompiler → Joules → reports |
| `hammer_power_node.py` | `run_joules_power` (`{"VLSI": 1, "joules": 1}`): stages configs/RTL/VCDs on the vlsi worker and runs hammer's `power` action in-process |
| `constants.py` | all knobs: design config, paths, testbench hierarchy, clock |
| `cluster.yaml` | reference cluster topology (four node types, below) |
| `env.yml` | conda env for the head (`pwr_loop`) |
| `hammer-ymls/tools.yml` | tool selection: Genus for synthesis, **Joules** for power, versions |
| `hammer-ymls/tools-fill.yml` | **fill this in**: your Cadence install root, license server, and `joules` binary path |
| `hammer-ymls/tech-sky130.yml` | sky130 technology config — **set `technology.sky130.basepath`** to your collateral checkout |
| `hammer-ymls/design.yml` | clocks / power-spec / placement inputs for the design |

## Setup

**1. Head conda env** — run from this directory:
```bash
conda env create -f env.yml
conda activate pwr_loop
```
Only the head needs the environment; workers get chia via Ray `py_modules`
and the `cluster.yaml` images. If you rename the env, update the
`conda activate pwr_loop` lines in `cluster.yaml` to match.

**2. Benchmarks** — fetch the submodule and build the riscv-tests ISA suite
(instructions in the benchmarks directory):
```bash
git submodule update --init examples/benchmarks
```
The loop globs `examples/benchmarks/riscv-tests/isa/rv64ui-p-*` (the rv64
base-integer, bare-metal tests — 52 ELFs); edit the glob in `powerloop.py` to
run a different subset.

**3. Cluster** — in `cluster.yaml`, the host lists are `${ENV_VAR}`-expanded.
Export the worker IPs/hostnames before `chia up` (an
`env.sh` you `source` can be a convenient place for them):
```bash
export HEAD_IP=...          # host running `chia up` / the Ray head (+ cacti container)
export BUILD_IP=...         # chisel_build (heavy — Chipyard docker image)
export VERILATOR_IP_1=...   # verilator_run
export VERILATOR_IP_2=...   # verilator_run
export VLSI_IP=...          # the Joules logical worker (see step 4)
```

| Node type | Workers | Role |
|---|---|---|
| `chisel_build` | 1 | elaborates + builds the debug simulator (docker) |
| `verilator_run` | 2 | runs the ISA tests, captures VCDs (docker) |
| `cacti` | 1 | CACTI SRAM characterization (docker, co-located on the head) |
| `vlsi` | 1 | Cadence Joules power runs (**logical worker**, no docker) |

Every listed host must be reachable via the SSH credentials in the top-level
`auth` section, have Docker (for the docker node types), and run an SSH agent.

**4. The `vlsi` logical worker** — Joules is a licensed commercial tool, so
this node runs bare (no docker) as a logical worker: a host with your
Cadence installs, a conda env containing `chia` and
[hammer](https://github.com/ucb-bar/hammer) (with its Joules plugin), and
license-server reachability. See the "Setting up logical workers" user guide
in the chia docs. **We do not want to publish details of setting up the
commercial tool publicly, but if you have Joules licenses, feel free to reach
out to us for help.** Update the node's `worker_env_commands` in
`cluster.yaml` to activate your env.

**5. Fill in the hammer configs** (`hammer-ymls/`):
- `tools-fill.yml` — your `cadence.cadence_home`, `cadence.CDS_LIC_FILE`
  (e.g. `port@your-license-server`), and `power.joules.joules_bin`.
- `tech-sky130.yml` — set `technology.sky130.basepath` to your sky130
  collateral checkout **on the vlsi worker**. Collateral can be acquired via
  [open-pdks](https://github.com/fossi-foundation/open-pdks).
- `design.yml` — clock names/periods must match your design's top-level ports
  (see step 6).

**6. Constants** (`constants.py`):
- `CHIPYARDCONF` — the Chipyard config to build (default `TinyRocketConfig`).
- `HAMMER_WORKDIR` — a scratch directory **on the vlsi worker** where each
  run's hammer obj_dir is created. Must NOT be inside a tool/source checkout.
- `TOP_MODULE` / `TB_NAME` / `TB_DUT` — how Joules maps the VCD onto the
  design: `read_stimulus -dut_instance {TB_NAME}/{TB_DUT}` (dots become
  `/`). The defaults match Chipyard's Verilator harness, whose VCD scope tree
  is `TOP → TestDriver → testHarness → chiptop0`. The power node prints the
  first VCD's actual scope tree and **fails fast** (before the multi-hour
  Joules run) if the configured path isn't in it — use that printout to fix
  these when adapting to another harness.
- `CLOCK_PORT` / `CLOCK_PERIOD_NS` — emitted as a `create_clock` SDC for
  Joules (the Joules plugin reads clocks from `power.inputs.sdc`, not
  `vlsi.inputs.clocks`). The port must exist on `TOP_MODULE`; for
  TinyRocketConfig's ChipTop that is `clock_uncore`.

**7. Bring up the cluster** — from the repo root with the env active:
```bash
chia up examples/hammer-pwr/cluster.yaml
```
The first bring-up pulls large docker images and can take a while.

**8. Launch** — from this directory:
```bash
chia job submit --working-dir . -- python powerloop.py
```
The job ships this directory as the Ray `working_dir`; the driver adds your
local `chia` checkout as `py_modules`, so chia changes reach the workers
without rebuilding images. Note the hammer configs are read from the
**checkout** (not the shipped working_dir), so packaging exclusions never
hide your filled-in files from the run.

## Outputs

Reports land in `power-reports/` (gitignored):

    power-reports/
    ├── power-output.json                      # hammer's -o output config
    └── power-rundir/
        ├── joules-<timestamp>.tcl             # the generated Joules script
        ├── joules_work/joules.log             # the Joules session log
        └── reports/
            ├── <benchmark>.power.rpt          # per-category power (leakage /
            │                                  #   internal / switching), in mW
            └── <benchmark>.hier.power.rpt     # same, broken down per hierarchy

One `.power.rpt` + `.hier.power.rpt` pair per benchmark. The hammer obj_dir
(Joules databases, staged RTL, collected VCDs — the VCDs are deleted after
the run) stays on the vlsi worker under `HAMMER_WORKDIR/<run-id>/` for
post-mortems; prune old run dirs periodically.

## Troubleshooting

- **Enabling auto super threading errors and the whole run aborts** — your
  Joules binary and license generations may disagree: older releases (e.g.
  JLS211) request super-threading under the old feature names
  (`JLS_ENG100`/`Joules_Power_SP`/`Joules_RTL_Power`), while newer license
  files serve `Joules_XL`/`Joules_MCPU`. Use a Joules release matching your
  license file, or disable that `set_db` in hammer's Joules plugin
  (`hammer/power/joules/__init__.py`). Check what your server offers with
  `lmutil lmstat -c <port>@<server> -a | grep -i joules`.
- **Chisel build fails with `returncode=-1`** — the build hit its
  `timeout_seconds` (the default in `powerloop.py` is sized for a fast
  machine); bump it in the `ChiselBuildNode(...)` call.