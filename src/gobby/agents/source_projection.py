"""Bounded Markdown evidence projections selected by caller-supplied YAML."""

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from gobby.utils.project_context import find_project_root


class SectionSpec(BaseModel):
    """Document selectors supplied by a workflow rather than inferred from a seat."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["table", "bullets", "topics"]
    headings: list[str] = Field(default_factory=list)
    heading_prefixes: list[str] = Field(default_factory=list)
    columns: dict[str, int] = Field(default_factory=dict)
    ref_columns: list[str] = Field(default_factory=list)
    row_pattern: str = ".*"
    topics: dict[str, str] = Field(default_factory=dict)
    trim_marker: str | None = None
    last: int = Field(default=2, ge=1)


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_glob: str
    date_pattern: str
    timezone: str = "UTC"
    sections: dict[str, SectionSpec]


def task_refs(text: str) -> list[str]:
    """Expand bounded task ranges in order, excluding qualified session refs."""
    refs: list[str] = []
    for match in re.finditer(r"(?<![\w])#(\d+)(?:\s*[–—-]\s*#(\d+))?", text):
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if last < first or last - first > 100:
            raise ValueError("Task range exceeds bounded resolution")
        for number in range(first, last + 1):
            ref = f"#{number}"
            if ref not in refs:
                refs.append(ref)
    return refs


def project_markdown(content: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Select level-two headings, then project configured tables or list entries."""
    config = SectionSpec.model_validate(spec)
    parts = re.split(r"^## ([^\n]+)\n", content, flags=re.MULTILINE)
    sections = [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]
    gaps = [
        f"Missing heading: {name}"
        for name in config.headings
        if not any(heading == name for heading, _ in sections)
    ]
    for prefix in config.heading_prefixes:
        if not any(name.startswith(prefix) for name, _ in sections):
            gaps.append(f"Missing heading prefix: {prefix}")
    selected = [
        (name, body)
        for name, body in sections
        if name in config.headings or any(name.startswith(p) for p in config.heading_prefixes)
    ]
    if not config.headings and not config.heading_prefixes:
        raise ValueError("At least one heading selector is required")
    if config.mode == "table":
        if not config.columns or any(index < 0 for index in config.columns.values()):
            raise ValueError("Table columns must have nonnegative indices")
        if not set(config.ref_columns).issubset(config.columns):
            raise ValueError("Reference columns must be selected columns")
        rows: list[dict[str, Any]] = []
        for heading, body in selected:
            for line in body.splitlines():
                if not line.startswith("|"):
                    continue
                cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
                if not cells or not re.search(config.row_pattern, cells[0]):
                    continue
                if max(config.columns.values()) >= len(cells):
                    gaps.append(f"Short table row in {heading}: {line}")
                    continue
                rows.append(
                    {
                        key: task_refs(cells[index]) if key in config.ref_columns else cells[index]
                        for key, index in config.columns.items()
                    }
                )
        return {"entries": rows, "gaps": gaps}
    if config.mode == "bullets":
        return {
            "entries": [
                {"heading": name, "text": item.strip()}
                for name, body in selected
                for item in re.split(r"(?m)^(?=[-*+] |\d+[.)] )", body)
                if item.strip()
            ],
            "gaps": gaps,
        }
    updates = [
        line.split(config.trim_marker, 1)[0].strip() if config.trim_marker else line.strip()
        for _, body in selected
        for line in body.splitlines()
        if line.startswith("- ")
    ]
    return {
        **{
            topic: [line for line in updates if re.search(pattern, line, re.IGNORECASE)][
                -config.last :
            ]
            for topic, pattern in config.topics.items()
        },
        "gaps": gaps,
    }


def page_projection(
    projection: dict[str, Any], metadata: dict[str, Any], offset: int, budget: int = 14_000
) -> dict[str, Any]:
    """Page whole entries, measuring UTF-8 bytes including the JSON envelope."""
    if offset < 0:
        raise ValueError("offset must be nonnegative")
    entries = [
        (name, entry)
        for name, values in projection.items()
        if isinstance(values, list)
        for entry in values
    ]
    evidence = {
        name: [] if isinstance(value, list) else value for name, value in projection.items()
    }
    result = {
        **metadata,
        "evidence": evidence,
        "offset": offset,
        "count": 0,
        "total": len(entries),
        "next_offset": None,
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) + 1 >= budget:
        raise ValueError("Projection metadata exceeds page budget")
    for index in range(offset, len(entries)):
        name, entry = entries[index]
        evidence[name].append(entry)
        result["count"] = index - offset + 1
        result["next_offset"] = index + 1 if index + 1 < len(entries) else None
        if len(json.dumps(result, ensure_ascii=False).encode()) + 1 >= budget:
            evidence[name].pop()
            result["count"] = index - offset
            result["next_offset"] = index
            if index == offset:
                raise ValueError(f"Entry at offset {offset} exceeds page budget")
            break
    return result


def select_source(root: Path, pattern: str, date_pattern: str, today: date) -> tuple[date, Path]:
    """Choose today's matching file, otherwise the newest prior dated source."""
    candidates = []
    for path in root.glob(pattern):
        match = re.search(date_pattern, path.name)
        if not match or not path.is_file():
            continue
        if not match.lastindex:
            raise ValueError("date_pattern requires an ISO date capture group")
        try:
            source_date = date.fromisoformat(match[1])
        except ValueError:
            continue
        if source_date <= today:
            candidates.append((source_date, path))
    if not candidates:
        raise ValueError("No dated source at or before today")
    return max(candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", help="YAML projection specification")
    source.add_argument("--spec-file", type=Path, help="YAML projection specification file")
    parser.add_argument("--section", required=True)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    path: Path | None = None
    try:
        raw = args.spec_file.read_text(encoding="utf-8") if args.spec_file else args.spec
        config = SourceSpec.model_validate(yaml.safe_load(raw))
        zone = ZoneInfo(config.timezone)
        today = datetime.now(zone).date()
        root = find_project_root()
        if root is None:
            raise ValueError("Source projection requires a project root")
        if args.section not in config.sections:
            raise ValueError(f"Unknown configured section: {args.section}")
        source_date, path = select_source(root, config.source_glob, config.date_pattern, today)
        metadata = {
            "source": str(path),
            "source_date": source_date.isoformat(),
            "stale": source_date < today,
            "age_days": (today - source_date).days,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, zone).isoformat(),
            "section": args.section,
        }
        result = page_projection(
            project_markdown(
                path.read_text(encoding="utf-8"), config.sections[args.section].model_dump()
            ),
            metadata,
            args.offset,
        )
    except (OSError, ValueError, yaml.YAMLError, KeyError, re.error) as exc:
        result = {"source": str(path) if path else None, "section": args.section, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
