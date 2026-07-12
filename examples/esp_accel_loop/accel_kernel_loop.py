"""Agentic accelerator loop on ESP's Vivado HLS flow: a hardware memcpy.

The agent implements an ESP accelerator tile in HLS C++ that copies its
input region to its output region, validated in hardware — a full-SoC RTL
simulation whose generated bare-metal self-test DMAs data through the tile
and checks output == input elementwise.

The harness is programmatic, built from EspWorkspaceNode members:

    setup   accgen skeleton -> HLS -> tile config -> soft + baremetal
            (one pass with the generated kernel, so socgen and the
            self-test binary exist), then scrub: the HLS work dir and the
            installed RTL are removed and espacc.cc is reduced to an empty
            stub. Nothing the agent didn't write can reach a simulation.
    loop    agent edits espacc.cc via a bash tool running IN the ESP
            workspace container -> harness re-runs HLS -> full-SoC sim
            until validation passes or ESP_LOOP_MAX_ITERS is reached

The agent only gets the kernel file; the golden model is the generated
identity check and the self-test binary is built once, before the agent
runs, so validation can only pass when the synthesized hardware really
copies the buffer.

Usage (host, chia env active, cluster up via esp_loop_cluster.yaml):
    cd <repo root>
    python examples/esp_accel_loop/accel_kernel_loop.py

With ``--fpga``, once the loop validates a kernel in simulation the same
accelerator is synthesized and run on a real board, its bare-metal self-test
checked over the UART — the sim result confirmed on silicon. Off by default,
so the example runs sim-only with no board or synthesis worker. It needs the
FPGA cluster (an ``esp_vivado`` worker and an attached board) and the shared
same-path workspace; see the FPGA notes in ``docs/api/esp`` and the site env.

Environment overrides: ESP_ROOT / ESP_BOARD / ESP_CPU / ESP_ACC_NAME /
ESP_ACC_TILE / ESP_SIM_TIMEOUT as in the chia/esp cluster tests, plus
ESP_TECHLIB, ESP_LOOP_MODEL, ESP_LOOP_MAX_ITERS; and for ``--fpga``,
ESP_FPGA_BOARD / ESP_FPGA_TECHLIB / ESP_SYN_TIMEOUT plus the CHIA_ESP_*
hardware endpoints.
"""

import argparse
import os
import sys
import time
from uuid import uuid4

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *([os.pardir] * 2)))
sys.path.insert(0, _REPO_ROOT)

import ray
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy

from chia.base.ChiaFunction import ChiaFunction, get
from chia.base.tools.BashTool import BashTool
from chia.esp.esp_workspace import EspWorkspaceNode, board_dir, with_acc_tile
from chia.esp.state_def import EspAccelSpec
from chia.models.claude import CLIResult, ClaudeCodeLLM

ESP_ROOT = os.environ.get("ESP_ROOT", "/home/espuser/esp")
ESP_BOARD = os.environ.get("ESP_BOARD", "xilinx-vc707-xc7vx485t")
ESP_CPU = os.environ.get("ESP_CPU", "ariane")
ESP_TECHLIB = os.environ.get("ESP_TECHLIB", "virtex7")
ACC_NAME = os.environ.get("ESP_ACC_NAME", "chiatest")
ACC_ROW, ACC_COL = (int(v) for v in os.environ.get("ESP_ACC_TILE", "1,0").split(","))
# A kernel with broken DMA control hangs the NoC and runs to this timeout,
# so it is the price of one bad iteration — keep it well under the default
# sim timeout of the non-agentic tests.
SIM_TIMEOUT = int(os.environ.get("ESP_SIM_TIMEOUT", str(1800)))
MODEL = os.environ.get("ESP_LOOP_MODEL", "claude-sonnet-4-6")
MAX_ITERS = int(os.environ.get("ESP_LOOP_MAX_ITERS", "5"))

DEFCONFIG = f"socs/defconfig/esp_{ESP_BOARD}_defconfig"

# --- Optional FPGA deployment (--fpga) -------------------------------------
# After the loop validates a kernel in simulation, optionally synthesize the
# accelerator SoC for a real board and run the accelerator's own self-test on
# hardware. Off by default so the example runs sim-only with no board. The
# board here may differ from the (faster) sim board; the C++ kernel is board-
# agnostic, so the winning source is reused and re-synthesized for this tech.
FPGA_BOARD = os.environ.get("ESP_FPGA_BOARD", "xilinx-vcu118-xcvu9p")
FPGA_TECHLIB = os.environ.get("ESP_FPGA_TECHLIB", "virtexup")
FPGA_IMPL = os.environ.get("ESP_FPGA_IMPL", "dma64_w64")
SYN_TIMEOUT = int(os.environ.get("ESP_SYN_TIMEOUT", str(6 * 3600)))
FPGA_PASS_PATTERN = os.environ.get("ESP_FPGA_PASS_PATTERN", r"PASS")
UART_TIMEOUT = int(os.environ.get("ESP_UART_TIMEOUT", "600"))
# Hardware endpoints as seen from the workers (see esp_site_env.example.sh).
FPGA_HOST = os.environ.get("CHIA_ESP_FPGA_HOST", "127.0.0.1")
HW_SERVER_PORT = int(os.environ.get("CHIA_ESP_HW_SERVER_PORT", "3121"))
UART_HOST = os.environ.get("CHIA_ESP_UART_HOST", "127.0.0.1")
UART_PORT = int(os.environ.get("CHIA_ESP_UART_PORT", "4001"))
ESPLINK_IP = os.environ.get("CHIA_ESP_ESPLINK_IP", "127.0.0.1")
VIVADO_SYN_BIN = os.environ.get("CHIA_ESP_VIVADO_SYN_BIN") or None
VIVADO_PROG_BIN = os.environ.get("CHIA_ESP_VIVADO_PROG_BIN") or None

# 100 64-bit words: the size register defaults to 100 (what the self-test
# uses); 64-bit tokens mean one HLS implementation point (dma64_w64) — one
# project to synthesize per iteration.
DATA_SIZE = 100
IMPL = "dma64_w64"
ANSWERS_TAIL = ["", str(DATA_SIZE), "1024",   # size register: default, max
                "",                           # no more registers
                "64"] + [""] * 11             # 64-bit tokens; rest defaults

TASK = (f"implement a hardware memcpy: the accelerator must DMA-read `size` "
        f"64-bit words from its input region and DMA-write them unchanged to "
        f"its output region (the self-test uses size={DATA_SIZE}, the PLM "
        f"skeleton allows up to 1024)")

STUB_KERNEL = """\
#include "../inc/espacc_config.h"
#include "../inc/espacc.h"
#include "hls_stream.h"
#include "hls_math.h"
#include <cstring>

// TODO: implement the accelerator. The top-level function must match the
// declaration in ../inc/espacc.h.
"""

SYSTEM_MESSAGE = (
    "You are implementing an ESP (Embedded Scalable Platforms) accelerator "
    "kernel in C++ for Vivado HLS, working inside the ESP source tree "
    "through a bash tool. Edit ONLY the accelerator's espacc.cc; do not "
    "modify other files, and do not run make targets — after each of your "
    "turns the harness synthesizes your kernel and evaluates it in an RTL "
    "simulation of the full SoC, and you will be shown the result. The "
    "kernel must be synthesizable C++ (no printf, no dynamic allocation, no "
    "standard library containers)."
)


@ChiaFunction(resources={"claude_creds": 0.01})
def write_kernel_llm(prompt_text: str, model: str, tools: list,
                     timeout_seconds: int = 1200) -> CLIResult:
    """Run one kernel-writing turn on a credentialed LLM worker.

    A direct prompt() call does not re-dispatch, so the CLI runs on the
    worker this task landed on; the bash tool it is handed executes in the
    ESP workspace container.
    """
    os.makedirs("/tmp/llm_env", exist_ok=True)
    os.chdir("/tmp/llm_env")
    os.makedirs("/tmp/ray/llm_logs", exist_ok=True)
    llm = ClaudeCodeLLM(
        model=model,
        system_message=SYSTEM_MESSAGE,
        timeout_seconds=timeout_seconds,
        log_dir="/tmp/ray/llm_logs",
        logging_name="esp_kernel_loop",
        # The LLM worker may run as container-root (rootless-podman hosts),
        # where the CLI refuses --dangerously-skip-permissions. MCP tools
        # are allowlisted individually instead, so nothing else is needed.
        dangerously_skip_permissions=False,
    )
    return llm.prompt(prompt_text, tools)


def _build_prompt(acc_dir_abs: str, source: str, feedback: str) -> str:
    return "\n\n".join([
        f"Task: {TASK}.",
        f"The accelerator directory is {acc_dir_abs} (your bash tool starts "
        f"there). Implement hw/src/espacc.cc; its required top-level "
        f"interface is declared in hw/inc/espacc.h, and sibling accelerators "
        f"under {os.path.dirname(acc_dir_abs)}/ show working examples of the "
        f"same structure.",
        f"--- current hw/src/espacc.cc ---\n{source}",
        f"--- last hardware evaluation ---\n{feedback}",
        "Edit the file in place with the bash tool, then reply with a short "
        "summary of what you changed.",
    ])


def _tail(label: str, text: str, n: int = 2500) -> None:
    print(f"--- {label} (tail) ---\n{text[-n:] if text else '(empty)'}\n---")


def _sim_verdict(sim) -> str:
    if not sim.pass_matched:
        return "did-not-complete"
    return "validation-pass" if "... PASS" in sim.stdout else "validation-fail"


def _fpga_deploy(ws, esp_root: str, spec: EspAccelSpec) -> bool:
    """Take the loop's validated kernel to real hardware.

    Re-runs the accelerator ladder for the FPGA board's technology (the C++
    kernel is board-agnostic, so the source the loop settled on is reused),
    synthesizes the SoC on the ``esp_vivado`` worker, programs the board, and
    runs the accelerator's own bare-metal self-test — the same check the
    simulation ran, now on silicon.
    """
    board, tech = FPGA_BOARD, FPGA_TECHLIB
    defconfig = f"socs/defconfig/esp_{board}_defconfig"
    print(f"\n=== FPGA deploy: {board} ({tech}) ===")
    t0 = time.time()

    # Fresh HLS for this board's tech (installs impl points under tech/<tech>),
    # then place the accelerator on the tile and build its software.
    get(ws.remove.chia_remote(esp_root, f"{spec.acc_dir}/hw/hls-work-{tech}"))
    get(ws.remove.chia_remote(esp_root, f"tech/{tech}/acc/{spec.make_name}"))
    hls = get(ws.accel.chia_remote(esp_root, board, spec.make_name, "hls"))
    col = get(ws.collect.chia_remote(esp_root, [defconfig]))
    cfg = get(ws.configure.chia_remote(
        esp_root, board,
        esp_config=with_acc_tile(col.files[defconfig], spec.make_name,
                                 ACC_ROW, ACC_COL, impl=FPGA_IMPL)))
    soft = get(ws.build.chia_remote(esp_root, board, cpu=ESP_CPU))
    bm = get(ws.accel.chia_remote(esp_root, board, spec.make_name, "baremetal"))
    for label, res in (("hls", hls), ("configure", cfg), ("soft", soft),
                       ("baremetal", bm)):
        print(f"{label:9s}: success={res.success}")
        if not res.success:
            _tail(f"{label} stderr", res.stderr)
            return False

    # The accelerator's bare-metal image is what we load into DRAM.
    binc = get(ws.collect.chia_remote(
        board_dir(esp_root, board),
        [f"soft-build/**/{spec.make_name}*.bin"], max_bytes_per_file=1))
    bins = sorted(binc.skipped)
    if not bins:
        print(f"no {spec.make_name}*.bin found under soft-build")
        return False
    dram_image = os.path.join(board_dir(esp_root, board), bins[0])

    print(f"synth (timeout={SYN_TIMEOUT}s) — on the esp_vivado worker ...")
    syn = get(EspWorkspaceNode.synth.chia_remote(
        esp_root, board, vivado_bin=VIVADO_SYN_BIN, timeout_seconds=SYN_TIMEOUT))
    print(f"synth    : success={syn.success} ({time.time() - t0:.0f}s so far)")
    if not syn.success:
        _tail("synth stderr", syn.stderr)
        return False

    prog = get(ws.fpga_program.chia_remote(
        esp_root, board, fpga_host=FPGA_HOST, hw_server_port=HW_SERVER_PORT,
        vivado_bin=VIVADO_PROG_BIN))
    print(f"program  : success={prog.success}")
    if not prog.success:
        _tail("program stderr", prog.stderr)
        return False

    run = get(ws.fpga_run.chia_remote(
        esp_root, board, uart_host=UART_HOST, uart_port=UART_PORT,
        esplink_ip=ESPLINK_IP, dram_image=dram_image,
        pass_pattern=FPGA_PASS_PATTERN, uart_timeout_seconds=UART_TIMEOUT))
    print(f"run      : success={run.success} pass_matched={run.pass_matched} "
          f"({time.time() - t0:.0f}s total)")
    _tail("UART transcript", run.uart, n=3000)
    return bool(run.success)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fpga", action="store_true",
        help="after the loop validates a kernel in simulation, synthesize and "
             "run it on a real FPGA board (needs the esp_vivado worker and a "
             "board; see the site env). Off by default.")
    args = parser.parse_args()

    # FPGA synthesis runs on a host worker that must see the workspace at the
    # same path the container does, so --fpga uses the shared workspace as the
    # ESP root for the whole run (see CHIA_ESP_SHARED_WORKSPACE).
    global ESP_ROOT
    if args.fpga:
        shared = os.environ.get("CHIA_ESP_SHARED_WORKSPACE")
        if not shared:
            print("--fpga requires CHIA_ESP_SHARED_WORKSPACE (the shared "
                  "same-path workspace); see esp_site_env.example.sh")
            return 2
        ESP_ROOT = shared

    print(f"[driver] connecting to ray cluster (working_dir={_REPO_ROOT})")
    ray.init(
        address="auto",
        runtime_env={
            "working_dir": _REPO_ROOT,
            "excludes": [".venv/**", ".git/**", "**/__pycache__/**",
                         "**/*.pyc", "**/.pytest_cache/**"],
        },
    )

    spec = EspAccelSpec(name=ACC_NAME, flow="vivado", answers_tail=ANSWERS_TAIL)
    acc_dir_abs = os.path.join(ESP_ROOT, spec.acc_dir)
    kernel_rel = f"{spec.acc_dir}/hw/src/espacc.cc"
    hls_work_rel = f"{spec.acc_dir}/hw/hls-work-{ESP_TECHLIB}"
    installed_rel = f"tech/{ESP_TECHLIB}/acc/{spec.make_name}"

    bash_tool = None
    # The loop's members run on this bundle; the FPGA program/run members need
    # the board seat too. (synth is dispatched separately to esp_vivado.)
    bundle = {"CPU": 1, "esp": 1, "esp_xcelium": 1}
    if args.fpga:
        bundle["esp_fpga"] = 1
    with EspWorkspaceNode(reserve_bundle=bundle) as ws:

        print("\n=== setup: build the SoC once, then scrub the kernel ===")
        t0 = time.time()
        gen = get(ws.accgen.chia_remote(ESP_ROOT, spec, overwrite=True))
        hls = get(ws.accel.chia_remote(ESP_ROOT, ESP_BOARD, spec.make_name, "hls"))
        col = get(ws.collect.chia_remote(ESP_ROOT, [DEFCONFIG]))
        cfg = get(ws.configure.chia_remote(
            ESP_ROOT, ESP_BOARD,
            esp_config=with_acc_tile(col.files[DEFCONFIG], spec.make_name,
                                     ACC_ROW, ACC_COL, impl=IMPL)))
        soft = get(ws.build.chia_remote(ESP_ROOT, ESP_BOARD, cpu=ESP_CPU))
        bm = get(ws.accel.chia_remote(ESP_ROOT, ESP_BOARD, spec.make_name,
                                      "baremetal"))
        for label, res in (("accgen", gen), ("hls", hls), ("configure", cfg),
                           ("soft", soft), ("baremetal", bm)):
            print(f"{label:9s}: success={res.success}")
            if not res.success:
                _tail(f"{label} stdout", res.stdout)
                _tail(f"{label} stderr", res.stderr)
                return 1

        exe_col = get(ws.collect.chia_remote(
            board_dir(ESP_ROOT, ESP_BOARD),
            [f"soft-build/**/{spec.make_name}*.exe"], max_bytes_per_file=1))
        exes = sorted(exe_col.skipped)
        if not exes:
            print(f"no {spec.make_name}*.exe found under soft-build")
            return 1
        test_program = f"./{exes[0]}"

        # Scrub everything derived from the generated kernel, so only RTL
        # synthesized from the agent's own espacc.cc can reach a simulation.
        get(ws.remove.chia_remote(ESP_ROOT, hls_work_rel))
        get(ws.remove.chia_remote(ESP_ROOT, installed_rel))
        get(ws.put_file.chia_remote(ESP_ROOT, kernel_rel, STUB_KERNEL))
        print(f"setup done in {time.time() - t0:.0f}s; kernel is now a stub")

        bash_tool = BashTool(
            name=f"esp_bash_{uuid4().hex[:8]}",
            work_dir=acc_dir_abs,
            timeout_seconds=120,
            task_options={
                "scheduling_strategy": PlacementGroupSchedulingStrategy(
                    placement_group=ws.placement_group,
                    placement_group_bundle_index=0,
                )
            },
        )

        feedback = ("nothing has been evaluated yet: espacc.cc is an empty "
                    "stub awaiting your implementation")
        converged = False
        try:
            for it in range(1, MAX_ITERS + 1):
                print(f"\n=== iteration {it}/{MAX_ITERS} ===")
                t1 = time.time()
                source = get(ws.collect.chia_remote(
                    ESP_ROOT, [kernel_rel])).files[kernel_rel]
                llm = get(write_kernel_llm.chia_remote(
                    _build_prompt(acc_dir_abs, source, feedback),
                    MODEL, [bash_tool]))
                print(f"llm      : success={llm.success} "
                      f"({time.time() - t1:.0f}s)")
                _tail("llm summary", llm.result or llm.stderr, n=800)

                # Fresh synthesis of exactly what the agent wrote: without the
                # scrub, a failed csynth would leave the previous iteration's
                # RTL in the project dir and `make ... install` would silently
                # re-install it.
                get(ws.remove.chia_remote(ESP_ROOT, hls_work_rel))
                get(ws.remove.chia_remote(ESP_ROOT, installed_rel))
                hls = get(ws.accel.chia_remote(ESP_ROOT, ESP_BOARD,
                                               spec.make_name, "hls"))
                installed = get(ws.collect.chia_remote(
                    ESP_ROOT, [f"{installed_rel}/{spec.make_name}_{IMPL}/*.v"],
                    max_bytes_per_file=1))
                if not (hls.success and installed.skipped):
                    print(f"iteration {it}: hls-failed "
                          f"({time.time() - t1:.0f}s)")
                    feedback = (f"HLS synthesis FAILED (no RTL was produced)."
                                f"\nlog tail:\n"
                                f"{(hls.stdout + hls.stderr)[-1500:]}")
                    continue

                sim = get(ws.sim.chia_remote(
                    ESP_ROOT, ESP_BOARD, test_program=test_program,
                    pass_pattern="Program Completed", clean=True,
                    timeout_seconds=SIM_TIMEOUT))
                verdict = _sim_verdict(sim)
                print(f"iteration {it}: {verdict} ({time.time() - t1:.0f}s)")
                _tail("sim transcript", sim.stdout, n=1200)
                if verdict == "validation-pass":
                    print(f"\nPASS (simulation): memcpy accelerator validated "
                          f"in RTL sim after {it} iteration(s).")
                    converged = True
                    break
                feedback = (
                    f"RTL simulation verdict: {verdict}\n"
                    + ("(the simulation timed out — a kernel whose DMA "
                       "control handshake is wrong hangs the SoC's "
                       "interconnect)\n" if sim.returncode == -1 else "")
                    + f"transcript tail:\n{sim.stdout[-1500:]}")
        finally:
            bash_tool.stop()

        if not converged:
            print(f"\nFAIL: no validated kernel within {MAX_ITERS} iterations.")
            return 1

        if not args.fpga:
            return 0

        # Capstone: the same kernel, now on real silicon.
        ok = _fpga_deploy(ws, ESP_ROOT, spec)
        print("\n" + ("PASS: the agent's accelerator ran on the FPGA — its "
                      "self-test passed on hardware." if ok else
                      "FAIL: FPGA deploy/run did not pass; see markers above."))
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
