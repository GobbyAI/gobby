"""Tests for model-generated import-community labels (plan section 6.2)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    TextGenerationRequest,
    TextGenerationService,
    decisions,
)
from gobby.ai.decisions import ChoiceQuestion, get_decision_service
from gobby.code_index.community_label_safety import sanitize_community_label
from gobby.code_index.community_labeler import (
    CommunityLabeler,
    CommunityLabelOutcome,
    GeneratedLabel,
)
from gobby.code_index.models import IndexedProject, IndexWriteMode, StoredCommunity
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.ai import DecisionsConfig
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
    return CommunityLabeler(*_generation(adapters))


def _generation(
    adapters: dict[str, ScriptedJSONAdapter],
) -> tuple[TextGenerationService, CodeIndexConfig]:
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
    return service, config


async def _run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def _insert_community(
    storage: CodeIndexStorage,
    community_id: int,
    members: Sequence[str],
    *,
    label_deterministic: str,
    label_candidates: Sequence[str],
    label: str | None = None,
    label_source: str = "deterministic",
    label_confidence: float | None = None,
    label_model: str | None = None,
    labeled_signature: str | None = None,
    representatives: Sequence[str] | None = None,
) -> StoredCommunity:
    storage.upsert_project_stats(
        IndexedProject(id=PROJECT_ID, root_path="/tmp/gobby-community-label-tests"),
        mode=IndexWriteMode.OVERLAY,
    )
    storage.db.execute(
        """INSERT INTO code_communities (
               machine_id, project_id, community_id, member_count, members,
               representatives, internal_edges, cohesion, boundary, member_signature,
               label_deterministic, label, label_source, label_candidates,
               label_confidence, label_model, labeled_signature
           )
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            require_machine_id(),
            PROJECT_ID,
            community_id,
            len(members),
            list(members),
            list(members[:1] if representatives is None else representatives),
            len(members) - 1,
            0.5,
            f"{community_id:016x}",
            label_deterministic,
            label if label is not None else label_deterministic,
            label_source,
            list(label_candidates),
            label_confidence,
            label_model,
            labeled_signature,
        ),
    )
    queued = storage.get_unlabeled_communities(PROJECT_ID, limit=500)
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


async def test_every_outcome_logs_at_debug_under_one_info_batch_summary(
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

    with caplog.at_level(logging.DEBUG, logger="gobby.code_index.community_labeler"):
        await labeler.label_batch(PROJECT_ID, communities, storage=code_storage, run_db=_run_db)

    events = [
        record
        for record in caplog.records
        if record.getMessage() == "code_index.community_label.outcome"
    ]
    # #22866: per-community outcomes are DEBUG; the batch gets one INFO summary.
    assert {event.levelno for event in events} == {logging.DEBUG}
    info = [
        record
        for record in caplog.records
        if record.name == "gobby.code_index.community_labeler" and record.levelno >= logging.INFO
    ]
    assert [record.getMessage() for record in info] == ["code_index.community_label.batch"]
    assert info[0].__dict__["project_id"] == PROJECT_ID
    assert info[0].__dict__["outcomes"] == {
        "generation_failed": 1,
        "sanitation_rejected": 1,
        "schema_rejected": 1,
        "written_deterministic": 1,
        "written_model": 1,
    }
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
    assert DecisionsConfig().community_label.mode == "off"

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


# --- community-label-decisions P2: Choice gate, Noul carry, Score sufficiency (#22604) ---

DECISION_MODEL = "jev-1.14"
RESPONSE_MODEL = "jev-1.14.0"
BACKEND = f"response:{RESPONSE_MODEL}"
REAL_ASYNC_CLIENT = httpx.AsyncClient
OUTCOME_EVENT = "code_index.community_label.outcome"
CARRY_EVENT = "code_index.community_label.carry"
SUFFICIENCY_EVENT = "code_index.community_label.sufficiency"
NINE_OUTCOMES = {
    "schema_rejected",
    "sanitation_rejected",
    "generation_failed",
    "written_deterministic",
    "written_model",
    "gate_admitted",
    "gate_rejected",
    "transport_failed",
    "write_superseded",
}
SCORE_LEVELS = [
    "misleading: the name points at the wrong purpose",
    "vague: the name fits but says little",
    "clear: the name says what the files share",
    "clear and specific: the name says what the files share and how they differ from their "
    "neighbors",
]
STALE_NAME = "Ask Evidence Admission"


class DecisionWire:
    """A stubbed decision server at the httpx boundary, scripted by question key."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.choices: dict[str, tuple[str, float]] = {}
        self.noul: dict[str, float] = {}
        self.score: dict[str, list[float]] = {}
        self.failing: set[str] = set()  # question types answered with HTTP 401

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if {question["type"] for question in body["questions"].values()} & self.failing:
            return httpx.Response(401)
        answers = {key: self._answer(key, q) for key, q in body["questions"].items()}
        return httpx.Response(
            200,
            json={"model": RESPONSE_MODEL, "answers": answers, "usage": {"input_tokens": 100}},
        )

    def _answer(self, key: str, question: dict[str, Any]) -> dict[str, Any]:
        kind = question["type"]
        if kind == "choice":
            option, confidence = self.choices[key]
            probabilities = dict.fromkeys(question["criteria"], 0.0)
            probabilities[option] = 1.0
            return {
                "type": "choice",
                "choice": option,
                "probabilities": probabilities,
                "confidence": confidence,
            }
        if kind == "noul":
            return {"type": "noul", "noul": self.noul[key]}
        assert kind == "score", kind
        weights = self.score[key]
        return {
            "type": "score",
            "score": sum(index * p for index, p in enumerate(weights)),
            "confidence": 0.9,
            "legend": {str(index): level for index, level in enumerate(question["criteria"])},
            "probabilities": {str(index): p for index, p in enumerate(weights)},
        }

    def questions(self, kind: str) -> dict[str, dict[str, Any]]:
        return {
            key: question
            for body in self.bodies
            for key, question in body["questions"].items()
            if question["type"] == kind
        }

    def kinds(self) -> set[str]:
        return {q["type"] for body in self.bodies for q in body["questions"].values()}


@pytest.fixture
def decision_wire(monkeypatch: pytest.MonkeyPatch) -> DecisionWire:
    wire = DecisionWire()

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(wire.handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(decisions, "_service_cache", None)
    return wire


def _consumer(
    mode: str,
    *,
    evaluated_model: str = DECISION_MODEL,
    evaluated_backend: str = BACKEND,
    **thresholds: float,
) -> dict[str, Any]:
    values: dict[str, Any] = {"mode": mode, **thresholds}
    if mode != "off":
        values |= {"evaluated_model": evaluated_model, "evaluated_backend": evaluated_backend}
    return values


def _decisions(**fields: Any) -> DecisionsConfig:
    values: dict[str, Any] = {
        "api_base": "https://decide.test",
        "model": DECISION_MODEL,
        "identity_contract": "response_version",
    }
    return DecisionsConfig.model_validate(values | fields)


def _gated_labeler(
    adapter: ScriptedJSONAdapter, resolver: Callable[[], DecisionsConfig | None] | None
) -> CommunityLabeler:
    service, config = _generation({"endpoint:lm-studio": adapter})
    return CommunityLabeler(service, config, decisions_resolver=resolver)


def _area(
    storage: CodeIndexStorage,
    community_id: int,
    *,
    members: Sequence[str] | None = None,
    **row: Any,
) -> StoredCommunity:
    root = f"src/gobby/area{community_id}"
    return _insert_community(
        storage,
        community_id,
        members or [f"{root}/service.py", f"{root}/models.py", f"{root}/helpers.py"],
        label_deterministic=root,
        label_candidates=[root, f"area{community_id} core"],
        **row,
    )


def _stale(storage: CodeIndexStorage, community_id: int, *, label: str) -> StoredCommunity:
    """A model name carried forward across a membership change, so its signature is stale."""
    return _area(
        storage,
        community_id,
        label=label,
        label_source="model",
        label_confidence=0.82,
        label_model="jev-1.13.0",
        labeled_signature="stale-signature",
    )


def _named(community_id: int) -> str:
    return f"Area {community_id} Evidence Flow"


def _generates(*communities: StoredCommunity) -> ScriptedJSONAdapter:
    return ScriptedJSONAdapter(
        {
            c.members[0]: {"name": _named(c.community_id), "rationale": f"Area {c.community_id}."}
            for c in communities
        }
    )


def _assert_decided(
    storage: CodeIndexStorage,
    community: StoredCommunity,
    *,
    label: str,
    label_source: str,
    label_confidence: float | None,
    label_model: str,
) -> None:
    row = _label_row(storage, community.community_id)
    assert (row["label"], row["label_source"], row["label_model"]) == (
        label,
        label_source,
        label_model,
    )
    if label_confidence is None:
        assert row["label_confidence"] is None
    else:
        assert row["label_confidence"] == pytest.approx(label_confidence, abs=1e-6)
    assert row["labeled_signature"] == community.member_signature
    assert row["labeled_at"] is not None


def _events(caplog: pytest.LogCaptureFixture, message: str) -> list[dict[str, Any]]:
    return [record.__dict__ for record in caplog.records if record.getMessage() == message]


def _superseding_run_db(storage: CodeIndexStorage, community_ids: set[int]) -> Any:
    """A run_db whose refresh replaces a row between the read and the guarded write."""

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        community_id = kwargs.get("community_id", args[1] if len(args) > 1 else None)
        if getattr(func, "__name__", "") == "update_community_label" and (
            community_id in community_ids
        ):
            storage.db.execute(
                """UPDATE code_communities SET member_signature = %s
                   WHERE machine_id = %s AND project_id = %s AND community_id = %s""",
                ("f" * 16, require_machine_id(), PROJECT_ID, community_id),
            )
        return func(*args, **kwargs)

    return run_db


async def test_gate_admits_at_threshold_and_rejects_below(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    wide_members = [f"src/gobby/area5/part_{index:02d}.py" for index in range(50)]
    at_threshold = _area(code_storage, 1)
    below = _area(code_storage, 2)
    deterministic = _area(code_storage, 3)
    wide = _area(code_storage, 5, members=wide_members)
    batch = [at_threshold, below, deterministic, wide]
    decision_wire.choices = {
        "c1": ("generated", 0.7),
        "c2": ("generated", 0.69),
        "c3": ("deterministic_1", 0.8),
        "c5": ("generated", 0.95),
    }
    config = _decisions(community_label=_consumer("enforce", min_confidence=0.7))

    outcomes = await _gated_labeler(_generates(*batch), lambda: config).label_batch(
        PROJECT_ID, batch, storage=code_storage, run_db=_run_db
    )

    assert outcomes == {
        1: "gate_admitted",
        2: "gate_rejected",
        3: "gate_rejected",
        5: "gate_admitted",
    }
    _assert_decided(
        code_storage,
        at_threshold,
        label=_named(1),
        label_source="model",
        label_confidence=0.7,
        label_model=RESPONSE_MODEL,
    )
    _assert_decided(
        code_storage,
        below,
        label=below.label_deterministic,
        label_source="deterministic",
        label_confidence=0.69,
        label_model=RESPONSE_MODEL,
    )
    _assert_decided(
        code_storage,
        deterministic,
        label=deterministic.label_candidates[1],
        label_source="deterministic",
        label_confidence=0.8,
        label_model=RESPONSE_MODEL,
    )
    _assert_decided(
        code_storage,
        wide,
        label=_named(5),
        label_source="model",
        label_confidence=0.95,
        label_model=RESPONSE_MODEL,
    )
    assert _queued_ids(code_storage, cooloff_seconds=0) == []

    choices = decision_wire.questions("choice")
    assert set(choices) == {"c1", "c2", "c3", "c5"}
    assert "c1" in choices["c1"]["instructions"]
    assert choices["c1"]["criteria"] == {
        "generated": _named(1),
        "deterministic_0": at_threshold.label_candidates[0],
        "deterministic_1": at_threshold.label_candidates[1],
    }
    state = next(body["state"] for body in decision_wire.bodies if "c5" in body["questions"])
    wide_state = state["communities"]["c5"]
    others = sorted(set(wide.members) - set(wide.representatives))
    assert wide_state["members"] == (wide.representatives + others)[:40]
    assert wide_state["representatives"] == wide.representatives
    assert wide_state["candidates"] == wide.label_candidates
    assert wide_state["generated"] == {"name": _named(5), "rationale": "Area 5."}

    # A transport failure is not a decision: it stamps the attempt only.
    failing = _area(code_storage, 4)
    decision_wire.failing = {"choice"}
    outcomes = await _gated_labeler(_generates(failing), lambda: config).label_batch(
        PROJECT_ID, [failing], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {4: "transport_failed"}
    _assert_attempt_only(code_storage, failing)
    assert _queued_ids(code_storage, cooloff_seconds=300) == []
    assert _queued_ids(code_storage, cooloff_seconds=0) == [4]


async def test_gate_outcomes_extend_the_discriminator_to_nine(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert {outcome.value for outcome in CommunityLabelOutcome} == NINE_OUTCOMES

    long_root = "src/gobby/area16/" + "deeply_nested_directory/" * 4
    gated = [_area(code_storage, community_id) for community_id in range(10, 16)]
    oversize = _area(
        code_storage, 16, members=[f"{long_root}module_{index:02d}.py" for index in range(40)]
    )
    superseded_gate = _area(code_storage, 17)
    schema, sanitation, failed, candidate, admitted, rejected = gated
    adapter = _generates(admitted, rejected, oversize, superseded_gate)
    adapter._responses |= {
        schema.members[0]: {"name": "x", "rationale": "r"},
        sanitation.members[0]: {"name": "Area service.py", "rationale": "r"},
        failed.members[0]: RuntimeError("provider unavailable"),
        candidate.members[0]: {"name": candidate.label_candidates[1], "rationale": "r"},
    }
    decision_wire.choices = {
        "c14": ("generated", 0.9),
        "c15": ("deterministic_0", 0.9),
        "c17": ("generated", 0.9),
    }
    enforce = _decisions(max_input_tokens=1024, community_label=_consumer("enforce"))

    enforced = await _gated_labeler(adapter, lambda: enforce).label_batch(
        PROJECT_ID,
        [*gated, oversize, superseded_gate],
        storage=code_storage,
        run_db=_superseding_run_db(code_storage, {17}),
    )

    assert enforced == {
        10: "schema_rejected",
        11: "sanitation_rejected",
        12: "generation_failed",
        13: "written_deterministic",
        14: "gate_admitted",
        15: "gate_rejected",
        16: "transport_failed",
        17: "write_superseded",
    }
    batch_lines = _events(caplog, "code_index.community_label.batch")
    assert batch_lines[-1]["outcomes"] == dict.fromkeys(enforced.values(), 1)

    shadow_written = _area(code_storage, 18)
    superseded_model = _area(code_storage, 19)
    decision_wire.choices |= {"c18": ("generated", 0.9), "c19": ("generated", 0.9)}
    shadow = _decisions(community_label=_consumer("shadow"))
    shadowed = await _gated_labeler(
        _generates(shadow_written, superseded_model), lambda: shadow
    ).label_batch(
        PROJECT_ID,
        [shadow_written, superseded_model],
        storage=code_storage,
        run_db=_superseding_run_db(code_storage, {19}),
    )
    assert shadowed == {18: "written_model", 19: "write_superseded"}

    events = {event["community_id"]: event for event in _events(caplog, OUTCOME_EVENT)}
    assert {event["outcome"] for event in events.values()} == NINE_OUTCOMES
    assert events[13]["decision_mode"] == "enforce"
    assert events[13]["decision_choice"] is None
    assert events[14]["decision_mode"] == "enforce"
    assert events[14]["decision_choice"] == "generated"
    assert events[14]["decision_confidence"] == pytest.approx(0.9)
    assert events[14]["decision_unavailable"] is None
    assert events[16]["decision_unavailable"] == "oversize"
    assert events[18]["decision_mode"] == "shadow"

    # write_superseded stamps no field: the refreshed row is legitimately requeued.
    for community in (superseded_gate, superseded_model):
        row = _label_row(code_storage, community.community_id)
        assert row["label"] == community.label_deterministic
        assert row["label_source"] == "deterministic"
        assert row["labeled_signature"] is None
        assert row["labeled_at"] is None
        assert row["label_attempted_at"] is None


def _assert_requests_fit(bodies: list[dict[str, Any]], config: DecisionsConfig) -> None:
    service = get_decision_service(config)
    for body in bodies:
        questions = {
            key: ChoiceQuestion(instructions=q["instructions"], criteria=q["criteria"])
            for key, q in body["questions"].items()
        }
        assert 1 <= len(questions) <= 64
        assert set(body["state"]["communities"]) == set(questions)
        assert service.estimate_request_tokens(body["state"], questions) <= config.max_input_tokens


async def test_gate_batches_fit_the_request_ceiling(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    batch = [_area(code_storage, community_id) for community_id in range(100, 170)]
    decision_wire.choices = {f"c{c.community_id}": ("generated", 0.9) for c in batch}
    roomy = _decisions(max_input_tokens=60000, community_label=_consumer("enforce"))

    outcomes = await _gated_labeler(_generates(*batch), lambda: roomy).label_batch(
        PROJECT_ID, batch, storage=code_storage, run_db=_run_db
    )

    assert outcomes == {c.community_id: "gate_admitted" for c in batch}
    # The 64-question limit splits an otherwise roomy batch, packed greedily in order.
    assert [len(body["questions"]) for body in decision_wire.bodies] == [64, 6]
    assert [key for body in decision_wire.bodies for key in body["questions"]] == [
        f"c{c.community_id}" for c in batch
    ]
    _assert_requests_fit(decision_wire.bodies, roomy)

    decision_wire.bodies.clear()
    tight_batch = [_area(code_storage, community_id) for community_id in range(200, 220)]
    decision_wire.choices |= {f"c{c.community_id}": ("generated", 0.9) for c in tight_batch}
    tight = _decisions(max_input_tokens=1024, community_label=_consumer("enforce"))

    outcomes = await _gated_labeler(_generates(*tight_batch), lambda: tight).label_batch(
        PROJECT_ID, tight_batch, storage=code_storage, run_db=_run_db
    )

    assert outcomes == {c.community_id: "gate_admitted" for c in tight_batch}
    assert len(decision_wire.bodies) >= 2
    assert sorted(key for body in decision_wire.bodies for key in body["questions"]) == sorted(
        f"c{c.community_id}" for c in tight_batch
    )
    _assert_requests_fit(decision_wire.bodies, tight)


async def test_gate_batches_fit_the_question_context_ceiling(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    batch = [_area(code_storage, community_id) for community_id in range(300, 320)]
    decision_wire.choices = {f"c{c.community_id}": ("generated", 0.9) for c in batch}
    config = _decisions(
        max_input_tokens=60000,
        backend_max_question_tokens=1024,
        community_label=_consumer("enforce"),
    )
    outcomes = await _gated_labeler(_generates(*batch), lambda: config).label_batch(
        PROJECT_ID, batch, storage=code_storage, run_db=_run_db
    )
    assert outcomes == {c.community_id: "gate_admitted" for c in batch}
    assert len(decision_wire.bodies) >= 2
    service = decisions.DecisionService(config)
    for body in decision_wire.bodies:
        for key, raw in body["questions"].items():
            question = ChoiceQuestion(raw["instructions"], raw["criteria"])
            assert service.estimate_request_tokens(body["state"], {key: question}) <= 1024


async def test_enforce_without_matching_evaluation_runs_as_shadow(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    communities = [_area(code_storage, community_id) for community_id in range(30, 34)]
    decision_wire.choices = {f"c{c.community_id}": ("deterministic_0", 0.95) for c in communities}
    live: dict[str, DecisionsConfig] = {
        "config": _decisions(
            community_label=_consumer("enforce", evaluated_backend="response:jev-1.13.0")
        )
    }
    labeler = _gated_labeler(_generates(*communities), lambda: live["config"])

    def mismatch_warnings() -> int:
        return sum(
            record.levelno == logging.WARNING
            and "evaluated_backend_mismatch" in record.getMessage()
            for record in caplog.records
        )

    async def label(community: StoredCommunity) -> dict[int, CommunityLabelOutcome]:
        return await labeler.label_batch(
            PROJECT_ID, [community], storage=code_storage, run_db=_run_db
        )

    backend_mismatch, model_mismatch, after_window, matching = communities
    assert await label(backend_mismatch) == {30: "written_model"}
    _assert_decided(
        code_storage,
        backend_mismatch,
        label=_named(30),
        label_source="model",
        label_confidence=None,
        label_model=GENERATION_PROFILE,
    )
    assert "c30" in decision_wire.questions("choice")
    assert _events(caplog, OUTCOME_EVENT)[-1]["decision_mode"] == "shadow"

    live["config"] = _decisions(community_label=_consumer("enforce", evaluated_model="jev-1.13"))
    assert await label(model_mismatch) == {31: "written_model"}
    assert mismatch_warnings() == 1

    live["config"] = _decisions(
        failure_cooldown_seconds=0,
        community_label=_consumer("enforce", evaluated_backend="response:jev-1.13.0"),
    )
    assert await label(after_window) == {32: "written_model"}
    assert mismatch_warnings() == 2

    # A reloaded policy takes effect on the next batch.
    live["config"] = _decisions(community_label=_consumer("enforce"))
    assert await label(matching) == {33: "gate_rejected"}
    _assert_decided(
        code_storage,
        matching,
        label=matching.label_deterministic,
        label_source="deterministic",
        label_confidence=0.95,
        label_model=RESPONSE_MODEL,
    )
    assert mismatch_warnings() == 2


async def test_gate_off_makes_no_decision_request(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cases: list[tuple[str, Callable[[], DecisionsConfig | None] | None]] = [
        ("no-resolver", None),
        ("resolver-returns-none", lambda: None),
        ("api-base-unset", DecisionsConfig),
        ("only-model-set", lambda: DecisionsConfig(model=DECISION_MODEL)),
        ("gate-off", _decisions),
    ]
    for offset, (name, resolver) in enumerate(cases):
        community = _area(code_storage, 40 + offset)
        outcomes = await _gated_labeler(_generates(community), resolver).label_batch(
            PROJECT_ID, [community], storage=code_storage, run_db=_run_db
        )
        assert outcomes == {community.community_id: "written_model"}, name
        _assert_decided(
            code_storage,
            community,
            label=_named(community.community_id),
            label_source="model",
            label_confidence=None,
            label_model=GENERATION_PROFILE,
        )
    assert decision_wire.bodies == []
    events = _events(caplog, OUTCOME_EVENT)
    assert [event["decision_choice"] for event in events] == [None] * len(cases)
    assert events[-1]["decision_mode"] == "off"


async def test_carry_keeps_name_above_threshold_without_generation(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stale = _stale(code_storage, 50, label=STALE_NAME)
    fresh = _area(code_storage, 51)
    decision_wire.noul = {"c50": 0.5}
    config = _decisions(community_label_carry=_consumer("enforce"))
    adapter = _generates(fresh)

    outcomes = await _gated_labeler(adapter, lambda: config).label_batch(
        PROJECT_ID, [stale, fresh], storage=code_storage, run_db=_run_db
    )

    assert outcomes == {51: "written_model"}
    assert len(adapter.requests) == 1
    assert stale.members[0] not in adapter.requests[0].prompt
    row = _label_row(code_storage, 50)
    assert (row["label"], row["label_source"], row["label_model"]) == (
        STALE_NAME,
        "model",
        "jev-1.13.0",
    )
    assert row["label_confidence"] == pytest.approx(0.82, abs=1e-6)
    assert row["labeled_signature"] == stale.member_signature
    assert decision_wire.questions("noul") == {
        "c50": {
            "type": "noul",
            "instructions": f"The name `{STALE_NAME}` describes what the files in community "
            "c50 have in common.",
        }
    }
    carried_state = json.dumps(decision_wire.bodies[0]["state"])
    assert all(member in carried_state for member in stale.members)
    carry = [(e["outcome"], e["mode"], e["probability"]) for e in _events(caplog, CARRY_EVENT)]
    assert carry == [("kept", "enforce", 0.5)]
    assert [event["community_id"] for event in _events(caplog, OUTCOME_EVENT)] == [51]
    assert _queued_ids(code_storage, cooloff_seconds=0) == []


async def test_carry_below_threshold_or_shadow_regenerates(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _consumer("enforce")

    def carry_events() -> list[tuple[str, str, float | None]]:
        return [(e["outcome"], e["mode"], e["probability"]) for e in _events(caplog, CARRY_EVENT)]

    # Below the floor, and a stale name that no longer sanitizes: both regenerate and gate.
    below = _stale(code_storage, 60, label=STALE_NAME)
    unsanitary = _stale(code_storage, 61, label="Area service.py")
    decision_wire.noul = {"c60": 0.49}
    decision_wire.choices = dict.fromkeys(("c60", "c61", "c62"), ("generated", 0.9))
    enforce = _decisions(community_label=gate, community_label_carry=_consumer("enforce"))
    outcomes = await _gated_labeler(_generates(below, unsanitary), lambda: enforce).label_batch(
        PROJECT_ID, [below, unsanitary], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {60: "gate_admitted", 61: "gate_admitted"}
    for community in (below, unsanitary):
        _assert_decided(
            code_storage,
            community,
            label=_named(community.community_id),
            label_source="model",
            label_confidence=0.9,
            label_model=RESPONSE_MODEL,
        )
    assert set(decision_wire.questions("noul")) == {"c60"}
    assert ("regenerated", "enforce", 0.49) in carry_events()

    # Shadow records the probability but never keeps the name.
    caplog.clear()
    shadowed = _stale(code_storage, 62, label=STALE_NAME)
    decision_wire.noul["c62"] = 0.99
    shadow = _decisions(community_label=gate, community_label_carry=_consumer("shadow"))
    outcomes = await _gated_labeler(_generates(shadowed), lambda: shadow).label_batch(
        PROJECT_ID, [shadowed], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {62: "gate_admitted"}
    assert carry_events() == [("regenerated", "shadow", 0.99)]

    # An unavailable decision falls through to generation (gate off: 6.2's write).
    caplog.clear()
    unavailable = _stale(code_storage, 63, label=STALE_NAME)
    decision_wire.failing = {"noul"}
    carry_only = _decisions(community_label_carry=_consumer("enforce"))
    outcomes = await _gated_labeler(_generates(unavailable), lambda: carry_only).label_batch(
        PROJECT_ID, [unavailable], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {63: "written_model"}
    assert [(outcome, probability) for outcome, _mode, probability in carry_events()] == [
        ("unavailable", None)
    ]


async def test_carry_runs_with_gate_off(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    kept = _stale(code_storage, 70, label=STALE_NAME)
    regenerated = _stale(code_storage, 71, label="Ask Evidence Review")
    decision_wire.noul = {"c70": 0.8, "c71": 0.2}
    config = _decisions(community_label_carry=_consumer("enforce"))

    outcomes = await _gated_labeler(_generates(kept, regenerated), lambda: config).label_batch(
        PROJECT_ID, [kept, regenerated], storage=code_storage, run_db=_run_db
    )

    assert outcomes == {71: "written_model"}
    row = _label_row(code_storage, 70)
    assert (row["label"], row["labeled_signature"]) == (STALE_NAME, kept.member_signature)
    _assert_decided(
        code_storage,
        regenerated,
        label=_named(71),
        label_source="model",
        label_confidence=None,
        label_model=GENERATION_PROFILE,
    )
    assert decision_wire.kinds() == {"noul"}


async def test_sufficient_deterministic_label_skips_generation(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clear = _area(code_storage, 80)
    boundary = _area(code_storage, 81)
    carried = _stale(code_storage, 83, label=STALE_NAME)
    decision_wire.noul = {"c83": 0.9}
    decision_wire.score = {"c80": [0.0, 0.0, 1.0, 0.0], "c81": [0.0, 0.5, 0.5, 0.0]}
    config = _decisions(
        community_label_carry=_consumer("enforce"),
        community_label_sufficiency=_consumer("enforce"),
    )
    adapter = _generates()

    outcomes = await _gated_labeler(adapter, lambda: config).label_batch(
        PROJECT_ID, [clear, boundary, carried], storage=code_storage, run_db=_run_db
    )

    assert outcomes == {}
    assert adapter.requests == []
    for community in (clear, boundary):
        _assert_decided(
            code_storage,
            community,
            label=community.label_deterministic,
            label_source="deterministic",
            label_confidence=None,
            label_model=RESPONSE_MODEL,
        )
    scored = decision_wire.questions("score")
    # Carry runs first, so a kept stale name is never re-scored.
    assert set(scored) == {"c80", "c81"}
    assert scored["c80"] == {
        "type": "score",
        "instructions": "Rate how well the name `src/gobby/area80` describes what the files "
        "in community c80 have in common.",
        "criteria": SCORE_LEVELS,
    }
    sufficiency = sorted(
        (e["outcome"], e["mode"], e["position"]) for e in _events(caplog, SUFFICIENCY_EVENT)
    )
    assert [(outcome, mode) for outcome, mode, _ in sufficiency] == [
        ("sufficient", "enforce"),
        ("sufficient", "enforce"),
    ]
    assert sorted(position for _, _, position in sufficiency) == [
        pytest.approx(0.5),
        pytest.approx(2 / 3),
    ]


async def test_insufficient_or_shadow_score_generates(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def sufficiency_events() -> list[dict[str, Any]]:
        return _events(caplog, SUFFICIENCY_EVENT)

    low = _area(code_storage, 84)
    decision_wire.score = {"c84": [0.5, 0.5, 0.0, 0.0], "c85": [0.0, 0.0, 0.0, 1.0]}
    enforce = _decisions(community_label_sufficiency=_consumer("enforce"))
    outcomes = await _gated_labeler(_generates(low), lambda: enforce).label_batch(
        PROJECT_ID, [low], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {84: "written_model"}
    [event] = sufficiency_events()
    assert (event["outcome"], event["mode"]) == ("insufficient", "enforce")
    assert event["position"] == pytest.approx(1 / 6)

    caplog.clear()
    high = _area(code_storage, 85)
    shadow = _decisions(community_label_sufficiency=_consumer("shadow"))
    outcomes = await _gated_labeler(_generates(high), lambda: shadow).label_batch(
        PROJECT_ID, [high], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {85: "written_model"}
    [event] = sufficiency_events()
    assert (event["outcome"], event["mode"]) == ("sufficient", "shadow")
    assert event["position"] == pytest.approx(1.0)

    caplog.clear()
    unavailable = _area(code_storage, 86)
    decision_wire.failing = {"score"}
    outcomes = await _gated_labeler(_generates(unavailable), lambda: enforce).label_batch(
        PROJECT_ID, [unavailable], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {86: "written_model"}
    [event] = sufficiency_events()
    assert event["outcome"] == "unavailable"


async def test_sufficiency_runs_alone_and_all_off_sends_nothing(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sufficient = _area(code_storage, 90)
    insufficient = _area(code_storage, 91)
    decision_wire.score = {"c90": [0.0, 0.0, 1.0, 0.0], "c91": [1.0, 0.0, 0.0, 0.0]}
    alone = _decisions(community_label_sufficiency=_consumer("enforce"))

    outcomes = await _gated_labeler(
        _generates(sufficient, insufficient), lambda: alone
    ).label_batch(PROJECT_ID, [sufficient, insufficient], storage=code_storage, run_db=_run_db)

    assert outcomes == {91: "written_model"}
    assert decision_wire.kinds() == {"score"}
    [batch_line] = _events(caplog, "code_index.community_label.batch")
    assert (batch_line["carried"], batch_line["sufficient"]) == (0, 1)

    requests_before = len(decision_wire.bodies)
    stale = _stale(code_storage, 92, label=STALE_NAME)
    fresh = _area(code_storage, 93)
    all_off = _decisions()
    outcomes = await _gated_labeler(_generates(stale, fresh), lambda: all_off).label_batch(
        PROJECT_ID, [stale, fresh], storage=code_storage, run_db=_run_db
    )
    assert outcomes == {92: "written_model", 93: "written_model"}
    assert len(decision_wire.bodies) == requests_before


# --- Choice evidence: de-dup, shared bounds, and the rationale boundary (#22604 round 2) ---

EVIDENCE_FILE_LIMIT = 40
EVIDENCE_CHAR_LIMIT = 8000


def _generates_by_label(
    *communities: StoredCommunity, rationale: Callable[[int], str] | None = None
) -> ScriptedJSONAdapter:
    """Script names by the deterministic-label line, which leads the bounded prompt."""
    return ScriptedJSONAdapter(
        {
            f"deterministic_label: {c.label_deterministic}\n": {
                "name": _named(c.community_id),
                "rationale": rationale(c.community_id) if rationale else f"Area {c.community_id}.",
            }
            for c in communities
        }
    )


def _evidence_chars(entry: dict[str, Any]) -> int:
    """Serialized evidence size in JSON-escaped characters (``ensure_ascii`` escapes)."""
    return len(json.dumps(entry))


def _state_entries(wire: DecisionWire, kind: str, key: str) -> list[dict[str, Any]]:
    return [
        body["state"]["communities"][key]
        for body in wire.bodies
        if key in body["questions"] and body["questions"][key]["type"] == kind
    ]


def _assert_bounded_evidence(entry: dict[str, Any], community: StoredCommunity) -> None:
    representatives, members = entry["representatives"], entry["members"]
    others = sorted(set(community.members) - set(community.representatives))
    assert representatives == community.representatives
    assert set(representatives).isdisjoint(members)
    assert len(set(members)) == len(members)
    # Whole paths only, in the stable remaining order.
    assert set(members) <= set(others)
    assert members == [path for path in others if path in set(members)]
    assert len(representatives) + len(members) <= EVIDENCE_FILE_LIMIT
    assert _evidence_chars(entry) <= EVIDENCE_CHAR_LIMIT
    assert entry["member_count"] == community.member_count
    assert entry["omitted_member_count"] == (
        community.member_count - len(representatives) - len(members)
    )


async def test_choice_state_deduplicates_without_changing_answer_identity(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    root = "src/gobby/area500"
    shared = [f"{root}/service.py", f"{root}/models.py", f"{root}/api.py"]
    rest = [f"{root}/{name}.py" for name in ("helpers", "errors", "config", "routes", "cache")]
    admitted = _area(code_storage, 500, members=[*shared, *rest], representatives=shared)
    rejected = _area(code_storage, 501)
    decision_wire.choices = {"c500": ("generated", 0.9), "c501": ("deterministic_1", 0.9)}
    config = _decisions(community_label=_consumer("enforce"))

    outcomes = await _gated_labeler(
        _generates_by_label(admitted, rejected), lambda: config
    ).label_batch(PROJECT_ID, [admitted, rejected], storage=code_storage, run_db=_run_db)

    assert outcomes == {500: "gate_admitted", 501: "gate_rejected"}
    choices = decision_wire.questions("choice")
    assert set(choices) == {"c500", "c501"}
    assert "c500" in choices["c500"]["instructions"]
    assert choices["c500"]["criteria"] == {
        "generated": _named(500),
        "deterministic_0": admitted.label_candidates[0],
        "deterministic_1": admitted.label_candidates[1],
    }
    [entry] = _state_entries(decision_wire, "choice", "c500")
    assert entry["representatives"] == shared
    assert entry["members"] == sorted(rest)
    assert entry["member_count"] == 8
    assert entry["omitted_member_count"] == 0
    # The criteria already carry the candidates and the generated name.
    assert "candidates" not in entry
    assert entry["generated"] == {"rationale": "Area 500."}
    _assert_decided(
        code_storage,
        admitted,
        label=_named(500),
        label_source="model",
        label_confidence=0.9,
        label_model=RESPONSE_MODEL,
    )
    _assert_decided(
        code_storage,
        rejected,
        label=rejected.label_candidates[1],
        label_source="deterministic",
        label_confidence=0.9,
        label_model=RESPONSE_MODEL,
    )


async def test_decision_evidence_has_stable_bounds_and_omission_count(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    count_root = "src/gobby/area510"
    counted = _area(
        code_storage,
        510,
        members=[f"{count_root}/part_{index:02d}.py" for index in range(60)],
        label=STALE_NAME,
        label_source="model",
        label_confidence=0.82,
        label_model="jev-1.13.0",
        labeled_signature="stale-signature",
    )
    # Each path escapes to roughly 390 JSON characters, so 40 of them cannot fit 8000.
    unicode_root = "src/gobby/area511/文档"
    byte_heavy = _area(
        code_storage,
        511,
        members=[f"{unicode_root}/{'模块' * 30}_{index:02d}.py" for index in range(40)],
        label=STALE_NAME,
        label_source="model",
        label_confidence=0.82,
        label_model="jev-1.13.0",
        labeled_signature="stale-signature",
    )
    batch = [counted, byte_heavy]
    decision_wire.choices = {"c510": ("generated", 0.9), "c511": ("generated", 0.9)}
    decision_wire.noul = {"c510": 0.2, "c511": 0.2}
    decision_wire.score = {key: [1.0, 0.0, 0.0, 0.0] for key in ("c510", "c511")}
    config = _decisions(
        community_label=_consumer("shadow"),
        community_label_carry=_consumer("shadow"),
        community_label_sufficiency=_consumer("shadow"),
    )

    await _gated_labeler(_generates_by_label(*batch), lambda: config).label_batch(
        PROJECT_ID, batch, storage=code_storage, run_db=_run_db
    )

    assert decision_wire.kinds() == {"choice", "noul", "score"}
    for kind in ("choice", "noul", "score"):
        [counted_entry] = _state_entries(decision_wire, kind, "c510")
        [heavy_entry] = _state_entries(decision_wire, kind, "c511")
        _assert_bounded_evidence(counted_entry, counted)
        _assert_bounded_evidence(heavy_entry, byte_heavy)
        # The count bound keeps the first 40 unique files.
        others = sorted(set(counted.members) - set(counted.representatives))
        assert counted_entry["members"] == others[:39]
        assert counted_entry["omitted_member_count"] == 20
        # The character bound omits whole files and counts them.
        assert heavy_entry["members"]
        assert heavy_entry["omitted_member_count"] > 0


async def test_required_representatives_cannot_fit_is_typed_oversize(
    code_storage: CodeIndexStorage,
    decision_wire: DecisionWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    root = "src/gobby/area520"
    representatives = [f"{root}/{letter * 3000}.py" for letter in "abc"]
    assert len(json.dumps(representatives)) > EVIDENCE_CHAR_LIMIT
    unfit = _area(
        code_storage,
        520,
        members=[*representatives, f"{root}/helpers.py"],
        representatives=representatives,
    )
    fits = _area(code_storage, 521)
    decision_wire.choices = {"c520": ("generated", 0.9), "c521": ("generated", 0.9)}
    config = _decisions(community_label=_consumer("enforce"))

    outcomes = await _gated_labeler(_generates_by_label(unfit, fits), lambda: config).label_batch(
        PROJECT_ID, [unfit, fits], storage=code_storage, run_db=_run_db
    )

    assert outcomes == {520: "transport_failed", 521: "gate_admitted"}
    events = {event["community_id"]: event for event in _events(caplog, OUTCOME_EVENT)}
    assert events[520]["decision_unavailable"] == "oversize"
    _assert_attempt_only(code_storage, unfit)
    assert set(decision_wire.questions("choice")) == {"c521"}
    # No request carries a partial required path.
    sent = json.dumps(decision_wire.bodies)
    assert not any(path[: len(root) + 200] in sent for path in representatives)


async def test_generated_rationale_schema_boundary(
    code_storage: CodeIndexStorage, decision_wire: DecisionWire
) -> None:
    at_limit = _area(code_storage, 530)
    over_limit = _area(code_storage, 531)
    rationales = {530: "r" * 120, 531: "r" * 121}
    decision_wire.choices = {"c530": ("generated", 0.9), "c531": ("generated", 0.9)}
    config = _decisions(community_label=_consumer("enforce"))

    outcomes = await _gated_labeler(
        _generates_by_label(at_limit, over_limit, rationale=rationales.__getitem__),
        lambda: config,
    ).label_batch(PROJECT_ID, [at_limit, over_limit], storage=code_storage, run_db=_run_db)

    assert outcomes == {530: "gate_admitted", 531: "schema_rejected"}
    assert set(decision_wire.questions("choice")) == {"c530"}
    [entry] = _state_entries(decision_wire, "choice", "c530")
    assert entry["generated"]["rationale"] == rationales[530]
    _assert_attempt_only(code_storage, over_limit)
