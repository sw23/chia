"""LLM nodes for the MemCpy example: implement + debug.

Both nodes drive an LLM backend — :class:`chia.models.claude.ClaudeCodeLLM` or
:class:`chia.models.opencode.OpenCodeLLM`, chosen per run with ``--llm`` — over
the ``chipyard_bash`` MCP tool (a BashTool deployed into the chipyard
container). A single LLM instance is built once (see :func:`make_llm`) and
reused across the whole loop; for Claude Code that lets the debug calls
``--resume`` the implement session and remember what they already tried.

  * :func:`implement` — first call. Writes ``memcpy.scala`` (the RoCC
    accelerator) and wires it into the target config. Runs in parallel with
    the test build.
  * :func:`debug` — feedback call. Given a formatted failure context (build
    error, runtime/timeout, or incorrect result), edits the Chisel to fix it.

The feedback builders mirror the information the other chia examples hand their
debugger (build stderr/stdout windowed on the first error; sim log/out tails;
the BOOM/chipyard source location) and, for verilator failures, additionally
include the last N lines of ``memcpy.dump`` and of the commit log — per this
example's spec.
"""

from __future__ import annotations

from pathlib import Path

from chia.base.ChiaFunction import get
from chia.base.tools.BashTool import BashTool
from chia.base.llm_call import QueryResult
from chia.models.claude import ClaudeCodeLLM
from chia.models.opencode import OpenCodeLLM

from constants import (
    BUILD_CONFIG,
    CHIPYARD_SRC_PATH,
    COMMIT_LOG_TAIL_LINES,
    DATA_SIZE,
    DUMP_TAIL_LINES,
    LLM_EXTRA_CLI_ARGS,
    LLM_MODEL,
    LLM_RESOURCE,
    LLM_SYSTEM_MESSAGE,
    LLM_TIMEOUT_SECONDS,
    MAX_OUTPUT_CHARS,
    OPENCODE_MODEL,
    OPENCODE_RESOURCE,
)
from helpers import Outcome
from chia.chipyard.state_def import BuildArtifact, RunResult


# ---------------------------------------------------------------------------
# Prompts (loaded from prompts/, with ${VAR} placeholders substituted)
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str, **subs: str) -> str:
    """Read prompts/<name> and replace ${KEY} placeholders with subs values.

    ``${KEY}`` (rather than str.format's ``{KEY}``) so prompt text can contain
    literal braces (Chisel/Scala snippets) without escaping.
    """
    text = (PROMPTS_DIR / name).read_text()
    for key, val in subs.items():
        text = text.replace("${" + key + "}", val)
    return text


# implement.md: the accelerator spec + custom-instruction ABI + the "only write
# Chisel" task wrapper. Kept in lockstep with memcpy.c, which issues
# ROCC_INSTRUCTION_DSS(1, rd, src, dst, 0) then ROCC_INSTRUCTION_DS(1, rd, len, 1)
# — opcode custom1, funct 0 = latch src/dst addrs, funct 1 = length + start.
_IMPLEMENT_TASK = _load_prompt(
    "implement.md",
    BUILD_CONFIG=BUILD_CONFIG,
    DATA_SIZE=str(DATA_SIZE),
    CHIPYARD_SRC_PATH=CHIPYARD_SRC_PATH,
)

# debug.md: the debugger charter — find the real root cause, don't revert /
# disable the feature, don't run tests yourself.
_DEBUGGER_PREAMBLE = _load_prompt(
    "debug.md",
    BUILD_CONFIG=BUILD_CONFIG,
    CHIPYARD_SRC_PATH=CHIPYARD_SRC_PATH,
)


# ---------------------------------------------------------------------------
# Feedback formatting
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int, keep: str = "tail") -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    if keep == "tail":
        return f"... [{len(text) - max_chars} chars truncated] ...\n" + text[-max_chars:]
    return text[:max_chars] + f"\n... [{len(text) - max_chars} chars truncated] ..."


def _tail_lines(text: str, n: int) -> str:
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:])


def format_build_failure(artifact: BuildArtifact, attempt: int) -> str:
    """Build-error context — stderr tail + stdout windowed on the first error.

    Same shape as the other examples' build-failure feedback.
    """
    stderr = _truncate(artifact.stderr, MAX_OUTPUT_CHARS, keep="tail")
    stdout_lines = artifact.stdout.splitlines()
    first_error = next((i for i, ln in enumerate(stdout_lines) if "error" in ln.lower()), None)
    if first_error is not None:
        stdout = "\n".join(stdout_lines[max(0, first_error - 5):])
    else:
        stdout = _truncate(artifact.stdout, MAX_OUTPUT_CHARS, keep="tail")
    return (
        f"# Build Failure — Debug Attempt {attempt}\n\n"
        f"The `{BUILD_CONFIG}` Chisel build failed (rc={artifact.returncode}).\n\n"
        f"## Build stderr\n```\n{stderr}\n```\n\n"
        f"## Build stdout (from first error)\n```\n{stdout}\n```\n\n"
        f"Fix ONLY the compilation errors. Do not refactor or optimize.\n"
    )


def format_sim_failure(
    run: RunResult,
    outcome: Outcome,
    dump: str,
    attempt: int,
) -> str:
    """Simulation-failure context (runtime / timeout / incorrect).

    Includes the sim log + spike-dasm'd out tails (the commit log, since the
    target is a HumanCommitLog config), and — per this example's spec — the
    last N lines of the test disassembly (memcpy.dump) and of the commit log.
    """
    log_tail = _truncate(run.log, MAX_OUTPUT_CHARS // 5, keep="tail")
    out_tail = _truncate(run.out, MAX_OUTPUT_CHARS // 5, keep="tail")
    commit_tail = _tail_lines(run.out or run.log, COMMIT_LOG_TAIL_LINES)
    dump_tail = _tail_lines(dump, DUMP_TAIL_LINES)

    if outcome.kind == "timeout":
        headline = (
            "The simulation TIMED OUT (the design likely hung — e.g. a RoCC or "
            "memory handshake that never completes, or a copy loop that never "
            "terminates)."
        )
    elif outcome.kind == "runtime":
        headline = f"The simulation hit a RUNTIME ERROR ({outcome.detail})."
    else:  # incorrect
        headline = (
            f"The simulation ran but produced an INCORRECT result "
            f"({outcome.detail}). The copied data does not match the source."
        )

    return (
        f"# Simulation Failure — Debug Attempt {attempt}\n\n"
        f"{headline}\n\n"
        f"## Simulator stdout (commit log + program output)\n```\n{log_tail}\n```\n\n"
        f"## spike-dasm output tail\n```\n{out_tail}\n```\n\n"
        f"## Commit log — last {COMMIT_LOG_TAIL_LINES} lines\n```\n{commit_tail}\n```\n\n"
        f"## Test disassembly (memcpy.dump) — last {DUMP_TAIL_LINES} lines\n"
        f"```\n{dump_tail}\n```\n\n"
        f"Use the disassembly to confirm which custom instructions the test "
        f"issues (funct 0 = load src/dst addresses, funct 1 = length + start) "
        f"and check your accelerator handles both and drives the RoCC response.\n"
    )


# ---------------------------------------------------------------------------
# LLM nodes  (dispatched onto the dedicated claude "llm" or "opencode" worker)
# ---------------------------------------------------------------------------
#
# Both ClaudeCodeLLM.prompt and OpenCodeLLM.prompt are @ChiaFunctions, so the
# CLI runs on the worker holding the backend's resource ("llm" for claude,
# "opencode_creds" for opencode), not on the head. The backend is chosen per run
# via the loop's --llm flag.
#
# Claude: implement (first call) and every debug call share ONE session — a
# fixed session_id plus the session transcript threaded from each call into the
# next (the @_session_tracked wrapper syncs cli.session_transcript on get), so
# the debugger resumes the implement conversation and remembers prior fixes.
# Session persistence for other backends (opencode) is in development; for now
# each opencode call is independent and the full failure context is re-supplied
# inline (see format_build_failure / format_sim_failure) regardless.


def make_llm(backend: str, chipyard_bash: BashTool):
    """Build the implement/debug LLM ONCE, to be reused across the whole loop.

    Reusing a single instance is what lets ``ClaudeCodeLLM``'s
    ``@_session_tracked`` wrapper thread the session automatically: each
    ``get()`` syncs the transcript *and* advances the call counter onto this
    instance, so every ``debug`` call ``--resume``s the ``implement``
    conversation with no manual session bookkeeping. (Session persistence for
    OpenCode is in development — each OpenCode call is independent — so reuse is
    just a convenience there; the failure context is re-supplied inline.)
    """
    if backend == "opencode":
        return OpenCodeLLM(
            model=OPENCODE_MODEL,
            system_message=LLM_SYSTEM_MESSAGE,
            timeout_seconds=LLM_TIMEOUT_SECONDS,
            logging_name="memcpy_generator",
            # Restrict opencode to ONLY the chipyard_bash MCP tool. Its built-in
            # write/edit/bash tools act on the opencode container's own FS, not
            # the chipyard container — deny all, allow just this MCP server.
            config={"*": "deny", f"{chipyard_bash.name}_*": "allow"},
        )
    return ClaudeCodeLLM(
        model=LLM_MODEL,
        system_message=LLM_SYSTEM_MESSAGE,
        timeout_seconds=LLM_TIMEOUT_SECONDS,
        logging_name="memcpy_generator",
        resume_session=True,   # implement + every debug call share one session
        projects_cwd=None,     # derive from the llm worker's CWD
        extra_cli_args=list(LLM_EXTRA_CLI_ARGS),
    )


def _run_llm(llm, prompt: str, chipyard_bash: BashTool) -> QueryResult:
    """Dispatch *prompt* to *llm*'s worker (its backend's creds resource)."""
    resources = (
        {"opencode_creds": OPENCODE_RESOURCE}
        if isinstance(llm, OpenCodeLLM)
        else {"llm": LLM_RESOURCE}
    )
    return get(
        llm.prompt.options(resources=resources).chia_remote(llm, prompt, [chipyard_bash])
    )


def implement(llm, chipyard_bash: BashTool) -> QueryResult:
    """First call: write the accelerator + config wiring via chipyard_bash."""
    return _run_llm(llm, _IMPLEMENT_TASK, chipyard_bash)


def debug(llm, chipyard_bash: BashTool, feedback: str) -> QueryResult:
    """Feedback call: diagnose and fix *feedback*. For claude this ``--resume``s
    the shared implement session (the reused instance carries the transcript +
    counter); for opencode the full failure context is inline in *feedback*."""
    return _run_llm(llm, f"{_DEBUGGER_PREAMBLE}\n\n{feedback}", chipyard_bash)
