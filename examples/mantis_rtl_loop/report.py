"""Deterministic JSON -> Markdown report for a Mantis run.

Per the dv-mantis ``mantis-pipeline-adapter`` guidance, findings are internal
state and the human-facing report is rendered by deterministic code, not an LLM,
so it can never hallucinate or corrupt the results. Ships to workers as a
py_module but only ever runs on the head.
"""

from __future__ import annotations

from typing import Any, Dict, List

_PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _rank(finding: Dict[str, Any]) -> tuple:
    pr = _PRIORITY_RANK.get(finding.get("priority", ""), 4)
    risk = finding.get("mantis_risk_score", 0) or 0
    return (pr, -float(risk))


def render_markdown(findings: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    """Render a ranked Markdown report from calibrated findings."""
    findings = sorted(findings, key=_rank)
    lines: List[str] = []
    lines.append(f"# Mantis RTL Design-Bug Report — {meta.get('target', 'design')}")
    lines.append("")
    lines.append(f"- Iterations run: {meta.get('iterations', '?')}")
    lines.append(f"- Backend: {meta.get('backend', '?')}  ")
    lines.append(f"- Total findings: {len(findings)}")

    by_priority: Dict[str, int] = {}
    reproduced = verified = 0
    for f in findings:
        by_priority[f.get("priority", "UNSCORED")] = by_priority.get(f.get("priority", "UNSCORED"), 0) + 1
        if f.get("repro_status") == "reproduced":
            reproduced += 1
        if f.get("patch_status") == "VERIFIED_SECURE":
            verified += 1
    dist = ", ".join(f"{k}: {by_priority[k]}" for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
                     if k in by_priority) or "none"
    lines.append(f"- Priority distribution: {dist}")
    lines.append(f"- Reproduced: {reproduced}   Patched & verified: {verified}")
    lines.append("")

    if not findings:
        lines.append("_No findings this run._")
        return "\n".join(lines) + "\n"

    lines.append("## Findings (ranked)")
    lines.append("")
    lines.append("| Priority | Risk | Severity | Repro | Patch | Title |")
    lines.append("|---|---|---|---|---|---|")
    for f in findings:
        lines.append(
            f"| {f.get('priority', '-')} | {f.get('mantis_risk_score', '-')} "
            f"| {f.get('severity', '-')} | {f.get('repro_status', '-')} "
            f"| {f.get('patch_status', '-')} | {_esc(f.get('title', '(untitled)'))} |"
        )
    lines.append("")

    for f in findings:
        lines.append(f"### {_esc(f.get('title', '(untitled)'))}")
        lines.append("")
        lines.append(f"- **id:** `{f.get('id', '')}`")
        lines.append(f"- **priority / risk:** {f.get('priority', '-')} / {f.get('mantis_risk_score', '-')}")
        lines.append(f"- **severity:** {f.get('severity', '-')}  "
                     f"**privileges:** {f.get('privileges_required', '-')}  "
                     f"**interaction:** {f.get('user_interaction', '-')}")
        lines.append(f"- **status:** {f.get('status', '-')}  "
                     f"**viability:** {f.get('production_viability', '-')}  "
                     f"**repro:** {f.get('repro_status', '-')}")
        paths = ", ".join(f.get("code_paths", []) or []) or "-"
        lines.append(f"- **code paths:** {paths}")
        lines.append("")
        if f.get("description"):
            lines.append(f"**Description.** {f['description']}")
            lines.append("")
        if f.get("impact"):
            lines.append(f"**Impact.** {f['impact']}")
            lines.append("")
        if f.get("mitigation"):
            lines.append(f"**Mitigation.** {f['mitigation']}")
            lines.append("")
        if f.get("patch_diff"):
            lines.append("**Verified fix (diff):**")
            lines.append("```diff")
            lines.append(str(f["patch_diff"]))
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def _esc(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")
