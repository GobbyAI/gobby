#!/usr/bin/env python3
"""Remap hub commit evidence after a git-filter-repo history rewrite."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from gobby.config.bootstrap import load_bootstrap
from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_STORED_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
_ZERO_SHA = "0" * 40
_SENSITIVE_PATHS = frozenset({"tasks.jsonl", "memories.jsonl"})


class HistoryScrubError(RuntimeError):
    """Raised when a history scrub safety condition is not satisfied."""


@dataclass(frozen=True, slots=True)
class ShaReference:
    source: str
    row_id: str
    value: str


@dataclass(frozen=True, slots=True)
class ShaPlan:
    replacements: Mapping[str, str]
    pruned: frozenset[str]
    unmatched: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScrubResult:
    project_id: UUID
    project_name: str
    reference_counts: Mapping[str, int]
    distinct_stored_shas: int
    changed_references: int
    unmatched_references: int
    pruned_references: int
    normalized_task_commit_entries: int
    applied: bool


_SCALAR_REFERENCE_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "checkpoints.commit_sha",
        """
        SELECT c.id::text AS row_id, c.commit_sha AS sha
        FROM checkpoints AS c
        JOIN tasks AS t ON t.id = c.task_id
        WHERE t.project_id = %s
        """,
    ),
    (
        "checkpoints.parent_sha",
        """
        SELECT c.id::text AS row_id, c.parent_sha AS sha
        FROM checkpoints AS c
        JOIN tasks AS t ON t.id = c.task_id
        WHERE t.project_id = %s
        """,
    ),
    (
        "task_artifacts.base_commit_sha",
        """
        SELECT a.task_id::text AS row_id, a.base_commit_sha AS sha
        FROM task_artifacts AS a
        JOIN tasks AS t ON t.id = a.task_id
        WHERE t.project_id = %s AND a.base_commit_sha IS NOT NULL
        """,
    ),
    (
        "task_delivery_campaigns.merge_sha",
        """
        SELECT c.task_id::text AS row_id, c.merge_sha AS sha
        FROM task_delivery_campaigns AS c
        JOIN tasks AS t ON t.id = c.task_id
        WHERE t.project_id = %s AND c.merge_sha IS NOT NULL
        """,
    ),
    (
        "task_stage_states.completed_commit_sha",
        """
        SELECT s.task_id::text || ':' || s.stage_name AS row_id,
               s.completed_commit_sha AS sha
        FROM task_stage_states AS s
        JOIN tasks AS t ON t.id = s.task_id
        WHERE t.project_id = %s AND s.completed_commit_sha IS NOT NULL
        """,
    ),
    (
        "tasks.closed_commit_sha",
        """
        SELECT t.id::text AS row_id, t.closed_commit_sha AS sha
        FROM tasks AS t
        WHERE t.project_id = %s AND t.closed_commit_sha IS NOT NULL
        """,
    ),
)

_SCALAR_UPDATE_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "checkpoints.commit_sha",
        """
        UPDATE checkpoints AS c
        SET commit_sha = m.new_sha
        FROM tasks AS t, _history_scrub_sha_map AS m
        WHERE c.task_id = t.id
          AND t.project_id = %s
          AND c.commit_sha = m.stored_sha
          AND c.commit_sha IS DISTINCT FROM m.new_sha
        """,
    ),
    (
        "checkpoints.parent_sha",
        """
        UPDATE checkpoints AS c
        SET parent_sha = m.new_sha
        FROM tasks AS t, _history_scrub_sha_map AS m
        WHERE c.task_id = t.id
          AND t.project_id = %s
          AND c.parent_sha = m.stored_sha
          AND c.parent_sha IS DISTINCT FROM m.new_sha
        """,
    ),
    (
        "task_artifacts.base_commit_sha",
        """
        UPDATE task_artifacts AS a
        SET base_commit_sha = m.new_sha
        FROM tasks AS t, _history_scrub_sha_map AS m
        WHERE a.task_id = t.id
          AND t.project_id = %s
          AND a.base_commit_sha = m.stored_sha
          AND a.base_commit_sha IS DISTINCT FROM m.new_sha
        """,
    ),
    (
        "task_delivery_campaigns.merge_sha",
        """
        UPDATE task_delivery_campaigns AS c
        SET merge_sha = m.new_sha
        FROM tasks AS t, _history_scrub_sha_map AS m
        WHERE c.task_id = t.id
          AND t.project_id = %s
          AND c.merge_sha = m.stored_sha
          AND c.merge_sha IS DISTINCT FROM m.new_sha
        """,
    ),
    (
        "task_stage_states.completed_commit_sha",
        """
        UPDATE task_stage_states AS s
        SET completed_commit_sha = m.new_sha
        FROM tasks AS t, _history_scrub_sha_map AS m
        WHERE s.task_id = t.id
          AND t.project_id = %s
          AND s.completed_commit_sha = m.stored_sha
          AND s.completed_commit_sha IS DISTINCT FROM m.new_sha
        """,
    ),
    (
        "tasks.closed_commit_sha",
        """
        UPDATE tasks AS t
        SET closed_commit_sha = m.new_sha
        FROM _history_scrub_sha_map AS m
        WHERE t.project_id = %s
          AND t.closed_commit_sha = m.stored_sha
          AND t.closed_commit_sha IS DISTINCT FROM m.new_sha
        """,
    ),
)


def parse_commit_map(path: Path) -> dict[str, str]:
    """Parse and validate git-filter-repo's commit-map."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoryScrubError(f"cannot read commit map {path}: {exc}") from exc
    if not lines or lines[0].split() != ["old", "new"]:
        raise HistoryScrubError(f"invalid commit-map header in {path}")

    mapping: dict[str, str] = {}
    for line_number, line in enumerate(lines[1:], start=2):
        fields = line.split()
        if len(fields) != 2:
            raise HistoryScrubError(f"invalid commit-map row {line_number}")
        old_sha, new_sha = fields
        if not _FULL_SHA_RE.fullmatch(old_sha) or not _FULL_SHA_RE.fullmatch(new_sha):
            raise HistoryScrubError(f"invalid commit-map SHA on row {line_number}")
        if old_sha in mapping:
            raise HistoryScrubError(f"duplicate old SHA in commit-map: {old_sha}")
        mapping[old_sha] = new_sha
    if not mapping:
        raise HistoryScrubError("commit-map contains no commits")
    return mapping


def plan_sha_rewrites(references: Sequence[ShaReference], mapping: Mapping[str, str]) -> ShaPlan:
    """Classify stored SHAs against one git-filter-repo commit map."""
    replacements: dict[str, str] = {}
    pruned: set[str] = set()
    unmatched: set[str] = set()
    old_shas = tuple(mapping)
    issues: list[str] = []
    for raw_value in sorted({reference.value for reference in references}):
        matching_references = [
            reference for reference in references if reference.value == raw_value
        ]
        locations = ", ".join(
            f"{reference.source}:{reference.row_id}" for reference in matching_references
        )
        value = raw_value.strip().lower()
        if raw_value != value or not _STORED_SHA_RE.fullmatch(value):
            issues.append(f"invalid stored commit SHA {raw_value!r} at {locations}")
            continue
        candidates = [old_sha for old_sha in old_shas if old_sha.startswith(value)]
        if not candidates:
            unmatched.add(raw_value)
            continue
        if len(candidates) != 1:
            issues.append(
                f"stored SHA {value} is an ambiguous commit-map prefix "
                f"({len(candidates)} matches) at {locations}"
            )
            continue
        new_sha = mapping[candidates[0]]
        if new_sha == _ZERO_SHA:
            non_nullable = [
                reference
                for reference in matching_references
                if reference.source in {"checkpoints.commit_sha", "checkpoints.parent_sha"}
            ]
            if non_nullable:
                issues.append(
                    f"stored SHA {value} maps to a pruned commit in required checkpoint "
                    f"evidence at {locations}"
                )
                continue
            pruned.add(raw_value)
            continue
        replacements[raw_value] = new_sha
    if issues:
        detail = "\n".join(f"- {issue}" for issue in issues[:20])
        suffix = f"\n- ... {len(issues) - 20} more" if len(issues) > 20 else ""
        raise HistoryScrubError(f"commit evidence preflight failed:\n{detail}{suffix}")
    return ShaPlan(
        replacements=replacements,
        pruned=frozenset(pruned),
        unmatched=frozenset(unmatched),
    )


def verify_scrubbed_repository(repo: Path, new_shas: Sequence[str]) -> None:
    """Require clean sensitive paths and commit objects for every replacement."""
    _run_git(repo, ["rev-parse", "--git-dir"])
    object_rows = _run_git(repo, ["rev-list", "--objects", "--all"]).splitlines()
    leaked_paths = sorted(
        path
        for row in object_rows
        if " " in row
        for path in [row.split(" ", 1)[1]]
        if _is_sensitive_history_path(path)
    )
    if leaked_paths:
        raise HistoryScrubError(
            "scrubbed repository still contains protected paths: " + ", ".join(leaked_paths[:5])
        )

    distinct_shas = sorted(set(new_shas))
    if not distinct_shas:
        return
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input="\n".join(distinct_shas) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git cat-file failed"
        raise HistoryScrubError(detail)
    observed = result.stdout.splitlines()
    expected = [f"{sha} commit" for sha in distinct_shas]
    if observed != expected:
        raise HistoryScrubError("one or more replacement SHAs are missing commit objects")


def run_with_connection(
    connection: psycopg.Connection[dict[str, object]],
    *,
    project_id: UUID,
    expected_project_name: str,
    mapping: Mapping[str, str],
    scrubbed_repo: Path,
    apply: bool,
) -> ScrubResult:
    """Preflight and optionally apply the SHA remap in one transaction."""
    isolation = "SERIALIZABLE" if apply else "REPEATABLE READ READ ONLY"
    connection.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
    if apply:
        connection.execute(
            """
            LOCK TABLE projects, tasks, checkpoints, task_artifacts,
                       task_delivery_campaigns, task_stage_states
            IN SHARE ROW EXCLUSIVE MODE
            """
        )

    project = connection.execute(
        "SELECT name FROM projects WHERE id = %s", (project_id,)
    ).fetchone()
    if project is None:
        raise HistoryScrubError(f"project does not exist: {project_id}")
    project_name = str(project["name"])
    if project_name != expected_project_name:
        raise HistoryScrubError(
            f"project name mismatch: expected {expected_project_name!r}, got {project_name!r}"
        )

    normalized_task_commit_entries = _count_split_commit_entries(connection, project_id)
    references = _collect_references(connection, project_id)
    plan = plan_sha_rewrites(references, mapping)
    verify_scrubbed_repository(scrubbed_repo, list(plan.replacements.values()))
    expected_after = _expected_reference_counter(references, plan)

    if apply:
        _apply_replacements(connection, project_id, plan)
        observed_after = Counter(
            (reference.source, reference.row_id, reference.value)
            for reference in _collect_references(connection, project_id)
        )
        if observed_after != expected_after:
            raise HistoryScrubError("post-update SHA evidence differs from the preflight plan")
        connection.commit()
    else:
        connection.rollback()

    return ScrubResult(
        project_id=project_id,
        project_name=project_name,
        reference_counts=Counter(reference.source for reference in references),
        distinct_stored_shas=len({reference.value for reference in references}),
        changed_references=sum(
            reference.value in plan.pruned
            or plan.replacements.get(reference.value, reference.value) != reference.value
            for reference in references
        ),
        unmatched_references=sum(reference.value in plan.unmatched for reference in references),
        pruned_references=sum(reference.value in plan.pruned for reference in references),
        normalized_task_commit_entries=normalized_task_commit_entries,
        applied=apply,
    )


def _expected_reference_counter(
    references: Sequence[ShaReference], plan: ShaPlan
) -> Counter[tuple[str, str, str]]:
    expected: Counter[tuple[str, str, str]] = Counter()
    task_commits: dict[str, list[str]] = {}
    for reference in references:
        if reference.value in plan.pruned:
            continue
        value = plan.replacements.get(reference.value, reference.value)
        if reference.source == "tasks.commits":
            task_id = reference.row_id.rsplit(":", maxsplit=1)[0]
            task_commits.setdefault(task_id, []).append(value)
            continue
        expected[(reference.source, reference.row_id, value)] += 1
    for task_id, values in task_commits.items():
        for ordinality, value in enumerate(values, start=1):
            expected[("tasks.commits", f"{task_id}:{ordinality}", value)] += 1
    return expected


def _collect_references(
    connection: psycopg.Connection[dict[str, object]], project_id: UUID
) -> list[ShaReference]:
    malformed = connection.execute(
        """
        SELECT id::text AS row_id
        FROM tasks
        WHERE project_id = %s
          AND commits IS NOT NULL
          AND jsonb_typeof(commits) <> 'array'
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if malformed is not None:
        raise HistoryScrubError(f"tasks.commits is not an array for task {malformed['row_id']}")

    references: list[ShaReference] = []
    for source, query in _SCALAR_REFERENCE_QUERIES:
        rows = connection.execute(query, (project_id,)).fetchall()
        references.extend(
            ShaReference(source=source, row_id=str(row["row_id"]), value=str(row["sha"]))
            for row in rows
        )
    commit_rows = connection.execute(
        """
        SELECT expanded.task_id, expanded.ordinality, expanded.sha
        FROM (
            SELECT t.id::text AS task_id,
                   row_number() OVER (
                       PARTITION BY t.id
                       ORDER BY e.ordinality, part.part_ordinality
                   ) AS ordinality,
                   part.sha
            FROM tasks AS t
            CROSS JOIN LATERAL jsonb_array_elements_text(t.commits)
                WITH ORDINALITY AS e(sha, ordinality)
            CROSS JOIN LATERAL regexp_split_to_table(
                e.sha,
                '[[:space:]]*,[[:space:]]*'
            ) WITH ORDINALITY AS part(sha, part_ordinality)
            WHERE t.project_id = %s AND t.commits IS NOT NULL
        ) AS expanded
        ORDER BY expanded.task_id, expanded.ordinality
        """,
        (project_id,),
    ).fetchall()
    references.extend(
        ShaReference(
            source="tasks.commits",
            row_id=f"{row['task_id']}:{row['ordinality']}",
            value=str(row["sha"]),
        )
        for row in commit_rows
    )
    return references


def _count_split_commit_entries(
    connection: psycopg.Connection[dict[str, object]], project_id: UUID
) -> int:
    row = connection.execute(
        """
        SELECT count(*) AS entry_count
        FROM tasks AS t
        CROSS JOIN LATERAL jsonb_array_elements_text(t.commits) AS e(sha)
        WHERE t.project_id = %s
          AND t.commits IS NOT NULL
          AND e.sha LIKE '%%,%%'
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise HistoryScrubError("failed to count split tasks.commits entries")
    entry_count = row["entry_count"]
    if not isinstance(entry_count, int):
        raise HistoryScrubError("split tasks.commits count is not an integer")
    return entry_count


def _apply_replacements(
    connection: psycopg.Connection[dict[str, object]],
    project_id: UUID,
    plan: ShaPlan,
) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE _history_scrub_sha_map (
            stored_sha text PRIMARY KEY,
            new_sha text
        ) ON COMMIT DROP
        """
    )
    map_rows: list[tuple[str, str | None]] = sorted(plan.replacements.items())
    map_rows.extend((stored_sha, None) for stored_sha in sorted(plan.pruned))
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO _history_scrub_sha_map (stored_sha, new_sha) VALUES (%s, %s)",
            map_rows,
        )
    for _source, query in _SCALAR_UPDATE_QUERIES:
        connection.execute(query, (project_id,))
    connection.execute(
        """
        UPDATE tasks AS t
        SET commits = remapped.commits
        FROM (
            SELECT source.id AS task_id,
                   COALESCE(
                       jsonb_agg(
                           to_jsonb(COALESCE(m.new_sha, part.sha))
                           ORDER BY e.ordinality, part.part_ordinality
                       ) FILTER (
                           WHERE m.stored_sha IS NULL OR m.new_sha IS NOT NULL
                       ),
                       '[]'::jsonb
                   ) AS commits
            FROM tasks AS source
            CROSS JOIN LATERAL jsonb_array_elements_text(source.commits)
                WITH ORDINALITY AS e(sha, ordinality)
            CROSS JOIN LATERAL regexp_split_to_table(
                e.sha,
                '[[:space:]]*,[[:space:]]*'
            ) WITH ORDINALITY AS part(sha, part_ordinality)
            LEFT JOIN _history_scrub_sha_map AS m ON m.stored_sha = part.sha
            WHERE source.project_id = %s AND source.commits IS NOT NULL
            GROUP BY source.id
        ) AS remapped
        WHERE t.id = remapped.task_id
          AND t.commits IS DISTINCT FROM remapped.commits
        """,
        (project_id,),
    )


def _is_sensitive_history_path(path: str) -> bool:
    parts = Path(path).parts
    return len(parts) >= 2 and parts[-2] == ".gobby" and parts[-1] in _SENSITIVE_PATHS


def _run_git(repo: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise HistoryScrubError(detail)
    return result.stdout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True, type=UUID)
    parser.add_argument("--expected-project-name", required=True)
    parser.add_argument("--scrubbed-repo", required=True, type=Path)
    parser.add_argument("--commit-map", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-project-id", type=UUID)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply and args.confirm_project_id != args.project_id:
        print("error: --apply requires --confirm-project-id matching --project-id", file=sys.stderr)
        return 2
    commit_map_path = args.commit_map or args.scrubbed_repo / "filter-repo" / "commit-map"
    try:
        mapping = parse_commit_map(commit_map_path)
        bootstrap = load_bootstrap(
            str(get_gobby_home() / "bootstrap.yaml"), resolve_database_url=True
        )
        if bootstrap.database_url is None:
            raise HistoryScrubError("bootstrap database_url is missing")
        with psycopg.connect(bootstrap.database_url, row_factory=dict_row) as connection:
            result = run_with_connection(
                connection,
                project_id=args.project_id,
                expected_project_name=args.expected_project_name,
                mapping=mapping,
                scrubbed_repo=args.scrubbed_repo,
                apply=args.apply,
            )
    except (HistoryScrubError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "applied" if result.applied else "dry-run"
    print(f"mode={mode}")
    print(f"project_id={result.project_id}")
    print(f"project_name={result.project_name}")
    print(f"distinct_stored_shas={result.distinct_stored_shas}")
    print(f"changed_references={result.changed_references}")
    print(f"unmatched_references={result.unmatched_references}")
    print(f"pruned_references={result.pruned_references}")
    print(f"normalized_task_commit_entries={result.normalized_task_commit_entries}")
    for source, count in sorted(result.reference_counts.items()):
        print(f"references[{source}]={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
