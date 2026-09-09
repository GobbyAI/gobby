"""Render recorded feedback evidence as a compact Markdown digest."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from gobby.feedback.storage import FeedbackRow
from gobby.sessions.handoff import FEEDBACK_TASK_REF_RE
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed

_RESOLVED_DISPOSITIONS = ("filed-task", "fixed")


def _shirked_cluster_lines(
    rows: list[FeedbackRow],
    findings: dict[str, Any],
    resolve_task: Callable[[FeedbackRow, str], Any | None] | None = None,
) -> list[str]:
    """One digest line per actionable cluster no observer filed or fixed in-line."""
    rows_by_id = {row.id: row for row in rows}
    lines: list[str] = []
    for cluster in findings["clusters"]:
        if cluster.get("classification") not in ("defect", "guidance-gap"):
            continue
        observed = [
            rows_by_id[obs_id]
            for obs_id in cluster.get("observation_ids", [])
            if obs_id in rows_by_id
        ]
        if not observed:
            continue
        resolved = False
        unclaimed_refs: list[str] = []
        for row in observed:
            if row.disposition not in _RESOLVED_DISPOSITIONS:
                continue
            if row.disposition == "fixed":
                resolved = True
                break
            match = FEEDBACK_TASK_REF_RE.search(row.evidence)
            task_ref = match.group(0) if match is not None else None
            task = resolve_task(row, task_ref) if resolve_task is not None and task_ref else None
            labels = set(getattr(task, "labels", ()) or ())
            if (
                is_task_closed(task)
                or get_claimed_session_id(task) is not None
                or {"needs-decision", "clean-window"}.intersection(labels)
            ):
                resolved = True
                break
            if task_ref is not None and task_ref not in unclaimed_refs:
                unclaimed_refs.append(task_ref)
        if resolved:
            continue
        dispositions = Counter(row.disposition or "none" for row in observed)
        breakdown = ", ".join(f"{name} {count}" for name, count in dispositions.most_common())
        unresolved = f"; {', '.join(unclaimed_refs)} filed unclaimed" if unclaimed_refs else ""
        lines.append(
            f"- **{cluster.get('theme', '(untitled)')}** "
            f"({len(observed)} obs; dispositions: {breakdown}{unresolved})"
        )
    return lines


def render_digest(
    rows: list[FeedbackRow],
    findings: dict[str, Any],
    actions: dict[str, Any],
    *,
    dry_run: bool,
    resolve_task: Callable[[FeedbackRow, str], Any | None] | None = None,
) -> str:
    kind_counts = Counter(row.kind for row in rows)
    lines = ["# Session-feedback review digest", ""]
    if dry_run:
        lines.extend(["**Dry run** — no tasks filed, no rows marked reviewed.", ""])
    lines.append(f"Rows considered: {len(rows)}")
    lines.append(
        "Counts by kind: "
        + ", ".join(f"{kind} {count}" for kind, count in kind_counts.most_common())
    )
    lines.append("")

    lines.append("## Clusters")
    for cluster in findings["clusters"]:
        classification = cluster.get("classification", "?")
        size = len(cluster.get("observation_ids", []))
        lines.append(f"- **{cluster.get('theme', '(untitled)')}** [{classification}, {size} obs]")
        note = str(cluster.get("digest_note") or "").strip()
        if note:
            lines.append(f"  {note}")
    lines.append("")

    lines.append("## Actions")
    for filed in actions.get("filed", []):
        lines.append(f"- Filed #{filed.get('task_id', '?')}: {filed.get('title', '')}")
    for suppressed in actions.get("suppressed", []):
        matched = suppressed.get("matched_task_ref")
        matched_suffix = f" ({matched})" if matched else ""
        lines.append(
            f"- Suppressed {suppressed.get('title', '(untitled)')}{matched_suffix}: "
            f"{suppressed.get('reason', 'resolved before filing')}"
        )
    if actions.get("deduplicated"):
        lines.append(f"- Deduplicated against existing tasks: {actions['deduplicated']}")
    for skipped in actions.get("skipped", []):
        lines.append(f"- Skipped: {skipped}")
    if (
        not actions.get("filed")
        and not actions.get("suppressed")
        and not actions.get("deduplicated")
        and not actions.get("skipped")
    ):
        lines.append("- None")
    lines.append("")

    lines.append("## Shirked found work")
    shirked = _shirked_cluster_lines(rows, findings, resolve_task)
    if shirked:
        lines.append(
            "Actionable clusters whose observations were deferred into the survey "
            "instead of being filed or fixed in-line per the found-work ladder:"
        )
        lines.extend(shirked)
    else:
        lines.append(
            "- None — every actionable cluster had a fixed disposition or a filed-task "
            "whose task was closed, claimed, or labeled needs-decision/clean-window."
        )
    lines.append("")

    label_counts = Counter(row.kind_other_label for row in rows if row.kind_other_label is not None)
    lines.append("## Other-label audit")
    if not label_counts:
        lines.append("- No `other` observations in this batch.")
    else:
        for label, count in label_counts.most_common():
            suffix = " — recurring; consider promoting to the kind enum" if count >= 2 else ""
            lines.append(f"- `{label}`: {count}{suffix}")
    return "\n".join(lines).strip()
