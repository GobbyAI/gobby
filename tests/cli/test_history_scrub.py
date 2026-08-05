from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from gobby.cli.history_scrub import (
    HistoryScrubError,
    ShaReference,
    parse_commit_map,
    plan_sha_rewrites,
    run_with_connection,
    verify_scrubbed_repository,
)
from gobby.storage.hub.postgres import PostgresHubDatabase


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def two_commit_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Gobby Tests")
    old_sha = _commit(repo, "first.txt", "first\n")
    new_sha = _commit(repo, "second.txt", "second\n")
    return repo, old_sha, new_sha


def test_plan_sha_rewrites_classifies_pruned_references(tmp_path: Path) -> None:
    old_sha = "1" * 40
    commit_map = tmp_path / "commit-map"
    commit_map.write_text(f"old new\n{old_sha} {'0' * 40}\n", encoding="utf-8")

    mapping = parse_commit_map(commit_map)
    stored_sha = old_sha[:8]
    plan = plan_sha_rewrites([ShaReference("tasks.commits", "task:1", stored_sha)], mapping)

    assert plan.pruned == frozenset({stored_sha})
    assert plan.replacements == {}
    assert plan.unmatched == frozenset()


def test_plan_sha_rewrites_preserves_unmatched_reference() -> None:
    plan = plan_sha_rewrites([ShaReference("tasks.closed_commit_sha", "task", "deadbeef")], {})

    assert plan.unmatched == frozenset({"deadbeef"})
    assert plan.replacements == {}
    assert plan.pruned == frozenset()


def test_plan_sha_rewrites_rejects_pruned_checkpoint_reference() -> None:
    old_sha = "1" * 40

    with pytest.raises(HistoryScrubError, match="required checkpoint evidence"):
        plan_sha_rewrites(
            [ShaReference("checkpoints.commit_sha", "checkpoint", old_sha[:8])],
            {old_sha: "0" * 40},
        )


def test_resolve_replacements_rejects_ambiguous_prefix() -> None:
    mapping = {
        "a" * 39 + "1": "b" * 40,
        "a" * 39 + "2": "c" * 40,
    }

    with pytest.raises(HistoryScrubError, match="ambiguous commit-map prefix"):
        plan_sha_rewrites([ShaReference("tasks.closed_commit_sha", "task", "aaaa")], mapping)


def test_verify_scrubbed_repository_rejects_nested_state_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Gobby Tests")
    sensitive = repo / "web" / ".gobby" / "tasks.jsonl"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "sensitive")

    with pytest.raises(HistoryScrubError, match="protected paths"):
        verify_scrubbed_repository(repo, [])


def test_run_with_connection_remaps_all_authoritative_sha_fields(
    postgres_db: PostgresHubDatabase,
    two_commit_repo: tuple[Path, str, str],
) -> None:
    repo, old_sha, new_sha = two_commit_repo
    project_id = uuid4()
    task_id = uuid4()
    pruned_task_id = uuid4()
    checkpoint_id = uuid4()
    short_old_sha = old_sha[:8]
    pruned_sha = "d" * 40
    short_pruned_sha = pruned_sha[:8]
    unmatched_sha = "feedface"

    with psycopg.connect(postgres_db.conninfo, row_factory=dict_row) as connection:
        connection.execute(
            """
            CREATE TEMP TABLE projects (
                id uuid PRIMARY KEY,
                name text NOT NULL
            );
            CREATE TEMP TABLE tasks (
                id uuid PRIMARY KEY,
                project_id uuid NOT NULL,
                closed_commit_sha text,
                commits jsonb
            );
            CREATE TEMP TABLE checkpoints (
                id uuid PRIMARY KEY,
                task_id uuid NOT NULL,
                commit_sha text NOT NULL,
                parent_sha text NOT NULL
            );
            CREATE TEMP TABLE task_artifacts (
                task_id uuid PRIMARY KEY,
                base_commit_sha text
            );
            CREATE TEMP TABLE task_delivery_campaigns (
                task_id uuid PRIMARY KEY,
                merge_sha text
            );
            CREATE TEMP TABLE task_stage_states (
                task_id uuid NOT NULL,
                stage_name text NOT NULL,
                completed_commit_sha text,
                PRIMARY KEY (task_id, stage_name)
            )
            """
        )
        connection.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, "gobby"),
        )
        connection.execute(
            """
            INSERT INTO tasks (id, project_id, closed_commit_sha, commits)
            VALUES (%s, %s, %s, jsonb_build_array(%s::text, %s::text))
            """,
            (task_id, project_id, short_old_sha, short_old_sha, old_sha),
        )
        connection.execute(
            """
            INSERT INTO tasks (id, project_id, closed_commit_sha, commits)
            VALUES (%s, %s, %s, jsonb_build_array(%s::text))
            """,
            (
                pruned_task_id,
                project_id,
                short_pruned_sha,
                f"{unmatched_sha},{short_pruned_sha}",
            ),
        )
        connection.execute(
            """
            INSERT INTO checkpoints (id, task_id, commit_sha, parent_sha)
            VALUES (%s, %s, %s, %s)
            """,
            (checkpoint_id, task_id, short_old_sha, old_sha),
        )
        connection.execute(
            "INSERT INTO task_artifacts (task_id, base_commit_sha) VALUES (%s, %s)",
            (task_id, short_old_sha),
        )
        connection.execute(
            "INSERT INTO task_artifacts (task_id, base_commit_sha) VALUES (%s, %s)",
            (pruned_task_id, short_pruned_sha),
        )
        connection.execute(
            "INSERT INTO task_delivery_campaigns (task_id, merge_sha) VALUES (%s, %s)",
            (task_id, old_sha),
        )
        connection.execute(
            """
            INSERT INTO task_stage_states (task_id, stage_name, completed_commit_sha)
            VALUES (%s, 'implementation', %s)
            """,
            (task_id, short_old_sha),
        )
        connection.commit()

        result = run_with_connection(
            connection,
            project_id=project_id,
            expected_project_name="gobby",
            mapping={old_sha: new_sha, pruned_sha: "0" * 40},
            scrubbed_repo=repo,
            apply=True,
        )

        assert result.applied is True
        assert result.distinct_stored_shas == 4
        assert result.changed_references == 11
        assert result.unmatched_references == 1
        assert result.pruned_references == 3
        assert result.normalized_task_commit_entries == 1
        assert result.reference_counts == {
            "checkpoints.commit_sha": 1,
            "checkpoints.parent_sha": 1,
            "task_artifacts.base_commit_sha": 2,
            "task_delivery_campaigns.merge_sha": 1,
            "task_stage_states.completed_commit_sha": 1,
            "tasks.closed_commit_sha": 2,
            "tasks.commits": 4,
        }
        task_row = connection.execute(
            "SELECT closed_commit_sha FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        assert task_row is not None
        assert task_row["closed_commit_sha"] == new_sha
        commits_row = connection.execute(
            "SELECT commits FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        assert commits_row is not None
        assert commits_row["commits"] == [new_sha, new_sha]
        pruned_task_row = connection.execute(
            "SELECT closed_commit_sha, commits FROM tasks WHERE id = %s",
            (pruned_task_id,),
        ).fetchone()
        assert pruned_task_row is not None
        assert pruned_task_row == {
            "closed_commit_sha": None,
            "commits": [unmatched_sha],
        }
        assert connection.execute(
            "SELECT commit_sha, parent_sha FROM checkpoints WHERE id = %s",
            (checkpoint_id,),
        ).fetchone() == {"commit_sha": new_sha, "parent_sha": new_sha}
        artifact_row = connection.execute(
            "SELECT base_commit_sha FROM task_artifacts WHERE task_id = %s", (task_id,)
        ).fetchone()
        assert artifact_row is not None
        assert artifact_row["base_commit_sha"] == new_sha
        pruned_artifact_row = connection.execute(
            "SELECT base_commit_sha FROM task_artifacts WHERE task_id = %s",
            (pruned_task_id,),
        ).fetchone()
        assert pruned_artifact_row is not None
        assert pruned_artifact_row["base_commit_sha"] is None
        delivery_row = connection.execute(
            "SELECT merge_sha FROM task_delivery_campaigns WHERE task_id = %s", (task_id,)
        ).fetchone()
        assert delivery_row is not None
        assert delivery_row["merge_sha"] == new_sha
        stage_row = connection.execute(
            """
            SELECT completed_commit_sha
            FROM task_stage_states
            WHERE task_id = %s AND stage_name = 'implementation'
            """,
            (task_id,),
        ).fetchone()
        assert stage_row is not None
        assert stage_row["completed_commit_sha"] == new_sha
