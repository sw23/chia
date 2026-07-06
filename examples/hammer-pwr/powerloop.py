

import os
import re
import uuid

import ray

from chia.vlsi.hammer import *
from chia.vlsi.sram_cacti import *
from chia.chipyard.verilator_run_node import *
from chia.chipyard.chisel_build_node import *
from constants import *
from chia.base.ChiaFunction import *
from hammer_power_node import run_joules_power

# CACTI SRAM-macro pipeline (mirrors examples/common's
# run_cacti_macrocompiler_prep, used by the timing_opt example)
from chia.chipyard.macrocompiler import generate_macro_stubs, remap_with_macrocompiler
from chia.vlsi.sram_cacti.cacti_runner import parse_mems_conf
from chia.vlsi.sram_cacti.sram_characterize import (
    assemble_generated_src_with_cacti,
    characterize_top_mems_conf_with_cacti,
    generate_cacti_macrocompiler_lib,
)


class Benchmark(NamedTuple):
    name: str       # test ELF filename (also names the sim's .log/.out)
    content: bytes  # raw ELF bytes, loaded from the local machine


def _resolve_tech_basepath(text: str) -> str:
    """Expand ``${technology.sky130.basepath}`` references in a tech YAML.

    Hammer only substitutes ``${...}`` on keys carrying a ``_meta: lazysubst``
    tag; the sky130 plugin reads keys like ``technology.sky130.sky130A``
    directly (e.g. ``setup_cdl``), so the raw ``${...}`` string leaks through
    and file lookups fail. Pre-expand in Python instead — same approach as
    ``sky130_vlsi.hammer_syn_node._resolve_tech_yaml``. (The ``extra_libraries``
    blocks keep their refs: they carry ``lazydeepsubst`` meta and expanding
    them here is harmless anyway.)
    """
    m = re.search(r'basepath:\s*"([^"]+)"', text)
    if not m:
        return text
    return text.replace("${technology.sky130.basepath}", m.group(1))


def _sweep_waveforms(run_ress) -> None:
    """Best-effort cleanup of kept waveforms on the verilator workers.

    Normally a no-op: run_joules_power removes each source right after
    collecting it. This only matters when the loop dies between the sims and
    that collection. soft=True affinity: if a worker node is gone its files
    are gone with it, so let the task run (and no-op) anywhere rather than
    hang waiting for a dead node.
    """
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
    from chia.chipyard.verilator_run_node import remove_waveform

    futures = []
    for _, rr in run_ress:
        if rr.vcd_path and rr.vcd_node_id:
            pin = NodeAffinitySchedulingStrategy(node_id=rr.vcd_node_id, soft=True)
            futures.append(remove_waveform.options(
                scheduling_strategy=pin).chia_remote(rr.vcd_path))
    for f in futures:
        try:
            get(f)
        except Exception:
            pass


def main():

    # Connect to the running cluster, shipping this example dir (working_dir)
    # and the head's chia checkout (py_modules) to every worker — see
    # constants.RUNTIME_ENV.
    #
    # Under `chia job submit --working-dir .` the job ALREADY ships the
    # working_dir and injects its runtime env (signalled by
    # RAY_JOB_CONFIG_JSON_ENV_VAR); ray.init may only ADD non-conflicting
    # fields — re-specifying working_dir raises a merge ValueError. So in job
    # mode drop working_dir and contribute just py_modules/excludes.
    runtime_env = RUNTIME_ENV
    if "RAY_JOB_CONFIG_JSON_ENV_VAR" in os.environ:
        runtime_env = {k: v for k, v in RUNTIME_ENV.items() if k != "working_dir"}
    ray.init(address="auto", runtime_env=runtime_env)

    print(f"Building config {CHIPYARDCONF} in Chipyard")

    bilder = ChiselBuildNode(
        CHIPYARDPATH,
        CHIPYARDCONF,
        timeout_seconds=1200,
        target=BuildTarget.VERILATOR_DEBUG,   # VCD-capable sim, required for waveforms
        collect_generated_src=True)           # elaborated .v/.sv -> Joules power.inputs.input_files
    bildArtFuture = bilder.build.chia_remote(bilder)

    print("Build dispatched, collecting bmarks")

    # Load bmarks: every rv64ui-p-* ELF from the local riscv-tests checkout,
    # read into memory (excluding the .dump disassembly files) to ship to the
    # run node.
    bmarks = [
        Benchmark(name=p.name, content=p.read_bytes())
        for p in sorted(ISA_DIR.glob("rv64ui-p-*"))
        if p.is_file() and not p.name.endswith(".dump")
    ]
    if not bmarks:
        raise RuntimeError(f"no rv64ui-p-* test ELFs found under {ISA_DIR}")

    print("Collected bmarks, waiting on build")

    # Fail fast before creating the bucket / fanning out sims: both the runs
    # and the power step are useless without a good build and its RTL.
    build_art = get(bildArtFuture)
    if not build_art.success:
        raise RuntimeError(f"Chisel build failed (rc={build_art.returncode}); "
                           f"stderr tail:\n{build_art.stderr[-2000:]}")
    if not build_art.generated_src_files:
        raise RuntimeError(
            "build returned no generated RTL (generated_src_files) for Joules")

    # --- CACTI SRAM characterization
    # .top.mems.conf, run CACTI per large SRAM, and generate Liberty/LEF on
    # the cacti worker. Dispatched here so it runs CONCURRENTLY with the
    # Verilator sims; resolved before the power step. This is what lets
    # Joules model the caches as SRAM macros instead of register arrays.
    cacti_future = characterize_top_mems_conf_with_cacti.chia_remote(
        build_art.generated_src_files, CACTI_PATH)

    print(f"Build finished ({len(bmarks)} bmarks), simulating in verilator "
          f"(CACTI characterization running concurrently)")

    vlator = VerilatorRunNode()
    run_futures = []

    for bmark in bmarks:
        run_futures.append((bmark, vlator.run.chia_remote(
            vlator,
            bildArtFuture,
            bmark.content,
            bmark.name,
            WORKDIR,
            capture_waveform=True,
            dump_all_waveform=True,
            keep_waveform=True,
        )))

    run_ress = [(bmark, get(future)) for bmark, future in run_futures]

    print("Verilator finished, analyzing power")

    # Kept waveforms sit on the verilator workers until the power node
    # collects (and removes) each one; if anything below raises first, sweep
    # them so they don't linger on the workers' disks.
    try:
        # --- Power: ONE batched Joules RTL-power run over every benchmark VCD.
        # Joules elaborates + internally synthesizes the design once, then does
        # a read_stimulus/compute_power pass per waveform; each waveform's
        # reports are named after its VCD basename (== the benchmark name).
        config_texts = {p.name: _resolve_tech_basepath(p.read_text())
                        for p in sorted(HAMMER_YMLS.glob("*.yml"))}

        waveforms = []
        for bmark, run_res in run_ress:
            if not (run_res.success and run_res.vcd_path):
                print(f"  skipping {bmark.name}: no kept VCD "
                      f"(success={run_res.success})")
                continue
            waveforms.append((bmark.name, run_res))
        if not waveforms:
            raise RuntimeError("no benchmark produced a VCD; nothing to analyze")

        # --- CACTI + MacroCompiler (follows examples/common's
        # run_cacti_macrocompiler_prep): resolve the characterization, remap
        # the synflop SRAM wrappers to cacti_* macro instantiations on the
        # chipyard node, and swap the remapped .top.mems.v + blackbox stubs
        # into the RTL Joules will read. The Liberty/LEF libs ride along as
        # cacti_sram_libs. Falls back to synflops if any stage comes up empty.
        power_src = build_art.generated_src_files
        cacti_char = get(cacti_future)
        cacti_libs = cacti_char.sram_libs
        mems_conf = next(
            (c for n, c in power_src if n.endswith(".top.mems.conf")), None)
        if mems_conf and cacti_libs:
            specs = parse_mems_conf(mems_conf)
            mc_lib_json = generate_cacti_macrocompiler_lib(specs)
            remapped_v = get(remap_with_macrocompiler.chia_remote(
                mems_conf, mc_lib_json, CHIPYARDPATH))
            if remapped_v:
                power_src = assemble_generated_src_with_cacti(
                    power_src, remapped_v, generate_macro_stubs(specs, "cacti_"))
                print(f"MacroCompiler remap OK — {len(power_src)} files, "
                      f"{len(cacti_libs)} CACTI SRAM libs")
            else:
                cacti_libs = []
                print("MacroCompiler remap returned None — proceeding with synflops")
        else:
            cacti_libs = []
            print(f"Skipping CACTI macros: mems_conf={bool(mems_conf)}, "
                  f"characterized={len(cacti_char.sram_libs)}")

        power_res = get(run_joules_power.chia_remote(
            config_texts,
            power_src,
            TOP_MODULE,
            TB_NAME,
            TB_DUT,
            waveforms,
            f"{HAMMER_WORKDIR}/{uuid.uuid4().hex[:8]}",  # fresh obj_dir per loop run
            cacti_sram_libs=cacti_libs,
            clock_port=CLOCK_PORT,
            clock_period_ns=CLOCK_PERIOD_NS,
        ))
    finally:
        # No-op in the normal path (run_joules_power removes each source
        # right after collecting it); only cleans up after failures.
        _sweep_waveforms(run_ress)

    # --- Save the Joules reports locally and print a summary ---
    for relpath, text in power_res.reports.items():
        dest = REPORTS_DIR / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    for relpath, size in power_res.skipped_reports.items():
        print(f"  report {relpath} over size cap ({size} bytes), "
              f"left on worker at {power_res.obj_dir}")
    print(f"Power ({power_res.test_name}): "
          f"{'OK' if power_res.success else 'FAILED'} — "
          f"{len(power_res.reports)} reports -> {REPORTS_DIR}")
    if not power_res.success:
        print(f"  stderr tail:\n{power_res.stderr[-2000:]}")

    return

if __name__ == "__main__":
    main()