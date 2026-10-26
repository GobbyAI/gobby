"""Inventory and drift checks for the generated Python-to-Rust parity ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import pytest
from fastapi import FastAPI
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from scripts.generate_parity_ledger import (
    LEDGER_PATH,
    ParityStatus,
    cli_verbs,
    collect_surfaces,
    committed_ledger,
    http_surfaces,
    load_statuses,
    render_ledger,
)
from scripts.parity_waivers import (
    DIFF,
    NATIVE_EXTENSION,
    PASSED,
    WAIVED,
    Waiver,
    WaiverError,
    load_waivers,
)
from tests.contracts.http_corpus import CorpusError, classify_replay

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def surfaces() -> set[str]:
    return collect_surfaces()


def test_committed_ledger_matches_current_public_inventory(surfaces: set[str]) -> None:
    """Any inventory, mapping, waiver or generator drift must update the committed document."""
    assert committed_ledger(surfaces) == LEDGER_PATH.read_bytes()


def test_inventory_contains_all_four_protocols_and_terminal_flip(surfaces: set[str]) -> None:
    assert {
        "HTTP POST /api/auth/login",
        "HTTP * /mcp",
        "WS transport /ws",
        "WS message chat_message",
        "WS message terminal_create",
        "MCP HTTP gobby:call_tool",
        "MCP stdio gobby:init_project",
        "MCP gobby-tasks:get_task",
        "MCP gobby-workspaces:read_pane",
        "CLI gobby status",
        "CLI gobby tasks list",
        "Gate native terminal default flip #21357",
    } <= surfaces


def test_route_inventory_detects_new_methods_and_mounts() -> None:
    def endpoint() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    child = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    child.add_api_route("/fresh", endpoint, methods=["GET", "POST"])
    app.mount("/nested", child)
    assert http_surfaces(app.routes) == {"HTTP GET /nested/fresh", "HTTP POST /nested/fresh"}


def test_route_inventory_keeps_unrestricted_asgi_endpoints() -> None:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    route = Route("/mcp", endpoint=app)
    assert http_surfaces([route]) == {"HTTP * /mcp"}


def test_cli_inventory_includes_nested_verbs_and_aliases_without_running_callbacks() -> None:
    def callback() -> None:
        raise AssertionError("Inventory must not invoke CLI callbacks")

    root = click.Group("gobby", callback=callback)
    group = click.Group("nested", callback=callback)
    verb = click.Command("fresh", callback=callback)
    group.add_command(verb)
    group.add_command(verb, "alias")
    root.add_command(group)
    assert cli_verbs(root) == {"gobby nested", "gobby nested fresh", "gobby nested alias"}


def test_rendering_orders_rows_and_exposes_unclassified_and_recorded_drops() -> None:
    statuses = {
        "CLI gobby ready": ParityStatus("delegated"),
        "HTTP GET /old": ParityStatus("dropped by decision #123"),
        "MCP HTTP gobby:call_tool": ParityStatus("proxied"),
    }
    rendered = render_ledger(
        {"WS message new", "CLI gobby ready", "MCP HTTP gobby:call_tool"}, statuses
    )
    assert rendered.endswith(
        b"| `CLI gobby ready` | delegated |  |\n"
        b"| `HTTP GET /old` | dropped by decision #123 |  |\n"
        b"| `MCP HTTP gobby:call_tool` | proxied |  |\n"
        b"| `WS message new` | **unclassified** |  |\n"
    )
    assert b"Unclassified: 1." in rendered


def test_absent_mapping_key_fails_instead_of_silently_hiding_a_rename() -> None:
    with pytest.raises(ValueError, match="Mapping references absent surfaces: HTTP GET /old"):
        render_ledger({"HTTP GET /new"}, {"HTTP GET /old": ParityStatus("delegated")})


@pytest.mark.parametrize("status", ["native", "unclassified", "dropped", "dropped by decision", 1])
def test_mapping_rejects_invalid_status_or_unrecorded_drop(tmp_path: Path, status: object) -> None:
    mapping = tmp_path / "statuses.json"
    mapping.write_text(json.dumps({"HTTP GET /health": status}))
    with pytest.raises(ValueError, match="Invalid parity status for HTTP GET /health"):
        load_statuses(tmp_path)


def test_mapping_accepts_each_documented_status(tmp_path: Path) -> None:
    mapping = tmp_path / "statuses.json"
    mapping.write_text(
        '{"HTTP GET /a": "proxied", "HTTP GET /b": "delegated", '
        '"HTTP GET /c": "dropped by decision #123"}'
    )
    assert load_statuses(tmp_path) == {
        "HTTP GET /a": ParityStatus("proxied"),
        "HTTP GET /b": ParityStatus("delegated"),
        "HTTP GET /c": ParityStatus("dropped by decision #123"),
    }


def test_mapping_merges_all_shards_in_stable_order(tmp_path: Path) -> None:
    (tmp_path / "z.json").write_text('{"HTTP GET /z": "delegated"}')
    (tmp_path / "a.json").write_text('{"HTTP GET /b": "proxied", "HTTP GET /a": "delegated"}')
    assert list(load_statuses(tmp_path).items()) == [
        ("HTTP GET /a", ParityStatus("delegated")),
        ("HTTP GET /b", ParityStatus("proxied")),
        ("HTTP GET /z", ParityStatus("delegated")),
    ]


def test_duplicate_surface_names_both_shards(tmp_path: Path) -> None:
    (tmp_path / "tasks.json").write_text('{"HTTP GET /api/tasks": "proxied"}')
    (tmp_path / "remainder.json").write_text('{"HTTP GET /api/tasks": "delegated"}')
    with pytest.raises(ValueError, match="Duplicate parity surface") as error:
        load_statuses(tmp_path)
    assert "tasks.json" in str(error.value)
    assert "remainder.json" in str(error.value)
    assert "HTTP GET /api/tasks" in str(error.value)


def test_missing_shards_fail_instead_of_unclassifying_everything(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No parity status shards"):
        load_statuses(tmp_path)


def test_object_and_string_entries_render_notes_without_changing_statuses(tmp_path: Path) -> None:
    entries = {
        "HTTP GET /a": {"status": "delegated", "note": "crates/gdaemon | route\r\ntest"},
        "HTTP GET /b": "proxied",
        "HTTP GET /removed": {
            "status": "dropped by decision #123",
            "note": "Recorded decision #123",
        },
    }
    (tmp_path / "statuses.json").write_text(json.dumps(entries))

    statuses = load_statuses(tmp_path)
    rendered = render_ledger({"HTTP GET /a", "HTTP GET /b"}, statuses)

    assert statuses["HTTP GET /a"] == ParityStatus("delegated", "crates/gdaemon | route\r\ntest")
    assert statuses["HTTP GET /b"] == ParityStatus("proxied")
    assert b"| Public surface | Rust status | Notes |" in rendered
    assert rendered.endswith(
        b"| `HTTP GET /a` | delegated | crates/gdaemon \\| route<br>test |\n"
        b"| `HTTP GET /b` | proxied |  |\n"
        b"| `HTTP GET /removed` | dropped by decision #123 | Recorded decision #123 |\n"
    )
    assert b"Unclassified: 0." in rendered


def test_note_backslashes_cannot_unescape_a_table_separator() -> None:
    rendered = render_ledger(
        {"HTTP GET /a"}, {"HTTP GET /a": ParityStatus("proxied", "Rust \\| work")}
    )

    assert rendered.endswith(b"| `HTTP GET /a` | proxied | Rust \\\\\\| work |\n")


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"status": "proxied"},
        {"note": "evidence"},
        {"status": "delegated", "note": 123},
        {"status": "delegated", "note": None},
        {"status": "delegated", "note": "evidence", "extra": "typo"},
    ],
)
def test_mapping_rejects_malformed_objects(tmp_path: Path, entry: object) -> None:
    (tmp_path / "statuses.json").write_text(json.dumps({"HTTP GET /a": entry}))

    with pytest.raises(ValueError, match="Invalid parity entry for HTTP GET /a in statuses.json"):
        load_statuses(tmp_path)


@pytest.mark.parametrize("status", [None, 123, "unknown", "dropped by decision #0"])
def test_object_entries_use_the_same_status_validation(tmp_path: Path, status: object) -> None:
    (tmp_path / "statuses.json").write_text(
        json.dumps({"HTTP GET /a": {"status": status, "note": "evidence"}})
    )

    with pytest.raises(ValueError, match="Invalid parity status for HTTP GET /a"):
        load_statuses(tmp_path)


def test_raw_asgi_mount_fails_instead_of_disappearing() -> None:
    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("Inventory must not invoke the ASGI application")

    with pytest.raises(ValueError, match="Cannot enumerate raw ASGI mount: /proxy/raw"):
        http_surfaces([Mount("/raw", app=endpoint)], prefix="/proxy")


def test_exit_rule_points_at_naming_switch_instead_of_gobby_backend() -> None:
    """S3.4 merged into S3.2 (#21573): Python is removed, nothing is renamed gobby-backend."""
    header = render_ledger(set(), {})
    assert b"#21573" in header
    assert b"#21574 Retire gobby-backend" not in header
    assert b"gobby-backend" not in header


def test_every_current_surface_is_classified(surfaces: set[str]) -> None:
    """Regenerating the ledger must not launder unclassified rows past the drift check."""
    assert sorted(surfaces - load_statuses().keys()) == []


@pytest.mark.parametrize(
    ("surface", "handler"),
    [
        ("HTTP POST /api/nodes/runs/code-index-warning", "code_index_preflight_warning"),
        ("HTTP POST /api/nodes/runs/prompt-delivery-failure", "prompt_delivery_failure"),
    ],
)
def test_node_run_outcome_routes_match_sibling_node_routes(surface: str, handler: str) -> None:
    statuses = load_statuses()
    siblings = {
        name: entry
        for name, entry in statuses.items()
        if name.startswith("HTTP POST /api/nodes/") and "/api/nodes/runs/" not in name
    }
    assert siblings, "sibling node routes disappeared from the mapping"
    entry = statuses.get(surface)
    assert entry is not None, f"{surface} is unclassified"
    assert {s.status for s in siblings.values()} == {entry.status}
    assert all("#24151" in s.note for s in siblings.values())
    assert "#24151" in entry.note
    assert f"src/gobby/servers/routes/node_runs.py::create_node_runs_router.{handler}" in entry.note


CRON_PATCH = "HTTP PATCH /api/cron/jobs/{job_id}"
CRON_LIST = "HTTP GET /api/cron/jobs"
CRON_OUTPUT = "HTTP GET /api/cron/runs/{run_id}/output"
INVENTORY = {CRON_PATCH, CRON_LIST}
ADDITIVE = {CRON_OUTPUT}
VALIDATION_ID = "cron.patch-job.schedule-validation"
VALIDATION_DECISION = ".gobby/plans/gdaemon-cron-family.md#3.2"
VALIDATION_BEHAVIOUR = "An invalid schedule on PATCH returns 400 instead of an internal error."
VALIDATION_REPLACEMENT = 'Native 400 with {"detail": "Invalid cron schedule"}.'
OUTPUT_ID = "cron.get-run-output.native-extension"
OUTPUT_DECISION = ".gobby/plans/gdaemon-cron-family.md#3.1a"
PYTHON_RESPONSE = {"status": 500, "headers": {}, "body": {"detail": "internal"}}
NATIVE_RESPONSE = {"status": 400, "headers": {}, "body": {"detail": "Invalid cron schedule"}}


def _entry(**changes: str) -> dict[str, str]:
    entry = {
        "id": VALIDATION_ID,
        "surface": CRON_PATCH,
        "behaviour": VALIDATION_BEHAVIOUR,
        "decision": VALIDATION_DECISION,
        "replacement": VALIDATION_REPLACEMENT,
    }
    entry.update(changes)
    return entry


def _manifest(tmp_path: Path, *entries: dict[str, str]) -> Path:
    path = tmp_path / "parity-waivers.json"
    path.write_text(json.dumps({"schema_version": 1, "waivers": list(entries)}))
    return path


def _waiver(**changes: str) -> Waiver:
    fields = {
        "id": VALIDATION_ID,
        "surface": CRON_PATCH,
        "behaviour": VALIDATION_BEHAVIOUR,
        "decision": VALIDATION_DECISION,
        "replacement": VALIDATION_REPLACEMENT,
    }
    fields.update(changes)
    return Waiver(**fields)


def _row(rendered: bytes, surface: str) -> str:
    prefix = f"| `{surface}` |"
    rows = [line for line in rendered.decode().splitlines() if line.startswith(prefix)]
    assert rows, f"no ledger row for {surface}"
    return rows[0]


@pytest.mark.parametrize("field", ["id", "surface", "behaviour", "decision", "replacement"])
def test_waiver_entry_requires_every_field(tmp_path: Path, field: str) -> None:
    entry = {key: value for key, value in _entry().items() if key != field}
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, entry), surfaces=INVENTORY)


@pytest.mark.parametrize("field", ["behaviour", "decision", "replacement"])
def test_waiver_entry_rejects_empty_text(tmp_path: Path, field: str) -> None:
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, _entry(**{field: ""})), surfaces=INVENTORY)


@pytest.mark.parametrize(
    "bad_id",
    [
        "Cron.patch-job.schedule-validation",
        "cron.patch-job",
        "cron.patch_job.schedule-validation",
        "cron.patch-job.schedule-validation.extra",
        "cron..schedule-validation",
        "cron.patch-job.-schedule-validation",
        "cron.patch-job.schedule-validation-",
    ],
)
def test_waiver_id_is_three_lowercase_kebab_segments(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, _entry(id=bad_id)), surfaces=INVENTORY)


def test_waiver_surface_must_be_an_exact_ledger_key(tmp_path: Path) -> None:
    entry = _entry(surface="HTTP PATCH /api/cron/jobs/{id}")
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, entry), surfaces=INVENTORY)


def test_duplicate_waiver_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, _entry(), _entry()), surfaces=INVENTORY)


@pytest.mark.parametrize("decision", ["#24952", "#1", ".gobby/plans/gdaemon-cron-family.md#3.1a"])
def test_decision_accepts_a_task_ref_or_plan_citation(tmp_path: Path, decision: str) -> None:
    loaded = load_waivers(_manifest(tmp_path, _entry(decision=decision)), surfaces=INVENTORY)
    assert loaded == {VALIDATION_ID: _waiver(decision=decision)}


@pytest.mark.parametrize(
    "decision",
    [
        "#0",
        "#12a",
        "#",
        "https://example.com/issues/24952",
        "see the cron plan",
        ".gobby/plans/gdaemon-cron-family.md",
        ".gobby/plans/gdaemon-cron-family.md#",
        "gdaemon-cron-family.md#3.2",
    ],
)
def test_decision_rejects_any_other_form(tmp_path: Path, decision: str) -> None:
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, _entry(decision=decision)), surfaces=INVENTORY)


def test_native_extension_for_an_exact_additive_surface_is_accepted(tmp_path: Path) -> None:
    entry = _entry(id=OUTPUT_ID, surface=CRON_OUTPUT, decision=OUTPUT_DECISION)
    loaded = load_waivers(
        _manifest(tmp_path, entry), surfaces=INVENTORY, additive_surfaces=ADDITIVE
    )
    assert set(loaded) == {OUTPUT_ID}
    assert loaded[OUTPUT_ID].surface == CRON_OUTPUT
    assert loaded[OUTPUT_ID].decision == OUTPUT_DECISION
    assert loaded[OUTPUT_ID].native_extension


def test_native_extension_surface_must_be_new_to_the_python_inventory(tmp_path: Path) -> None:
    entry = _entry(id="cron.patch-job.native-extension", surface=CRON_PATCH)
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, entry), surfaces=INVENTORY, additive_surfaces={CRON_PATCH})


def test_native_extension_must_name_a_declared_additive_surface(tmp_path: Path) -> None:
    entry = _entry(
        id=OUTPUT_ID, surface="HTTP GET /api/cron/runs/{run_id}/raw", decision=OUTPUT_DECISION
    )
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path, entry), surfaces=INVENTORY, additive_surfaces=ADDITIVE)


def test_additive_surface_without_an_entry_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WaiverError):
        load_waivers(_manifest(tmp_path), surfaces=INVENTORY, additive_surfaces=ADDITIVE)


def test_ledger_row_reports_a_waived_behaviour_with_its_citation_and_keeps_status() -> None:
    rendered = render_ledger(
        {CRON_PATCH}, {CRON_PATCH: ParityStatus("proxied")}, {VALIDATION_ID: _waiver()}
    )
    row = _row(rendered, CRON_PATCH)
    assert row.startswith(f"| `{CRON_PATCH}` | proxied |")
    assert WAIVED in row
    assert VALIDATION_ID in row
    assert VALIDATION_DECISION in row


def test_ledger_reports_a_native_extension_with_its_citation_and_no_python_status() -> None:
    waiver = _waiver(id=OUTPUT_ID, surface=CRON_OUTPUT, decision=OUTPUT_DECISION)
    rendered = render_ledger(set(), {}, {OUTPUT_ID: waiver})
    row = _row(rendered, CRON_OUTPUT)
    assert row.startswith(f"| `{CRON_OUTPUT}` | {NATIVE_EXTENSION} |")
    assert OUTPUT_DECISION in row
    for python_evidence in ("proxied", "delegated", PASSED, "Verified"):
        assert python_evidence not in row


def _case(**changes: object) -> dict[str, Any]:
    case: dict[str, Any] = {
        "name": "cron_patch_invalid_schedule",
        "family": "cron",
        "request": {
            "method": "PATCH",
            "path": "/api/cron/jobs/job-1",
            "query": {},
            "headers": {},
            "body": {"schedule": "not a cron"},
        },
        "response": PYTHON_RESPONSE,
        "mask": [],
    }
    case.update(changes)
    return case


def test_waived_case_reports_waived_with_its_citation() -> None:
    case = _case(waiver=VALIDATION_ID, native_response=NATIVE_RESPONSE)

    result = classify_replay(case, NATIVE_RESPONSE, {VALIDATION_ID: _waiver()})

    assert result.outcome == WAIVED
    assert result.waiver == VALIDATION_ID
    assert result.decision == VALIDATION_DECISION


def test_waived_case_that_still_matches_python_is_a_diff() -> None:
    case = _case(waiver=VALIDATION_ID, native_response=NATIVE_RESPONSE)

    result = classify_replay(case, PYTHON_RESPONSE, {VALIDATION_ID: _waiver()})

    assert result.outcome == DIFF


def test_unwaived_case_passes_only_on_the_python_outcome() -> None:
    case = _case()

    assert classify_replay(case, PYTHON_RESPONSE, {}).outcome == PASSED
    difference = classify_replay(case, NATIVE_RESPONSE, {})
    assert difference.outcome == DIFF
    assert difference.waiver is None


def test_waived_case_citing_an_unknown_waiver_is_rejected() -> None:
    case = _case(waiver="cron.nope.nope", native_response=NATIVE_RESPONSE)

    with pytest.raises(WaiverError):
        classify_replay(case, NATIVE_RESPONSE, {VALIDATION_ID: _waiver()})


def test_waived_case_must_assert_its_native_outcome() -> None:
    case = _case(waiver=VALIDATION_ID)

    with pytest.raises(CorpusError):
        classify_replay(case, NATIVE_RESPONSE, {VALIDATION_ID: _waiver()})


def test_native_extension_case_never_reports_python_success() -> None:
    native_response = {"status": 200, "headers": {}, "body": {"output": "hello"}}
    case: dict[str, Any] = {
        "name": "cron_run_output",
        "family": "cron",
        "request": {
            "method": "GET",
            "path": "/api/cron/runs/run-1/output",
            "query": {},
            "headers": {},
            "body": None,
        },
        "mask": [],
        "waiver": OUTPUT_ID,
        "native_response": native_response,
    }
    waiver = _waiver(id=OUTPUT_ID, surface=CRON_OUTPUT, decision=OUTPUT_DECISION)

    result = classify_replay(case, native_response, {OUTPUT_ID: waiver})

    assert result.outcome == NATIVE_EXTENSION
    assert result.decision == OUTPUT_DECISION
    assert result.outcome not in {PASSED, WAIVED}
