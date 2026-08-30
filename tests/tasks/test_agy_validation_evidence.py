"""AGY validation-evidence parity with the five incumbent providers (plan 4.2.9–4.2.10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.sessions.transcripts import PARSER_REGISTRY
from gobby.storage.session_models import Session
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import derive_transcript_evidence

pytestmark = pytest.mark.unit

BASE_TIME = datetime(2026, 8, 22, 8, 21, 24, tzinfo=UTC)
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"
_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "provider_contracts"
    / "agy"
    / "transcript-manifest.json"
)
_PYTEST_CMD = "uv run pytest tests/agy/test_example.py"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


def _session(transcript_path: Path) -> Session:
    return Session(
        id="00000000-0000-0000-0000-0000000000a1",
        external_id="transcript-evidence-agy-parser",
        machine_id=LOCAL_MACHINE_ID,
        source="agy",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript_path),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def _created(offset_seconds: int) -> str:
    return BASE_TIME.replace(second=min(59, offset_seconds)).isoformat().replace("+00:00", "Z")


def _planner(step_index: int, command: str, *, created_at: str) -> dict[str, Any]:
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": created_at,
        "tool_calls": [
            {
                "name": "run_command",
                "args": {"CommandLine": command, "Cwd": "/workspace"},
            }
        ],
    }


def _generic(
    step_index: int,
    content: str,
    *,
    created_at: str,
    status: str = "DONE",
) -> dict[str, Any]:
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "GENERIC",
        "status": status,
        "created_at": created_at,
        "content": content,
    }


def _live_contents() -> tuple[str, str]:
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    zero = manifest["zero_exit_run_command"]["transcript_full"][1]["content"]
    nonzero = manifest["nonzero_exit_run_command"]["transcript_full"][1]["content"]
    assert isinstance(zero, str)
    assert isinstance(nonzero, str)
    return zero, nonzero


async def _evidence(path: Path) -> Any:
    assert "agy" in PARSER_REGISTRY
    return await derive_transcript_evidence(
        _session(path),
        None,
        default_validation_detection_config(),
        set(),
        str(path.parent),
    )


def _gate(evidence: Any) -> Any:
    return evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
    )


@pytest.mark.asyncio
async def test_agy_success_failure_and_recovery_use_live_generic_payloads(
    tmp_path: Path,
) -> None:
    zero, nonzero = _live_contents()
    transcript = tmp_path / "transcript_full.jsonl"
    _write_jsonl(
        transcript,
        [
            _planner(2, _PYTEST_CMD, created_at=_created(0)),
            _generic(3, nonzero, created_at=_created(1)),
        ],
    )
    failed = await _evidence(transcript)
    assert [(run.outcome, run.exit_code) for run in failed.validation_runs] == [("failure", 7)]
    assert _gate(failed).status == "failed"

    _write_jsonl(
        transcript,
        [
            _planner(2, _PYTEST_CMD, created_at=_created(0)),
            _generic(3, nonzero, created_at=_created(1)),
            _planner(4, _PYTEST_CMD, created_at=_created(2)),
            _generic(5, zero, created_at=_created(3)),
        ],
    )
    recovered = await _evidence(transcript)
    assert [(run.outcome, run.exit_code) for run in recovered.validation_runs] == [
        ("failure", 7),
        ("success", 0),
    ]
    assert _gate(recovered).status == "passed"


@pytest.mark.asyncio
async def test_agy_nonterminal_unstructured_contradictory_and_provenance_free(
    tmp_path: Path,
) -> None:
    zero, _nonzero = _live_contents()
    transcript = tmp_path / "transcript_full.jsonl"

    _write_jsonl(
        transcript,
        [
            _planner(2, _PYTEST_CMD, created_at=_created(0)),
            _generic(3, "still running", created_at=_created(1), status="RUNNING"),
        ],
    )
    nonterminal = await _evidence(transcript)
    assert [run.outcome for run in nonterminal.validation_runs] == ["unknown"]
    assert _gate(nonterminal).status == "failed"

    _write_jsonl(
        transcript,
        [
            _planner(2, _PYTEST_CMD, created_at=_created(0)),
            _generic(3, "Created At: now\nOutput:\nno exit sentence", created_at=_created(1)),
        ],
    )
    unstructured = await _evidence(transcript)
    assert [run.outcome for run in unstructured.validation_runs] == ["unknown"]
    assert unstructured.degraded_capabilities
    assert _gate(unstructured).status == "failed"

    _write_jsonl(
        transcript,
        [
            _planner(2, _PYTEST_CMD, created_at=_created(0)),
            {
                **_generic(3, zero, created_at=_created(1), status="ERROR"),
            },
        ],
    )
    contradictory = await _evidence(transcript)
    assert [run.outcome for run in contradictory.validation_runs] == ["unknown"]
    assert _gate(contradictory).status == "failed"

    _write_jsonl(
        transcript,
        [
            _planner(2, _PYTEST_CMD, created_at=_created(0)),
            _generic(3, zero, created_at=_created(1)),
        ],
    )
    provenance_free = await _evidence(transcript)
    assert [(run.outcome, run.exit_code) for run in provenance_free.validation_runs] == [
        ("success", 0)
    ]
    assert provenance_free.degraded_capabilities == ()
    assert _gate(provenance_free).status == "passed"
    end_raw = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
    assert "outcome_provenance" not in end_raw
