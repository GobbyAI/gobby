"""Human-friendly display names for cron jobs.

System cron identifiers are stable machine names like
``gobby:wiki-recap:project:<uuid>``. These helpers derive a readable
default display name from that identifier (resolving project UUIDs to
project names) while leaving the identifier itself untouched. A stored
``display_name`` on the row always wins over the generated default.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from gobby.storage.cron_models import CronJob

_GOBBY_PREFIX = "gobby:"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Ad hoc task-monitor jobs: gobby:monitor:<start>-<end>-<cadence>
_MONITOR_RE = re.compile(r"^monitor:(\d+)-(\d+)-([a-z]+)$")
_WORD_OVERRIDES = {"github": "GitHub"}


def _humanize(segment: str) -> str:
    return " ".join(_WORD_OVERRIDES.get(word, word) for word in segment.split("-"))


def default_cron_display_name(name: str, project_names: Mapping[str, str]) -> str | None:
    """Generate a display name for a ``gobby:``-namespaced cron identifier.

    Returns None for names outside the ``gobby:`` namespace — those are
    user-chosen and render as-is.
    """
    if not name.startswith(_GOBBY_PREFIX):
        return None
    rest = name[len(_GOBBY_PREFIX) :]

    monitor = _MONITOR_RE.match(rest)
    if monitor:
        start, end, cadence = monitor.groups()
        return f"Monitor tasks #{start}–#{end} ({cadence})"

    words: list[str] = []
    scopes: list[str] = []
    segments = rest.split(":")
    index = 0
    while index < len(segments):
        segment = segments[index]
        next_segment = segments[index + 1] if index + 1 < len(segments) else None
        if segment == "project" and next_segment and _UUID_RE.match(next_segment):
            scopes.append(project_names.get(next_segment, next_segment[:8]))
            index += 2
            continue
        if _UUID_RE.match(segment):
            scopes.append(project_names.get(segment, segment[:8]))
            index += 1
            continue
        words.append(_humanize(segment))
        index += 1

    label = " ".join(word for word in words if word).strip()
    if label:
        label = label[0].upper() + label[1:]
    else:
        label = name
    if scopes:
        label = f"{label} — {', '.join(scopes)}"
    return label


def effective_cron_display_name(job: CronJob, project_names: Mapping[str, str]) -> str:
    """Resolve the display name shown for a job: stored override, generated
    default, or the raw identifier."""
    stored = (job.display_name or "").strip()
    if stored:
        return stored
    return default_cron_display_name(job.name, project_names) or job.name
