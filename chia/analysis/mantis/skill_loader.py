"""Turn a dv-mantis ``SKILL.md`` into a stage prompt for a coding agent.

The dv-mantis repo (https://github.com/sw23/dv-mantis) is the *source of truth*
for the review skills. Rather than copy their text into CHIA, the loop points at
a checkout and loads each stage's ``SKILL.md`` at run time. The skill body — the
instructions the human would get by typing ``/mantis-researcher`` — becomes the
system/user prompt for the agent node. This is precisely how a Mantis skill
"becomes a CHIA function": the discrete stage node wraps the skill body, wires in
the concrete workspace paths, and dispatches it to a coding-agent worker.

The map from stage name to skill directory follows dv-mantis's ``mantis-<stage>``
convention. No Ray import here; the loader is pure filesystem + string work and
is unit-testable against a fixture skills directory.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Optional

from chia.analysis.mantis import schema

# dv-mantis names each skill directory ``mantis-<stage>`` (hyphenated). CHIA stage
# names use underscores (e.g. ``threat_model``), so underscores are converted to
# hyphens to form the directory. Kept explicit so a rename in dv-mantis surfaces
# as a clear KeyError here instead of a silent miss.
def _skill_dir(stage: str) -> str:
    return "mantis-" + stage.replace("_", "-")


STAGE_TO_SKILL_DIR: Dict[str, str] = {stage: _skill_dir(stage) for stage in schema.ALL_STAGES}

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def skill_path(skills_root: str, stage: str) -> str:
    """Absolute path to ``<skills_root>/mantis-<stage>/SKILL.md``."""
    try:
        skill_dir = STAGE_TO_SKILL_DIR[stage]
    except KeyError as exc:
        raise KeyError(f"no dv-mantis skill mapped for stage {stage!r}") from exc
    return os.path.join(os.path.abspath(skills_root), skill_dir, "SKILL.md")


def load_skill_body(skills_root: str, stage: str) -> str:
    """Read a stage's SKILL.md and strip its YAML frontmatter.

    The frontmatter (``name``/``description``) is metadata for a slash-command
    registry; the agent only needs the instruction body. Raises FileNotFoundError
    with an actionable message if the skills checkout is missing or mis-pointed.
    """
    path = skill_path(skills_root, stage)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"SKILL.md for stage {stage!r} not found at {path}. "
            f"Set the skills root to a checkout of https://github.com/sw23/dv-mantis."
        )
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def render_stage_prompt(
    skills_root: str,
    stage: str,
    workspace_dir: str,
    design_dir: str,
    extra: Optional[str] = None,
) -> str:
    """Compose the full prompt handed to a stage's coding-agent.

    Structure: a short harness preamble that pins the concrete paths and the
    non-negotiable state contract, then the verbatim skill body, then any
    per-invocation ``extra`` (e.g. the specific investigation for a fanned-out
    researcher, or the finding id for a per-finding review). Keeping the skill
    body verbatim means improvements in dv-mantis flow through unchanged.
    """
    body = load_skill_body(skills_root, stage)
    findings_dir = os.path.join(workspace_dir, "findings")
    preamble = (
        f"You are executing the Mantis '{stage}' stage inside a deterministic "
        f"CHIA harness.\n\n"
        f"Concrete paths for this run (use these exactly):\n"
        f"  - Design under review (repo root, your working directory): {design_dir}\n"
        f"  - Mantis workspace: {workspace_dir}\n"
        f"  - Findings directory: {findings_dir}\n"
        f"  - plan.json: {os.path.join(workspace_dir, 'plan.json')}\n"
        f"  - Knowledge base: {os.path.join(workspace_dir, 'kb')}\n"
        f"  - learnings.jsonl: {os.path.join(workspace_dir, 'learnings.jsonl')}\n\n"
        f"State contract (schema.json): findings live one-per-file at "
        f"findings/<uuid>.json. When you need to create, update, merge, or delete "
        f"a finding, prefer the provided finding-store tools over ad-hoc file "
        f"edits or one-off scripts. Do not print full finding JSON back to me — "
        f"return only ids/status/counts. The harness runs verification and "
        f"control flow; do not invoke the next stage yourself.\n\n"
        f"--- BEGIN SKILL: {STAGE_TO_SKILL_DIR[stage]} ---\n"
    )
    closing = f"\n--- END SKILL: {STAGE_TO_SKILL_DIR[stage]} ---\n"
    prompt = preamble + body + closing
    if extra:
        prompt += f"\nThis invocation's specific task:\n{extra}\n"
    return prompt
