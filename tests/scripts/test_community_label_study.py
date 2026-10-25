"""Tests for the label-quality study tooling (community-label-decisions 3.3).

Each test chains the real stages: ``decisions_live_capture`` writes the capture,
``prepare`` writes the sheet, key and run record, the test fills ratings, and ``score``
writes the results section. Only the plan's flags, sheet columns, arms and ship keys are
pinned; the seams listed in ``community_label_study.main`` are injected.
"""

from __future__ import annotations

import copy
import csv
import json
import re
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from gobby.code_index.models import IndexedProject, IndexWriteMode, StoredCommunity
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexCommunityLabelConfig, CodeIndexConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id
from scripts import community_label_study, decisions_live_capture
from tests.code_index.conftest import PROJECT_ID
from tests.scripts.conftest import (
    ALIAS_MODEL,
    BACKEND,
    BACKEND_MAX_STATE_TOKENS,
    REQUEST_MODEL,
    RESPONSE_MODEL,
    OpenRouterWire,
    decision_flags,
    resolve_secret,
    write_png,
)

pytestmark = pytest.mark.unit

GOBBY = "d45545c5-ded5-4335-b115-0245752edacf"
C1 = "c8e071af-e1f7-434f-b3c0-f31a1b1299c9"
CORPUS_BASE = {GOBBY: 0, C1: 100}
STORED_LABEL = "Stored Label Never Read"
SHEET_COLUMNS = ["item_id", "members", "name", "accuracy (0-2)", "findability (0-2)"]
MEMBER_PATH = re.compile(r"p[0-9a-f]{4}/c\d+/m\d+\.py")
MAIN_ARMS = {"D", "G", "J"}
MOVED_MODEL = "typesafe/jev-1.14-20261101"

type Rating = tuple[int, int]
type Rater = Callable[[dict[str, Any], str], Rating]


def _community(project_id: str, community_id: int, size: int) -> StoredCommunity:
    members = [f"p{project_id[:4]}/c{community_id}/m{index}.py" for index in range(size)]
    deterministic = f"Deterministic Area {community_id}"
    return StoredCommunity(
        machine_id="machine",
        project_id=project_id,
        community_id=community_id,
        member_count=size,
        members=members,
        representatives=members[:2],
        internal_edges=size,
        cohesion=0.5,
        boundary=[],
        member_signature=f"sig-{community_id}",
        label_deterministic=deterministic,
        label=STORED_LABEL,
        label_source="model",
        label_confidence=0.7,
        label_model="stored-model",
        label_candidates=[deterministic],
        labeled_signature=f"sig-{community_id}",
        labeled_at=None,
        label_attempted_at=None,
        refreshed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _corpus(project_id: str, eligible: int = 30) -> list[StoredCommunity]:
    """``eligible`` communities sized 3, 4, ... plus two below the ``member_count >= 3`` floor."""
    base = CORPUS_BASE[project_id]
    rows = [_community(project_id, base + i, i + 2) for i in range(1, eligible + 1)]
    return rows + [_community(project_id, base + 90 + i, 2) for i in range(2)]


class NamingAdapter:
    """Text generation that names each call uniquely and counts the calls."""

    def __init__(self) -> None:
        self.names: list[str] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        raise AssertionError(f"community labels must use JSON generation: {request.caller}")

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        name = f"Generated Group {len(self.names) + 1}"
        self.names.append(name)
        return {"name": name, "rationale": "Files share one purpose."}


def _generation(adapter: NamingAdapter) -> tuple[TextGenerationService, CodeIndexConfig]:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("label-model",),
            )
        ]
    )
    service = TextGenerationService(registry, {"endpoint:lm-studio": adapter})
    config = CodeIndexConfig(
        community_label=CodeIndexCommunityLabelConfig(
            enabled=True, candidates=["endpoint:lm-studio/label-model"]
        )
    )
    return service, config


@dataclass
class Prepared:
    code: int
    out: Path
    adapter: NamingAdapter
    corpora: dict[str, list[StoredCommunity]]

    @property
    def sheet(self) -> Path:
        return self.out / "sheet.csv"

    def rows(self) -> list[dict[str, str]]:
        with self.sheet.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == SHEET_COLUMNS
            return list(reader)

    def key(self) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = json.loads(
            (self.out / "key.json").read_text(encoding="utf-8")
        )
        return loaded

    def community(self, community_id: int) -> StoredCommunity:
        return next(
            c for rows in self.corpora.values() for c in rows if c.community_id == community_id
        )


def _prepare(
    tmp_path: Path,
    name: str,
    *extra: str,
    corpora: dict[str, list[StoredCommunity]] | None = None,
) -> Prepared:
    corpora = corpora or {GOBBY: _corpus(GOBBY), C1: _corpus(C1)}
    adapter = NamingAdapter()
    out = tmp_path / name
    code = community_label_study.main(
        ["prepare", *decision_flags(), "--out", str(out), *extra],
        load_communities=lambda project_id: corpora[project_id],
        generation=_generation(adapter),
        resolve_secret=resolve_secret,
    )
    return Prepared(code, out, adapter, corpora)


def _capture(tmp_path: Path, wire: OpenRouterWire, *, cost_per_token: float) -> Path:
    wire.cost_per_token = cost_per_token
    out = tmp_path / f"capture-{cost_per_token}.md"
    code = decisions_live_capture.main(
        [
            *decision_flags(),
            "--identity-contract",
            "response_version",
            "--backend-max-state-tokens",
            str(BACKEND_MAX_STATE_TOKENS),
            "--alias",
            ALIAS_MODEL,
            "--box-image",
            str(write_png(tmp_path)),
            "--out",
            str(out),
        ],
        resolve_secret=resolve_secret,
    )
    assert code == 0
    wire.cost_per_token = 1e-8
    return out


def _arms(entry: dict[str, Any]) -> set[str]:
    """The arms an item stands for: one arm, or several when D, G and J names coincide."""
    arm = entry["arms"] if "arms" in entry else entry["arm"]
    return {arm} if isinstance(arm, str) else {str(a) for a in arm}


def _rate(
    prepared: Prepared,
    rater: Rater,
    name: str = "rated.csv",
    *,
    accuracy_overrides: dict[str, str] | None = None,
) -> Path:
    key = prepared.key()
    overrides = accuracy_overrides or {}
    rated = prepared.out / name
    with rated.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        for row in prepared.rows():
            accuracy, findability = rater(key[row["item_id"]], row["name"])
            writer.writerow(
                row
                | {
                    "accuracy (0-2)": overrides.get(row["item_id"], str(accuracy)),
                    "findability (0-2)": str(findability),
                }
            )
    return rated


def _score(prepared: Prepared, sheet: Path, capture: Path) -> tuple[int, str]:
    out = prepared.out / f"results-{sheet.stem}-{capture.stem}.md"
    code = community_label_study.main(
        [
            "score",
            "--sheet",
            str(sheet),
            "--key",
            str(prepared.out / "key.json"),
            "--capture",
            str(capture),
            "--out",
            str(out),
        ]
    )
    return code, out.read_text(encoding="utf-8") if out.exists() else ""


SHIP_LINE = re.compile(r"((?:ai|code_index)\.[A-Za-z_.]+)`?\s*[:=]\s*`?\"?([A-Za-z0-9_:./-]+)")


def _ship(results: str) -> dict[str, str]:
    """The ship configuration as dotted keys, one ``key: value`` per line."""
    return {match[1]: match[2] for match in SHIP_LINE.finditer(results)}


def _off_everywhere(enabled: str) -> dict[str, str]:
    return {
        "code_index.community_label.enabled": enabled,
        "ai.decisions.community_label.mode": "off",
        "ai.decisions.community_label_carry.mode": "off",
        "ai.decisions.community_label_sufficiency.mode": "off",
    }


def _enforced(consumer: str) -> dict[str, str]:
    prefix = f"ai.decisions.{consumer}"
    return {
        f"{prefix}.mode": "enforce",
        f"{prefix}.evaluated_model": REQUEST_MODEL,
        f"{prefix}.evaluated_backend": BACKEND,
    }


def _assert_ships(results: str, expected: dict[str, str]) -> None:
    ship = _ship(results)
    assert {key: ship.get(key) for key in expected} == expected, results


def _admit_odd(key: str) -> tuple[str, float]:
    """Admit odd community ids (and any probe key); reject even ids below the threshold."""
    match = re.fullmatch(r"c(\d+)", key)
    rejected = match is not None and int(match[1]) % 2 == 0
    return ("generated", 0.3) if rejected else ("generated", 0.9)


def _rule_two_ratings(entry: dict[str, Any], _name: str) -> Rating:
    """Admitted G names beat D; rejected G names lose to D, which J then shows."""
    arms = _arms(entry)
    if arms == {"carry"}:
        return (2, 2)
    admitted = entry["community"] % 2 == 1
    if admitted:
        return (2, 2) if "G" in arms else (0, 0)
    return (1, 1) if "D" in arms else (0, 0)


def _flat_ratings(entry: dict[str, Any], _name: str) -> Rating:
    return (2, 2)


def _fresh_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decisions, "_service_cache", None)


def test_prepare_stratifies_and_refuses_short_corpus(
    tmp_path: Path, openrouter_wire: OpenRouterWire, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _prepare(tmp_path, "first", "--no-decisions")
    second = _prepare(tmp_path, "second", "--no-decisions")

    assert (first.code, second.code) == (0, 0)
    sampled = [
        {entry["community"] for entry in run.key().values() if "D" in _arms(entry)}
        for run in (first, second)
    ]
    assert sampled[0] == sampled[1]
    for project_id, base in CORPUS_BASE.items():
        ids = sorted(i for i in sampled[0] if base < i <= base + 30)
        assert len(ids) == 15, project_id
        terciles = Counter((i - base - 1) // 10 for i in ids)
        assert terciles == {0: 5, 1: 5, 2: 5}, project_id
    assert len(sampled[0]) == 30
    assert openrouter_wire.posts() == []

    short = _prepare(
        tmp_path,
        "short",
        "--no-decisions",
        corpora={GOBBY: _corpus(GOBBY), C1: _corpus(C1, eligible=14)},
    )
    assert short.code != 0
    captured = capsys.readouterr()
    assert "14" in captured.out + captured.err
    assert not short.sheet.exists()


def test_arms_share_generations_and_abort_on_shadow(
    tmp_path: Path, openrouter_wire: OpenRouterWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, "shared")
    snapshot = copy.deepcopy(prepared.corpora)

    assert prepared.code == 0
    # 30 main communities, generated once for G and J; every carry copy is kept.
    assert len(prepared.adapter.names) == 30
    assert prepared.corpora == snapshot
    assert {"choice", "noul", "score"} <= openrouter_wire.kinds_sent()
    assert STORED_LABEL not in prepared.sheet.read_text(encoding="utf-8")

    _fresh_decisions(monkeypatch)
    openrouter_wire.response_model = lambda sent: (
        MOVED_MODEL
        if any(q["type"] == "noul" for q in sent["questions"].values())
        else RESPONSE_MODEL
    )
    aborted = _prepare(tmp_path, "carry-shadow")
    assert aborted.code != 0
    assert not aborted.sheet.exists()


def test_sheet_is_blinded_and_key_is_separate(
    tmp_path: Path, openrouter_wire: OpenRouterWire
) -> None:
    seen: list[str] = []

    def alternate(key: str) -> float:
        if key not in seen:
            seen.append(key)
        return 0.9 if seen.index(key) % 2 == 0 else 0.1

    openrouter_wire.noul = alternate
    prepared = _prepare(tmp_path, "blinded")
    assert prepared.code == 0
    rows = prepared.rows()
    key = prepared.key()
    text = prepared.sheet.read_text(encoding="utf-8")

    assert {row["item_id"] for row in rows} == set(key)
    assert len(rows) <= 120
    for hidden in (GOBBY, C1, "carry", STORED_LABEL, "kept", "regenerated"):
        assert hidden not in text, hidden

    names = {row["item_id"]: row["name"] for row in rows}
    main: dict[int, dict[frozenset[str], str]] = {}
    for item_id, entry in key.items():
        if _arms(entry) <= MAIN_ARMS:
            main.setdefault(entry["community"], {})[frozenset(_arms(entry))] = names[item_id]
    assert len(main) == 30
    for community_id, items in main.items():
        community = prepared.community(community_id)
        assert items[frozenset({"D"})] == community.label_deterministic
        assert items[frozenset({"G", "J"})].startswith("Generated Group ")

    carry = {item_id: entry for item_id, entry in key.items() if _arms(entry) == {"carry"}}
    assert len(carry) == 30
    assert {entry["carry_outcome"] for entry in carry.values()} == {"kept", "regenerated"}
    assert Counter(entry["perturbation"] for entry in carry.values()) == {"mild": 16, "heavy": 14}
    for item_id, entry in carry.items():
        community = prepared.community(entry["community"])
        assert names[item_id] == main[community.community_id][frozenset({"G", "J"})]
        row = next(row for row in rows if row["item_id"] == item_id)
        members = set(MEMBER_PATH.findall(row["members"]))
        original = set(community.members)
        size = community.member_count
        if entry["perturbation"] == "mild":
            assert members < original
            assert len(original - members) == max(1, size // 10)
            assert (original - members).isdisjoint(community.representatives)
        else:
            added = members - original
            donors = {
                c.community_id
                for c in prepared.corpora[community.project_id]
                if added & set(c.members)
            }
            assert len(donors) == 1
            donor = prepared.community(donors.pop())
            assert len(added) == len(original - members) == min(size // 2, donor.member_count)
    regenerated = set(prepared.adapter.names[30:])
    assert regenerated
    assert regenerated.isdisjoint(names.values())


def test_score_computes_bars_and_rejects_incomplete_sheet(
    tmp_path: Path,
    openrouter_wire: OpenRouterWire,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cheap = _capture(tmp_path, openrouter_wire, cost_per_token=1e-8)
    costly = _capture(tmp_path, openrouter_wire, cost_per_token=1.0)
    _fresh_decisions(monkeypatch)
    openrouter_wire.choice = _admit_odd
    prepared = _prepare(tmp_path, "bars")
    assert prepared.code == 0
    rated = _rate(prepared, _rule_two_ratings)

    # Every gate bar passes on cheap decisions; the cost bar alone fails on costly ones.
    code, results = _score(prepared, rated, cheap)
    assert code == 0
    _assert_ships(results, _enforced("community_label"))
    code, results = _score(prepared, rated, costly)
    assert code == 0
    _assert_ships(results, _off_everywhere("false"))

    rows = prepared.rows()
    target = rows[len(rows) // 2]["item_id"]
    for bad, label in (("", "blank"), ("3", "out-of-range")):
        sheet = _rate(prepared, _rule_two_ratings, f"{label}.csv", accuracy_overrides={target: bad})
        code, _ = _score(prepared, sheet, cheap)
        captured = capsys.readouterr()
        assert code != 0, label
        assert target in captured.out + captured.err, label

    # No position reaches min_score, so sufficiency has no predicted positive: its bar
    # fails instead of dividing by zero, while carry still passes its own bars.
    _fresh_decisions(monkeypatch)
    openrouter_wire.choice = lambda _key: ("generated", 0.9)
    openrouter_wire.score_top = False
    flat = _prepare(tmp_path, "undefined")
    assert flat.code == 0
    code, results = _score(flat, _rate(flat, _flat_ratings), cheap)
    assert code == 0
    _assert_ships(
        results,
        {
            "code_index.community_label.enabled": "true",
            "ai.decisions.community_label.mode": "off",
            "ai.decisions.community_label_sufficiency.mode": "off",
            **_enforced("community_label_carry"),
        },
    )


def test_j_arm_enforces_with_alias_request_and_aborts_on_backend_change(
    tmp_path: Path, openrouter_wire: OpenRouterWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    openrouter_wire.choice = _admit_odd
    prepared = _prepare(tmp_path, "alias")

    assert prepared.code == 0
    assert {body["model"] for body in openrouter_wire.posts()} == {REQUEST_MODEL}
    names = {row["item_id"]: row["name"] for row in prepared.rows()}
    j_names = {
        entry["community"]: names[item_id]
        for item_id, entry in prepared.key().items()
        if "J" in _arms(entry)
    }
    assert len(j_names) == 30
    for community_id, name in j_names.items():
        deterministic = prepared.community(community_id).label_deterministic
        assert (name == deterministic) is (community_id % 2 == 0), community_id
    record = (prepared.out / "run.json").read_text(encoding="utf-8")
    assert REQUEST_MODEL in record
    assert BACKEND in record

    def gate_request(sent: dict[str, Any]) -> bool:
        return any(
            q["type"] == "choice" and re.fullmatch(r"c\d+", key)
            for key, q in sent["questions"].items()
        )

    _fresh_decisions(monkeypatch)
    openrouter_wire.response_model = lambda sent: (
        MOVED_MODEL if gate_request(sent) else RESPONSE_MODEL
    )
    moved = _prepare(tmp_path, "moved")
    assert moved.code != 0
    assert not moved.sheet.exists()


def test_ship_configuration_follows_each_branch(
    tmp_path: Path, openrouter_wire: OpenRouterWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _capture(tmp_path, openrouter_wire, cost_per_token=1e-8)

    # Rule 1: G never regresses, so the gate ships off; carry and sufficiency pass their bars.
    _fresh_decisions(monkeypatch)
    rule_one = _prepare(tmp_path, "rule-one")
    assert rule_one.code == 0
    code, results = _score(rule_one, _rate(rule_one, _flat_ratings), capture)
    assert code == 0
    _assert_ships(
        results,
        {
            "code_index.community_label.enabled": "true",
            "ai.decisions.community_label.mode": "off",
            **_enforced("community_label_carry"),
            **_enforced("community_label_sufficiency"),
        },
    )

    # Rule 2: G regresses, J passes every gate bar; sufficiency fails its precision bar.
    _fresh_decisions(monkeypatch)
    openrouter_wire.choice = _admit_odd
    rule_two = _prepare(tmp_path, "rule-two")
    assert rule_two.code == 0
    code, results = _score(rule_two, _rate(rule_two, _rule_two_ratings), capture)
    assert code == 0
    _assert_ships(
        results,
        {
            "code_index.community_label.enabled": "true",
            **_enforced("community_label"),
            **_enforced("community_label_carry"),
            "ai.decisions.community_label_sufficiency.mode": "off",
        },
    )

    # Rule 3 (D5): G regresses and J shows no lift, so every stage ships off.
    def no_lift(entry: dict[str, Any], name: str) -> Rating:
        if _arms(entry) != {"carry"} and entry["community"] % 2 == 1:
            return (1, 1)
        return _rule_two_ratings(entry, name)

    code, results = _score(rule_two, _rate(rule_two, no_lift, "no-lift.csv"), capture)
    assert code == 0
    _assert_ships(results, _off_everywhere("false"))

    # --no-decisions: rule 2 is unreachable; rule 1 or D5 decides ``enabled``.
    _fresh_decisions(monkeypatch)
    bare = _prepare(tmp_path, "bare", "--no-decisions")
    assert bare.code == 0
    code, results = _score(bare, _rate(bare, _flat_ratings), capture)
    assert code == 0
    _assert_ships(results, _off_everywhere("true"))

    def g_regresses(entry: dict[str, Any], _name: str) -> Rating:
        return (1, 1) if "D" in _arms(entry) else (0, 0)

    code, results = _score(bare, _rate(bare, g_regresses, "regressed.csv"), capture)
    assert code == 0
    _assert_ships(results, _off_everywhere("false"))


def test_missing_capture_cost_fails_cost_bar(
    tmp_path: Path, openrouter_wire: OpenRouterWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _capture(tmp_path, openrouter_wire, cost_per_token=1e-8)
    text = capture.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    assert match is not None
    payload = json.loads(match[1])
    for probe in payload["probes"]:
        probe.get("usage", {}).pop("cost", None)
    capture.write_text(
        text[: match.start(1)] + json.dumps(payload) + text[match.end(1) :], encoding="utf-8"
    )
    _fresh_decisions(monkeypatch)
    openrouter_wire.choice = _admit_odd
    prepared = _prepare(tmp_path, "missing-cost")
    assert prepared.code == 0

    code, results = _score(prepared, _rate(prepared, _rule_two_ratings), capture)

    assert code == 0
    assert "| decision_cost_usd | undefined |" in results
    _assert_ships(results, _off_everywhere("false"))


def test_live_loader_scopes_machine_and_omits_stored_labels(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = require_machine_id()
    foreign = "21000000-0000-4000-8000-999999999999"
    storage = CodeIndexStorage(temp_db)
    storage.upsert_project_stats(
        IndexedProject(id=PROJECT_ID, root_path=str(tmp_path)), mode=IndexWriteMode.OVERLAY
    )
    temp_db.execute(
        "INSERT INTO machines (id, owner_user_id) SELECT %s, owner_user_id "
        "FROM machines WHERE id = %s",
        (foreign, local),
    )
    for machine, cid in ((local, 1), (foreign, 2)):
        temp_db.execute(
            "INSERT INTO code_communities (machine_id, project_id, community_id, member_count, "
            "members, representatives, internal_edges, cohesion, boundary, member_signature, "
            "label_deterministic, label, label_source, label_candidates, label_model, "
            "labeled_signature) VALUES (%s, %s, %s, 3, %s, %s, 2, 0.5, '[]'::jsonb, %s, "
            "%s, %s, 'model', %s, 'stored-model', %s)",
            (
                machine,
                PROJECT_ID,
                cid,
                ["a.py", "b.py", "c.py"],
                ["a.py"],
                f"{cid:016x}",
                "Deterministic Area",
                STORED_LABEL,
                ["Deterministic Area"],
                f"{cid:016x}",
            ),
        )
    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database", lambda **_kwargs: nullcontext(temp_db)
    )

    rows = community_label_study._load_communities(PROJECT_ID)

    assert [(row.machine_id, row.community_id) for row in rows] == [(local, 1)]
    assert rows[0].label == rows[0].label_source == ""
    assert rows[0].label_model is rows[0].labeled_signature is None
    assert rows[0].members == ["a.py", "b.py", "c.py"]
