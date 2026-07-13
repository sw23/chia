# esp_accel_loop — Agentic ESP Accelerator Generation (Vivado HLS)

An agentic loop in which Claude implements a hardware `memcpy` as an
[ESP](https://esp.cs.columbia.edu) accelerator tile in Vivado HLS C++ and
iterates until it passes hardware validation: a full-SoC RTL simulation
(Xcelium) whose generated bare-metal self-test DMAs 100 64-bit words
through the tile and checks output == input elementwise. With `--fpga`,
the validated accelerator is then synthesized, programmed onto a real
board, and its self-test re-run on silicon.

The agent works through a bash tool that executes **inside the ESP
workspace container**, starting in the accelerator's directory: it reads
the generated headers (the required top-level interface), can study
sibling accelerators, and edits `hw/src/espacc.cc` in place. Everything
else is harness, built from `chia.esp.EspWorkspaceNode` members; the LLM
call runs on a separate credentialed worker, and its bash tool is an MCP
server pinned into the workspace's placement group.

## Flow

```
setup (once)                              loop (≤ ESP_LOOP_MAX_ITERS)
  accgen skeleton ─ HLS ─ tile config       Claude edits hw/src/espacc.cc
  ─ soft ─ baremetal self-test              via the workspace bash tool
  then scrub:                                          │
    espacc.cc -> empty stub,                           ▼
    HLS work dir + installed RTL removed    scrub ─ HLS ─ RTL installed?
         └──────────────┬───────────┘          │ no: HLS log tail feeds back
                        ▼                      ▼ yes
                     iterate ◄──── full-SoC Xcelium sim: the self-test
                                   DMAs the buffer through the tile
                                      │
                        verdict + transcript tail feed back
                        validation-pass? ── yes ──> --fpga? ──> deploy
                                                       │ no
                                                       ▼
                                                     DONE
```

## Components

| File | Role |
|------|------|
| `accel_kernel_loop.py` | The whole loop: setup ladder, per-iteration scrub → HLS → sim, LLM dispatch (`write_kernel_llm`), feedback formatting, optional FPGA deploy. |
| `esp_loop_cluster.yaml` | Single-machine cluster: an ESP sim worker (`chia-esp` image; `esp`/`esp_xcelium` seats, plus the `esp_fpga` board seat), a credentialed LLM worker (`chia-claude-code` image; `claude_creds`), and `esp_vivado` synthesis seats on the head for `--fpga`. |

## What keeps the fitness signal honest

- The SoC and self-test binary are built once, from the accgen skeleton,
  **before** the kernel is reduced to an empty stub — the agent never
  touches the golden model or the test.
- Before every synthesis the HLS work dir and installed RTL are scrubbed
  (`ws.remove`): ESP's `make <acc>-hls` pipes the tool through `tee` and
  re-installs whatever RTL sits in the project dir, so without the scrub a
  failed csynth would silently re-install the previous iteration's
  hardware. Only RTL synthesized from the agent's own kernel reaches a
  simulation.
- A kernel with a broken DMA handshake hangs the SoC's interconnect, so
  the sim timeout (default 1800 s) is the price of a bad iteration — the
  timeout itself, plus a hint, is the feedback.

## Setup

General ESP-on-CHIA setup notes (worker image, CAD mounts, site
configuration) are in `docs/api/esp`; the concrete steps for this example:

1. **Images.** The ESP worker image (build from `dockerfiles/EspDockerfile`)
   on the sim host, and `ghcr.io/ucb-bar/chia-claude-code` on the LLM host
   (one machine may serve both).
2. **CAD tools.** Xcelium (simulation), Vivado (one-time simulation-library
   compile), and `vivado_hls` (the HLS flow; shipped with Vivado through
   2020.1) must be reachable from the sim worker via the bind mounts and
   license variables in the site env.
3. **Site env.** Copy `chia/esp/test/cluster/esp_site_env.example.sh` to
   `esp_site_env.sh` next to it (gitignored) and fill in your site's
   values. This example uses the simulation/HLS group of variables plus
   `CHIA_ESP_CLAUDE_CONFIG` — a `.claude` config directory (with
   credentials) mounted into the LLM worker.
4. **Bring the cluster up and run:**

```sh
ESP_CLUSTER_YAML=examples/esp_accel_loop/esp_loop_cluster.yaml \
    ./chia/esp/test/cluster/esp_cluster.sh up -y
python examples/esp_accel_loop/accel_kernel_loop.py
```

`ESP_LOOP_MODEL` selects the model (default `claude-sonnet-4-6`),
`ESP_LOOP_MAX_ITERS` bounds the loop (default 5), and `ESP_BOARD` /
`ESP_CPU` / `ESP_TECHLIB` / `ESP_SIM_TIMEOUT` select the simulation target.

## On real hardware (`--fpga`)

```sh
python examples/esp_accel_loop/accel_kernel_loop.py --fpga
```

Once the loop validates a kernel in simulation, `--fpga` re-synthesizes the
accelerator for an FPGA board's technology, synthesizes the full SoC
bitstream, programs the board, and runs the accelerator's own bare-metal
self-test over the board's UART — the simulation result confirmed on
silicon. It is off by default so the example needs no board. Additional
prerequisites, all covered in the FPGA section of `docs/api/esp`:

1. **A shared workspace.** FPGA synthesis runs outside the container (on a
   bare host worker), so the ESP checkout must live on the host and be
   bind-mounted into the container at the same path. Set
   `CHIA_ESP_SHARED_WORKSPACE` in the site env; the cluster YAML mounts it
   and the driver uses it as the ESP root for the whole run.
2. **Synthesis seats and toolchain on the head.** The YAML advertises
   `esp_vivado` on the Ray head and sources `CHIA_ESP_SYN_ENV` (synthesis
   Vivado on PATH, `XILINX_VIVADO`, license) there.
3. **A reachable board.** `hw_server` on the machine holding the JTAG cable
   (`CHIA_ESP_FPGA_HOST` / `CHIA_ESP_HW_SERVER_PORT`), the board's UART
   exposed over TCP (`CHIA_ESP_UART_HOST` / `CHIA_ESP_UART_PORT`), and a
   UDP route to the board's Ethernet for program loading
   (`CHIA_ESP_ESPLINK_IP`).
4. **Board selection.** The loop iterates on the fast simulation board;
   the deployment targets `ESP_FPGA_BOARD` / `ESP_FPGA_TECHLIB` /
   `ESP_FPGA_IMPL` (defaults: `xilinx-vcu118-xcvu9p`, `virtexup`,
   `dma64_w64`). `CHIA_ESP_VIVADO_SYN_BIN` / `CHIA_ESP_VIVADO_PROG_BIN`
   pick the synthesis and programming Vivados when they must differ.
