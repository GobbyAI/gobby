"""Tests for model-generated import-community labels (plan section 6.2)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    TextGenerationRequest,
    TextGenerationService,
)
from gobby.code_index.community_label_safety import sanitize_community_label
from gobby.code_index.community_labeler import (
    CommunityLabeler,
    CommunityLabelOutcome,
    GeneratedLabel,
)
from gobby.code_index.models import IndexedProject, IndexWriteMode, StoredCommunity
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexCommunityLabelConfig, CodeIndexConfig
from gobby.utils.machine_id import require_machine_id
from tests.code_index.conftest import PROJECT_ID

pytestmark = pytest.mark.unit

ASK_MEMBERS = [
    "src/gobby/ask/service.py",
    "src/gobby/ask/evidence.py",
    "src/gobby/ask/validation.py",
]
GENERATION_PROFILE = "feature_low"

type ScriptedResponse = dict[str, Any] | Exception


class ScriptedJSONAdapter:
    """JSON adapter that answers by the first scripted marker found in the prompt."""

    def __init__(self, responses: dict[str, ScriptedResponse]) -> None:
        self._responses = responses
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        raise AssertionError(f"community labels must use JSON generation: {request.caller}")

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        self.requests.append(request)
        for marker, response in self._responses.items():
            if marker in request.prompt:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError("prompt matched no scripted community")


def _labeler(adapters: dict[str, ScriptedJSONAdapter]) -> CommunityLabeler:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("label-model",),
            )
            for provider in adapters
        ]
    )
    service = TextGenerationService(registry, adapters)
    config = CodeIndexConfig(
        community_label=CodeIndexCommunityLabelConfig(
            candidates=[f"{provider}/label-model" for provider in adapters]
        )
    )
    return CommunityLabeler(service, config)


async def _run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def _insert_community(
    storage: CodeIndexStorage,
    community_id: int,
    members: Sequence[str],
    *,
    label_deterministic: str,
    label_candidates: Sequence[str],
) -> StoredCommunity:
    storage.upsert_project_stats(
        IndexedProject(id=PROJECT_ID, root_path="/tmp/gobby-community-label-tests"),
        mode=IndexWriteMode.OVERLAY,
    )
    storage.db.execute(
        """INSERT INTO code_communities (
               machine_id, project_id, community_id, member_count, members,
               representatives, internal_edges, cohesion, boundary, member_signature,
               label_deterministic, label, label_source, label_candidates
           )
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb, %s, %s, %s, 'deterministic', %s)""",
        (
            require_machine_id(),
            PROJECT_ID,
            community_id,
            len(members),
            list(members),
            list(members[:1]),
            len(members) - 1,
            0.5,
            f"{community_id:016x}",
            label_deterministic,
            label_deterministic,
            list(label_candidates),
        ),
    )
    queued = storage.get_unlabeled_communities(PROJECT_ID, limit=20)
    return next(community for community in queued if community.community_id == community_id)


def _label_row(storage: CodeIndexStorage, community_id: int) -> dict[str, Any]:
    row = storage.db.fetchone(
        """SELECT label, label_source, label_confidence, label_model, labeled_signature,
                  labeled_at, label_attempted_at
           FROM code_communities
           WHERE machine_id = %s AND project_id = %s AND community_id = %s""",
        (require_machine_id(), PROJECT_ID, community_id),
    )
    assert row is not None
    return dict(row)


def _queued_ids(storage: CodeIndexStorage, *, cooloff_seconds: int) -> list[int]:
    return sorted(
        community.community_id
        for community in storage.get_unlabeled_communities(
            PROJECT_ID, limit=20, failure_cooloff_seconds=cooloff_seconds
        )
    )


def _assert_attempt_only(storage: CodeIndexStorage, community: StoredCommunity) -> None:
    row = _label_row(storage, community.community_id)
    assert row["label"] == community.label_deterministic
    assert row["label_source"] == "deterministic"
    assert row["label_confidence"] is None
    assert row["label_model"] is None
    assert row["labeled_signature"] is None
    assert row["labeled_at"] is None
    assert row["label_attempted_at"] is not None


def test_sanitize_rejects_paths_fences_and_long_names() -> None:
    fenced = "```text\nAsk Evidence Admission\n```"
    assert sanitize_community_label(fenced, members=ASK_MEMBERS) == "Ask Evidence Admission"
    quoted = '"Ask Evidence Admission"'
    assert sanitize_community_label(quoted, members=ASK_MEMBERS) == "Ask Evidence Admission"

    assert sanitize_community_label("Ask evidence.py", members=ASK_MEMBERS) is None
    assert sanitize_community_label("scripts/code index", members=ASK_MEMBERS) == (
        "scripts/code index"
    )
    assert sanitize_community_label("scripts/code index", members=["scripts/code index"]) is None
    assert sanitize_community_label("Ask", members=ASK_MEMBERS) is None
    assert sanitize_community_label("Ask evidence admit check review publish", members=[]) is None
    assert sanitize_community_label("Evidence admission verification pipelines", members=[]) is None
    assert sanitize_community_label("Ask <b>Evidence</b>", members=ASK_MEMBERS) is None


async def test_generated_name_equal_to_candidate_writes_deterministic(
    code_storage: CodeIndexStorage,
) -> None:
    community = _insert_community(
        code_storage,
        1,
        ASK_MEMBERS,
        label_deterministic="src/gobby/ask",
        label_candidates=["src/gobby/ask", "evidence", "ask"],
    )
    adapter = ScriptedJSONAdapter(
        {ASK_MEMBERS[0]: {"name": "evidence", "rationale": "Every member handles evidence."}}
    )

    outcomes = await _labeler({"endpoint:lm-studio": adapter}).label_batch(
        PROJECT_ID, [community], storage=code_storage, run_db=_run_db
    )

    assert outcomes == {1: CommunityLabelOutcome.WRITTEN_DETERMINISTIC}
    row = _label_row(code_storage, 1)
    assert row["label"] == "evidence"
    assert row["label_source"] == "deterministic"
    assert row["label_confidence"] is None
    assert row["label_model"] == GENERATION_PROFILE
    assert row["labeled_signature"] == community.member_signature
    assert _queued_ids(code_storage, cooloff_seconds=0) == []


async def test_generation_failure_stamps_attempt_only(code_storage: CodeIndexStorage) -> None:
    community = _insert_community(
        code_storage,
        2,
        ASK_MEMBERS,
        label_deterministic="src/gobby/ask",
        label_candidates=["src/gobby/ask", "evidence"],
    )
    adapter = ScriptedJSONAdapter({ASK_MEMBERS[0]: RuntimeError("provider unavailable")})

    outcomes = await _labeler({"endpoint:lm-studio": adapter}).label_batch(
        PROJECT_ID, [community], storage=code_storage, run_db=_run_db
    )

    assert outcomes == {2: CommunityLabelOutcome.GENERATION_FAILED}
    _assert_attempt_only(code_storage, community)
    assert _queued_ids(code_storage, cooloff_seconds=300) == []
    assert _queued_ids(code_storage, cooloff_seconds=0) == [2]


async def test_rejections_stamp_attempt_only(code_storage: CodeIndexStorage) -> None:
    schema_members = ["src/gobby/tasks/claim.py", "src/gobby/tasks/close.py"]
    sanitation_members = ["src/gobby/memory/recall.py", "src/gobby/memory/digest.py"]
    schema_community = _insert_community(
        code_storage,
        3,
        schema_members,
        label_deterministic="src/gobby/tasks",
        label_candidates=["src/gobby/tasks", "claim"],
    )
    sanitation_community = _insert_community(
        code_storage,
        4,
        sanitation_members,
        label_deterministic="src/gobby/memory",
        label_candidates=["src/gobby/memory", "recall"],
    )
    responses: dict[str, ScriptedResponse] = {
        schema_members[0]: {"name": "x", "rationale": "Too short to be a name."},
        sanitation_members[0]: {"name": "memory/recall.py", "rationale": "The busiest file."},
    }

    outcomes = await _labeler(
        {
            "endpoint:a": ScriptedJSONAdapter(responses),
            "endpoint:b": ScriptedJSONAdapter(responses),
        }
    ).label_batch(
        PROJECT_ID,
        [schema_community, sanitation_community],
        storage=code_storage,
        run_db=_run_db,
    )

    assert outcomes == {
        3: CommunityLabelOutcome.SCHEMA_REJECTED,
        4: CommunityLabelOutcome.SANITATION_REJECTED,
    }
    _assert_attempt_only(code_storage, schema_community)
    _assert_attempt_only(code_storage, sanitation_community)
    assert _queued_ids(code_storage, cooloff_seconds=300) == []
    assert _queued_ids(code_storage, cooloff_seconds=0) == [3, 4]


async def test_every_outcome_emits_one_log_event(
    code_storage: CodeIndexStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scripted: list[tuple[str, ScriptedResponse, list[str]]] = [
        ("src/gobby/ask/service.py", {"name": "evidence", "rationale": "r"}, ["evidence"]),
        ("src/gobby/tasks/claim.py", {"name": "Task Claiming", "rationale": "r"}, ["claim"]),
        ("src/gobby/memory/recall.py", {"name": "x", "rationale": "r"}, ["recall"]),
        ("src/gobby/hooks/router.py", {"name": "router.py only", "rationale": "r"}, ["router"]),
        ("src/gobby/cli/install.py", RuntimeError("provider unavailable"), ["install"]),
    ]
    communities = [
        _insert_community(
            code_storage,
            community_id,
            [member, f"{member.rsplit('/', 1)[0]}/helpers.py"],
            label_deterministic=member.rsplit("/", 1)[0],
            label_candidates=candidates,
        )
        for community_id, (member, _response, candidates) in enumerate(scripted, start=10)
    ]
    responses = {member: response for member, response, _candidates in scripted}
    labeler = _labeler(
        {
            "endpoint:a": ScriptedJSONAdapter(responses),
            "endpoint:b": ScriptedJSONAdapter(responses),
        }
    )

    with caplog.at_level(logging.INFO, logger="gobby.code_index.community_labeler"):
        await labeler.label_batch(PROJECT_ID, communities, storage=code_storage, run_db=_run_db)

    events = [
        record
        for record in caplog.records
        if record.getMessage() == "code_index.community_label.outcome"
    ]
    assert sorted(
        (event.__dict__["community_id"], event.__dict__["outcome"]) for event in events
    ) == [
        (10, "written_deterministic"),
        (11, "written_model"),
        (12, "schema_rejected"),
        (13, "sanitation_rejected"),
        (14, "generation_failed"),
    ]
    for event, community in zip(
        sorted(events, key=lambda event: event.__dict__["community_id"]), communities, strict=True
    ):
        assert event.__dict__["project_id"] == PROJECT_ID
        assert event.__dict__["member_signature"] == community.member_signature


async def test_ungated_validated_name_writes_model_label(code_storage: CodeIndexStorage) -> None:
    community = _insert_community(
        code_storage,
        5,
        ASK_MEMBERS,
        label_deterministic="src/gobby/ask",
        label_candidates=["src/gobby/ask", "evidence"],
    )
    payload = {"name": "Ask Evidence Admission", "rationale": "Admits and validates evidence."}
    adapter = ScriptedJSONAdapter({ASK_MEMBERS[0]: payload})
    labeler = _labeler({"endpoint:lm-studio": adapter})
    assert CodeIndexCommunityLabelConfig().decisions_api_base is None

    generated = await labeler.generate_batch([community])
    outcomes = await labeler.label_batch(
        PROJECT_ID, [community], storage=code_storage, run_db=_run_db
    )

    assert generated == {
        5: GeneratedLabel(
            name="Ask Evidence Admission",
            rationale="Admits and validates evidence.",
            model=GENERATION_PROFILE,
        )
    }
    assert outcomes == {5: CommunityLabelOutcome.WRITTEN_MODEL}
    row = _label_row(code_storage, 5)
    assert row["label"] == "Ask Evidence Admission"
    assert row["label_source"] == "model"
    assert row["label_confidence"] is None
    assert row["label_model"] == GENERATION_PROFILE
    assert row["labeled_signature"] == community.member_signature
    assert "untrusted" in adapter.requests[0].prompt
    assert all(member in adapter.requests[0].prompt for member in ASK_MEMBERS)
    assert _queued_ids(code_storage, cooloff_seconds=0) == []
