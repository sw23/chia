"""RTL power-estimation node for the hammer-pwr flow.

Mirrors ``examples/sky130_vlsi/hammer_syn_node.py``: a :class:`ChiaFunction`
that lands on the ``vlsi`` worker (holding the ``joules`` license resource) and
runs the hammer-vlsi ``power`` action *in-process* via
:meth:`chia.vlsi.hammer.HammerNode.run` — so the run uses this worker's own
resource slot instead of HammerNode's separate ``hammer`` placement group.

Inputs are shipped by value into the call and materialized on the worker:
  * the checked-in hammer configs (tech-sky130 / tools / tools-priv) as text,
  * the elaborated RTL (``BuildArtifact.generated_src_files``) as
    ``(filename, contents)`` pairs, written under ``obj_dir/input_src/``,
  * the switching-activity VCDs, streamed here from the Verilator workers via
    ``collect_waveform`` (each sim keeps its VCD on-worker and returns a claim
    ticket in its ``RunResult``; too large to return inline).

A generated ``power-inputs.yml`` override wires those into ``power.inputs.*``.
"""

import logging
import os
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction
from chia.chipyard.verilator_run_node import collect_waveform
from chia.vlsi.hammer import HammerNode, HammerResult

logger = logging.getLogger(__name__)

# Report files fetched from obj_dir after the power action. Globs are relative
# to obj_dir (** is recursive); large artifacts (the VCD, Joules databases) are
# excluded by the per-file size cap rather than by name. Paths match what the
# hammer Joules plugin actually writes: reports under power-rundir/reports/,
# the session log/cmd under power-rundir/joules_work/, and the generated
# joules-<timestamp>.tcl at power-rundir/ top level.
_REPORT_PATTERNS = [
    "power-rundir/reports/**",
    "power-rundir/**/*.rpt",
    "power-rundir/joules_work/*.log",
    "power-rundir/joules_work/*.cmd",
    "power-rundir/*.tcl",
    "power-output.json",
]
_REPORT_MAX_BYTES = 2 * 1024 * 1024  # per-file cap; oversized files are listed, not shipped


# Corner metadata matching the Sky130 CACTI characterization suffixes
_SKY130_CORNER_INFO = {
    "ff_n40C_1v95": {"nmos": "fast", "pmos": "fast", "temperature": "-40 C",
                     "VDD": "1.95 V"},
    "ss_100C_1v60": {"nmos": "slow", "pmos": "slow", "temperature": "100 C",
                     "VDD": "1.60 V"},
    "tt_025C_1v80": {"nmos": "typical", "pmos": "typical", "temperature": "25 C",
                     "VDD": "1.80 V"},
}


def _generate_cacti_libs_yaml(obj_dir: str, cacti_sram_libs: list) -> str | None:
    """Write CACTI SRAM Liberty/LEF collateral + a hammer override registering it.

    Mirrors examples/sky130_vlsi's Sky130SynNode._generate_cacti_libs_yaml:
    each SRAM contributes one ``vlsi.technology.extra_libraries`` entry per
    characterized corner (ff/ss/tt), so hammer's MMMC corners pick the macros
    up — Joules' ``read_libs`` consumes them the same way Genus does.
    """
    if not cacti_sram_libs:
        return None

    lib_dir = os.path.join(obj_dir, "cacti_libs")
    os.makedirs(lib_dir, exist_ok=True)

    lines = [
        'vlsi.technology.extra_libraries_meta: ["append", "lazydeepsubst"]',
        "vlsi.technology.extra_libraries:",
    ]
    for lp in cacti_sram_libs:
        lib_contents = lp.get("lib_contents", {})

        # LEF written once per SRAM (geometry is not corner-dependent)
        lef_path = None
        if lp.get("lef_content"):
            lef_path = os.path.join(lib_dir, f"cacti_{lp['name']}.lef")
            with open(lef_path, "w") as f:
                f.write(lp["lef_content"])

        for corner_suffix, lib_content in lib_contents.items():
            info = _SKY130_CORNER_INFO.get(corner_suffix,
                                           _SKY130_CORNER_INFO["tt_025C_1v80"])
            lib_path = os.path.join(lib_dir, f"{lp['name']}_{corner_suffix}.lib")
            with open(lib_path, "w") as f:
                f.write(lib_content)
            lines.append("  - library:")
            lines.append(f'      nldm_liberty_file: "{lib_path}"')
            if lef_path:
                lines.append(f'      lef_file: "{lef_path}"')
            lines.append("      corner:")
            lines.append(f'        nmos: "{info["nmos"]}"')
            lines.append(f'        pmos: "{info["pmos"]}"')
            lines.append(f'        temperature: "{info["temperature"]}"')
            lines.append("      supplies:")
            lines.append(f'        VDD: "{info["VDD"]}"')
            lines.append('        GND: "0 V"')

    yaml_path = os.path.join(obj_dir, "cacti_sram_libs.yml")
    with open(yaml_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return yaml_path


@dataclass
class PowerResult:
    """One benchmark's Joules power run: hammer outcome + fetched reports."""
    test_name: str
    success: bool
    returncode: int
    obj_dir: str                # on the vlsi worker
    stdout: str
    stderr: str
    reports: dict[str, str] = field(default_factory=dict)   # relpath -> text
    skipped_reports: dict[str, int] = field(default_factory=dict)  # relpath -> bytes (over cap)


def _check_vcd(vcd_path: str, dut_instance: str = "",
               max_bytes: int = 262144, max_scopes: int = 15) -> None:
    """Print the head of the VCD's ``$scope`` tree and sanity-check it.

    Args:
        dut_instance: slash-separated scope path (e.g.
            ``TOP/TestDriver/testHarness/chiptop0``); when given, raise if it
            is not a scope path in the VCD header.
    """
    try:
        with open(vcd_path, "r", errors="replace") as f:
            head = f.read(max_bytes)
        lines, stack, paths = [], [], set()
        for chunk in head.split("$end"):
            chunk = chunk.strip()
            if chunk.startswith("$scope"):
                name = chunk.split()[-1]
                if len(lines) < max_scopes:
                    lines.append("  " * len(stack) + name)
                stack.append(name)
                paths.add("/".join(stack))
            elif chunk.startswith("$upscope"):
                if stack:
                    stack.pop()
            elif chunk.startswith("$enddefinitions"):
                break
        print(f"VCD scope tree head ({os.path.basename(vcd_path)}):\n"
              + "\n".join(lines), flush=True)

        if dut_instance and dut_instance not in paths:
            near = sorted(p for p in paths if p.count("/") <= dut_instance.count("/"))
            raise RuntimeError(
                f"dut_instance '{dut_instance}' is not a scope path in "
                f"{os.path.basename(vcd_path)} — Joules would fail.    "
                f"{near[:10]}")

        # Value-change sanity: timestamps ("#<n>" lines) must appear in the
        # tail. A header-only VCD means the sim dumped nothing (chia's chipyard
        # gates dumping behind +wf_dump_all / PC windows).
        size = os.path.getsize(vcd_path)
        with open(vcd_path, "rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode(errors="replace")
        if not any(l.startswith("#") for l in tail.splitlines()):
            raise RuntimeError(
                f"{os.path.basename(vcd_path)} ({size} bytes) has NO value "
                f"changes — run the sim with dump_all_waveform=True (or wave "
                f"windows), else Joules fails after synthesis.")
    except OSError as e:
        print(f"WARNING: could not scan VCD {vcd_path}: {e}", flush=True)


@ChiaFunction(resources={"VLSI": 1, "joules": 1})
def run_joules_power(
    config_texts: dict,                    # {filename: yaml_text}: tech / tools / tools-priv
    input_files: list,                     # [(filename, verilog_text)]: elaborated RTL
    top_module: str,                       # design's top RTL module
    tb_name: str,                          # testbench top in the VCD
    tb_dut: str,                           # hierarchical path to the DUT inside the VCD
    waveforms: list,                       # [(test_name, RunResult)]: ALL benchmark VCD tickets
    obj_dir: str,                          # hammer obj_dir on this worker
    cacti_sram_libs: list | None = None,   # CharacterizedSRAM.to_lib_dict() entries
    clock_port: str = "",                  # if set, a create_clock SDC -> power.inputs.sdc
    clock_period_ns: float = 0.0,          # clock period (ns) for that SDC
    timeout_seconds: int = 86400,
) -> PowerResult:
    """Stage inputs and run ONE hammer ``power`` action over all waveforms.

    ``power.inputs.waveforms`` is a list: the Joules plugin elaborates and
    internally synthesizes the design ONCE, then does a
    ``read_stimulus``/``compute_power`` pass per waveform, naming each
    waveform's reports after the VCD's basename (== the benchmark name). One
    batched run therefore replaces N per-benchmark runs whose dominant cost
    (RTL->gate mapping of the whole SoC) was identical every time.

    Trade-off: one corrupt VCD aborts the TCL at its ``read_stimulus``, taking
    the remaining stimuli with it — acceptable against a ~Nx runtime saving.
    """
    import yaml

    obj_dir = os.path.abspath(obj_dir)
    os.makedirs(obj_dir, exist_ok=True)

    # 1. Materialize the shipped hammer configs on this worker.
    cfg_dir = os.path.join(obj_dir, "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    config_paths = []
    for name, text in config_texts.items():
        path = os.path.join(cfg_dir, name)
        with open(path, "w") as f:
            f.write(text)
        config_paths.append(path)

    # 1b. Register CACTI-characterized SRAM macros (Liberty + LEF) so Joules
    #     models the caches as macros instead of register arrays. The RTL in
    #     input_files must be the MacroCompiler-remapped set (cacti_* stubs).
    cacti_yml = _generate_cacti_libs_yaml(obj_dir, cacti_sram_libs or [])
    if cacti_yml:
        config_paths.append(cacti_yml)
        print(f"CACTI SRAM libs: {len(cacti_sram_libs)} macros -> {cacti_yml}",
              flush=True)

    # 2. Write the elaborated RTL and collect every activity VCD from its
    #    verilator worker (chunk-streamed, then the worker-side copy is
    #    removed — frees their disk before the hours-long Joules run). The
    #    VCD filename (minus extension) becomes the report stem, so name
    #    each one after its benchmark.
    src_dir = os.path.join(obj_dir, "input_src")
    os.makedirs(src_dir, exist_ok=True)
    rtl_paths = []
    for filename, contents in input_files:
        if not filename.endswith((".v", ".sv")):
            continue
        src_path = os.path.join(src_dir, filename)
        with open(src_path, "w") as f:
            f.write(contents)
        rtl_paths.append(src_path)

    vcd_paths = []
    try:
        for test_name, run_res in waveforms:
            dest = os.path.join(obj_dir, f"{test_name}.vcd")
            n = collect_waveform(dest, run_res, remove_source=True)
            print(f"collected {test_name}.vcd ({n} bytes)", flush=True)
            vcd_paths.append(dest)
        if vcd_paths:
            # Fail fast (pre-synthesis) if the DUT path or signal activity is
            # missing
            _check_vcd(vcd_paths[0], f"{tb_name}/{tb_dut.replace('.', '/')}")

        # 3. Generate the power.inputs override (RTL-level Joules run).
        #    The Joules plugin reads clocks from power.inputs.sdc (not
        #    vlsi.inputs.clocks), so emit a create_clock SDC when a port is given.
        power_inputs = {
            "power.inputs.level": "rtl",
            "power.inputs.top_module": top_module,
            "power.inputs.input_files": rtl_paths,
            "power.inputs.waveforms": vcd_paths,
            "power.inputs.tb_name": tb_name,
            "power.inputs.tb_dut": tb_dut,
        }
        if clock_port:
            sdc_path = os.path.join(obj_dir, "power.sdc")
            with open(sdc_path, "w") as f:
                f.write(f"create_clock -name {clock_port} -period {clock_period_ns} "
                        f"[get_ports {clock_port}]\n")
            power_inputs["power.inputs.sdc"] = sdc_path

        override_yml = os.path.join(obj_dir, "power-inputs.yml")
        with open(override_yml, "w") as f:
            yaml.safe_dump(power_inputs, f, default_flow_style=False)
        config_paths.append(override_yml)

        # 4. Run the power action in-process. We already hold the joules/VLSI
        #    slot, so calling the HammerNode.run ChiaFunction directly (not
        #    .chia_remote) executes here without re-dispatching or requesting
        #    its "hammer" PG.
        batch = f"batch of {len(vcd_paths)} waveforms"
        result = HammerNode.run(
            "power",
            configs=config_paths,
            obj_dir=obj_dir,
            timeout_seconds=timeout_seconds,
        )
        # 5. Fetch the power reports while we're still on the worker that owns
        #    obj_dir. HammerNode.collect called statically runs in-process (same
        #    reasoning as HammerNode.run above); its glob/size-cap semantics ship
        #    the .rpt/log text back by value and list oversized files as skipped.
        collected = HammerNode.collect(
            obj_dir, _REPORT_PATTERNS, max_bytes_per_file=_REPORT_MAX_BYTES)

        # Success determination. Neither obvious signal works here:
        #   * "vlsi.builtins.is_complete" in the -o output is a hammer
        #     CONVENTION — plugins always export it False ("this dict is not a
        #     complete input config"); driver.py errors if a plugin exports
        #     True. It is NOT a run status.
        #   * joules exits 0 even when its sourced TCL aborts mid-script.
        # The honest signals: the TCL-abort marker in joules.log, and whether
        # per-waveform power reports actually materialized.
        power_reports = [p for p in collected.files if p.endswith(".power.rpt")]
        tcl_aborted = any("Encountered problems processing file" in text
                          for name, text in collected.files.items()
                          if name.endswith("joules.log"))
        success = result.success and bool(power_reports) and not tcl_aborted
        if not success:
            logger.error(f"power on {batch} failed (rc={result.returncode}, "
                         f"tcl_aborted={tcl_aborted}, "
                         f"power_reports={len(power_reports)}); stderr tail: "
                         f"{result.stderr[-500:] if result.stderr else '(empty)'}")
        print(f"power on {batch}: {'OK' if success else 'FAILED'} — "
              f"{len(power_reports)} power reports, "
              f"{len(collected.files)} files collected "
              f"({len(collected.skipped)} over size cap)", flush=True)

        return PowerResult(
            test_name=batch,
            success=success,
            returncode=result.returncode,
            obj_dir=obj_dir,
            stdout=result.stdout,
            stderr=result.stderr,
            reports=collected.files,
            skipped_reports=collected.skipped,
        )
    finally:
        # VCDs can be huge and their S3 copies are torn down with the loop's
        # temp bucket; don't let local copies pile up on this worker.
        for path in vcd_paths:
            try:
                os.remove(path)
            except OSError:
                pass
