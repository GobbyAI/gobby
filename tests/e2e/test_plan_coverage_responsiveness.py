"""E2E regression for #19878: coverage regeneration must not stall the daemon.

On 2026-08-08 an in-daemon plan-coverage regeneration (triggered by
`gobby-plans:update_plan_hash`) spent minutes in pure-Python CPU on a worker
thread. The GIL convoy starved the event loop: `/api/health` flapped and timed
out, MCP calls hit their 30s timeout, and background loops froze until the run
finished. This test seeds a project large enough that the pre-fix loader wedged
the HTTP plane, then proves concurrent health requests keep answering while
`update_plan_hash` regenerates coverage.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.storage.hub.protocol import HubDatabase
from tests.e2e.conftest import DaemonInstance, daemon_auth_headers

pytestmark = pytest.mark.e2e

PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
PLAN_ID = "wedge-plan"
SEEDED_TASKS = 10_000
HEALTH_TIMEOUT_SECONDS = 2.0

PLAN_TEXT = """> **Plan ID:** wedge-plan

## A1 Work [category: code]
`kind: deliverable`

Implement the covered behavior.

**Acceptance:**
- A1.1 - Behavior exists. file: `src/behavior.py`
"""


def _register_project(client: httpx.Client, repo_path: Path) -> None:
    response = client.post(
        "/api/admin/test/register-project",
        json={
            "project_id": PROJECT_ID,
            "name": "E2E Test Project",
            "repo_path": str(repo_path),
        },
    )
    assert response.is_success, response.text


def _seed_tasks(postgres_db: HubDatabase) -> str:
    """Bulk-insert a flat task tree big enough to wedge the pre-fix loader.

    Returns the root task ref covering the plan.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[tuple[Any, ...]] = []
    for index in range(1, SEEDED_TASKS + 1):
        created = base + timedelta(seconds=index)
        rows.append(
            (
                str(uuid.uuid4()),
                PROJECT_ID,
                f"Seeded task {index}",
                index % 5,
                "task",
                index,
                str(index),
                None,
                "Seeded task state remains queryable.",
                created,
                created,
            )
        )
    root_id = str(uuid.uuid4())
    root_created = base + timedelta(seconds=SEEDED_TASKS + 1)
    rows.append(
        (
            root_id,
            PROJECT_ID,
            "Wedge plan root",
            1,
            "epic",
            SEEDED_TASKS + 1,
            str(SEEDED_TASKS + 1),
            None,
            None,
            root_created,
            root_created,
        )
    )
    leaf_created = base + timedelta(seconds=SEEDED_TASKS + 2)
    rows.append(
        (
            str(uuid.uuid4()),
            PROJECT_ID,
            "Wedge plan leaf",
            1,
            "task",
            SEEDED_TASKS + 2,
            f"{SEEDED_TASKS + 1}.{SEEDED_TASKS + 2}",
            root_id,
            "Touches src/behavior.py.",
            leaf_created,
            leaf_created,
        )
    )
    with postgres_db.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO tasks (
                id, project_id, title, priority, task_type, seq_num,
                path_cache, parent_task_id, validation_criteria,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
        conn.execute(
            "UPDATE tasks SET labels = %s::jsonb WHERE project_id = %s AND seq_num = %s",
            (f'["covers:{PLAN_ID}:A1:A1.1"]', PROJECT_ID, SEEDED_TASKS + 2),
        )
    return f"#{SEEDED_TASKS + 1}"


def _call_tool(
    client: httpx.Client,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        "/api/mcp/tools/call",
        json={
            "server_name": "gobby-plans",
            "tool_name": tool_name,
            "arguments": arguments,
        },
    )
    assert response.is_success, response.text
    payload = response.json()
    result = payload.get("result", payload)
    if isinstance(result, dict) and "result" in result and "ok" not in result:
        result = result["result"]
    assert isinstance(result, dict), payload
    return result


@pytest.mark.timeout(600)
def test_http_plane_responsive_during_coverage_regeneration(
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
) -> None:
    gobby_home = daemon_instance.gobby_home
    headers = daemon_auth_headers(gobby_home)

    with httpx.Client(
        base_url=daemon_instance.http_url, headers=headers, timeout=300.0
    ) as slow_client:
        _register_project(slow_client, daemon_instance.project_dir)
        root_ref = _seed_tasks(postgres_db)

        plan_dir = daemon_instance.project_dir / ".gobby" / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plan_dir / f"{PLAN_ID}.md"
        plan_file.write_text(PLAN_TEXT, encoding="utf-8")

        created = _call_tool(
            slow_client,
            "create_plan",
            {
                "plan_id": PLAN_ID,
                "plan_path": str(plan_file.relative_to(daemon_instance.project_dir)),
                "plan_kind": "implementation",
                "root_task_ref": root_ref,
                "project": PROJECT_ID,
            },
        )
        assert created.get("ok") is True, created

        # Change the plan bytes so update_plan_hash regenerates coverage.
        plan_file.write_text(PLAN_TEXT + "\nAmended for hash change.\n", encoding="utf-8")

        update_result: dict[str, Any] = {}
        update_error: list[BaseException] = []
        update_finished = threading.Event()

        def _update() -> None:
            try:
                update_result.update(
                    _call_tool(
                        slow_client,
                        "update_plan_hash",
                        {"plan_id": PLAN_ID, "project": PROJECT_ID},
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                update_error.append(exc)
            finally:
                update_finished.set()

        update_thread = threading.Thread(target=_update, name="update-plan-hash")

        health_failures: list[str] = []
        health_latencies: list[float] = []
        with httpx.Client(
            base_url=daemon_instance.http_url,
            headers=headers,
            timeout=HEALTH_TIMEOUT_SECONDS,
        ) as health_client:
            update_thread.start()
            try:
                while not update_finished.is_set():
                    started = time.monotonic()
                    try:
                        response = health_client.get("/api/health")
                        elapsed = time.monotonic() - started
                        if response.status_code == 200:
                            health_latencies.append(elapsed)
                        else:
                            health_failures.append(f"status={response.status_code}")
                    except httpx.HTTPError as exc:
                        elapsed = time.monotonic() - started
                        health_failures.append(f"{type(exc).__name__} after {elapsed:.2f}s")
                    update_finished.wait(timeout=0.2)
            finally:
                update_thread.join(timeout=300.0)
            assert not update_thread.is_alive(), "update_plan_hash never returned"

            # Guarantee the health plane was actually sampled even if the
            # regeneration finished quickly.
            while len(health_latencies) + len(health_failures) < 3:
                started = time.monotonic()
                response = health_client.get("/api/health")
                assert response.status_code == 200
                health_latencies.append(time.monotonic() - started)

        if health_latencies:
            print(
                f"health polls={len(health_latencies)} "
                f"max={max(health_latencies):.3f}s "
                f"mean={sum(health_latencies) / len(health_latencies):.3f}s"
            )

    assert not update_error, f"update_plan_hash raised: {update_error[0]!r}"
    assert update_result.get("ok") is True, update_result
    assert not health_failures, (
        f"{len(health_failures)} health request(s) failed while coverage "
        f"regeneration ran ({len(health_latencies)} succeeded): {health_failures[:5]}"
    )
