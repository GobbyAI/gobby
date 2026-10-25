"""Label-quality study: prepare the blinded sheet, then score it (community-label-decisions 3.3)."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import random
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from gobby.ai.decisions import ChoiceQuestion, DecisionService, DecisionsUnavailable
from gobby.ai.text_generation import TextGenerationService, build_daemon_text_generation_service
from gobby.code_index.community_labeler import (
    CommunityLabeler,
    CommunityLabelOutcome,
    GeneratedLabel,
)
from gobby.code_index.models import StoredCommunity
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.ai import (
    ChoiceConsumerConfig,
    DecisionsConfig,
    RerankConsumerConfig,
    ScoreConsumerConfig,
)
from gobby.config.code_index import CodeIndexConfig

SEED = 22604
CORPORA = {
    "gobby": "d45545c5-ded5-4335-b115-0245752edacf",
    "C1": "c8e071af-e1f7-434f-b3c0-f31a1b1299c9",
}
SHEET_COLUMNS = ["item_id", "members", "name", "accuracy (0-2)", "findability (0-2)"]
type Arm = Literal["D", "G", "J", "carry"]
type Generation = GeneratedLabel | CommunityLabelOutcome
type Cache = dict[tuple[str, int, str], Generation]
type Rating = tuple[int, int]


class LabelWrite(BaseModel):
    label: str
    label_source: str
    label_confidence: float | None = None
    label_model: str | None = None
    labeled_signature: str


class KeyEntry(BaseModel):
    corpus: str
    community: int
    arms: list[Arm]
    label_confidence: float | None = None
    perturbation: Literal["mild", "heavy"] | None = None
    carry_outcome: str | None = None
    probability: float | None = None
    regenerated_name: str | None = None


class MainResult(BaseModel):
    corpus: str
    community: int
    names: dict[str, str]
    g_outcome: str
    j_outcome: str | None = None
    label_confidence: float | None = None
    position: float | None = None


class RunRecord(BaseModel):
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    seed: int = SEED
    no_decisions: bool
    reason: str | None = None
    evaluated_model: str | None = None
    evaluated_backend: str | None = None
    min_score: float = 0.5
    probe: list[dict[str, Any]] = Field(default_factory=list)
    samples: dict[str, list[int]] = Field(default_factory=dict)
    main: list[MainResult] = Field(default_factory=list)
    carry_skipped: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)
    event_counts: dict[str, int] = Field(default_factory=dict)
    decision_usage: list[dict[str, Any]] = Field(default_factory=list)


class RecordingStorage:
    """The labeler's write seam; this object never holds a live database."""

    def __init__(self) -> None:
        self.writes: dict[int, LabelWrite] = {}

    def update_community_label(
        self, project_id: str, community_id: int, signature: str, **fields: Any
    ) -> bool:
        self.writes[community_id] = LabelWrite.model_validate(fields)
        return True

    def mark_community_labels_attempted(self, attempts: Sequence[tuple[str, int, str]]) -> int:
        return len(attempts)


async def _run_memory[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return function(*args, **kwargs)


class MemoLabeler(CommunityLabeler):
    """Reuse exactly the same generation, including a failure, across study arms."""

    def __init__(
        self,
        generation: tuple[TextGenerationService, CodeIndexConfig],
        decisions: DecisionsConfig | None,
        cache: Cache,
    ) -> None:
        super().__init__(*generation, decisions_resolver=lambda: decisions)
        self.cache = cache

    async def generate_batch(self, communities: Sequence[StoredCommunity]) -> dict[int, Generation]:
        missing = [c for c in communities if self._key(c) not in self.cache]
        generated = await super().generate_batch(missing)
        for community in missing:
            self.cache[self._key(community)] = generated[community.community_id]
        return {c.community_id: self.cache[self._key(c)] for c in communities}

    @staticmethod
    def _key(community: StoredCommunity) -> tuple[str, int, str]:
        return community.project_id, community.community_id, community.member_signature


class StudyEvents(logging.Handler):
    """Collect the product's emitted decision evidence, scoped to one CLI run."""

    def __init__(self, run: RunRecord) -> None:
        super().__init__(logging.DEBUG)
        self.run = run
        self.arm = "probe"
        self.shadow = False
        self.loggers = [
            logging.getLogger("gobby.code_index.community_labeler"),
            logging.getLogger("gobby.ai.decisions"),
        ]
        self.levels = [logger.level for logger in self.loggers]

    def __enter__(self) -> StudyEvents:
        for logger in self.loggers:
            logger.setLevel(logging.DEBUG)
            logger.addHandler(self)
        return self

    def __exit__(self, *_: object) -> None:
        for logger, level in zip(self.loggers, self.levels, strict=True):
            logger.removeHandler(self)
            logger.setLevel(level)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("decisions.call "):
            fields = dict(re.findall(r"(\w+)=([^ ]+)", message))
            self.run.decision_usage.append(
                {**fields, "arm": self.arm, "estimated_tokens": int(fields["estimated_tokens"])}
            )
        elif message.startswith("code_index.community_label."):
            fields = {
                key: getattr(record, key)
                for key in (
                    "project_id",
                    "community_id",
                    "member_signature",
                    "outcome",
                    "mode",
                    "probability",
                    "position",
                    "decision_mode",
                    "decision_confidence",
                    "outcomes",
                )
                if hasattr(record, key)
            }
            self.run.events.append({"event": message, "arm": self.arm, **fields})
            if self.arm in {"J", "carry"} and "shadow" in (
                fields.get("mode"),
                fields.get("decision_mode"),
            ):
                self.shadow = True
        elif "evaluated_backend_mismatch" in message and self.arm in {"J", "carry"}:
            self.shadow = True


def _load_communities(project_id: str) -> list[StoredCommunity]:
    from gobby.storage.hub.runtime import runtime_hub_database
    from gobby.utils.machine_id import require_machine_id

    # Select only structural data. Stored names and their provenance never enter the study.
    with runtime_hub_database(apply_migrations=False) as db, db.transaction() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute(
            "SELECT machine_id, project_id, community_id, member_count, members, representatives, "
            "internal_edges, cohesion, boundary, member_signature, label_deterministic, "
            "label_candidates, refreshed_at FROM code_communities "
            "WHERE machine_id = %s AND project_id = %s",
            (require_machine_id(), project_id),
        ).fetchall()
    return [
        StoredCommunity.from_row(
            dict(
                row,
                label="",
                label_source="",
                label_confidence=None,
                label_model=None,
                labeled_signature=None,
                labeled_at=None,
                label_attempted_at=None,
            )
        )
        for row in rows
    ]


def _load_generation() -> tuple[TextGenerationService, CodeIndexConfig]:
    from gobby.storage.config_repository import ConfigRepository
    from gobby.storage.hub.runtime import runtime_hub_database

    with runtime_hub_database(apply_migrations=False) as db:
        repository = ConfigRepository(db)
        snapshot = repository.read()
        config = repository.runtime_candidate(dict(snapshot.overrides), snapshot.secret_bindings)
    return build_daemon_text_generation_service(config), config.code_index


def _sample(loader: Callable[[str], Sequence[StoredCommunity]]) -> dict[str, list[StoredCommunity]]:
    rng = random.Random(SEED)
    eligible = {
        corpus: sorted(
            (c for c in loader(project) if c.member_count >= 3),
            key=lambda c: (c.member_count, c.community_id),
        )
        for corpus, project in CORPORA.items()
    }
    if any(len(rows) < 15 for rows in eligible.values()):
        raise ValueError(
            f"Need 15 eligible communities per corpus; counts: "
            f"{ {name: len(rows) for name, rows in eligible.items()} }"
        )
    sampled: dict[str, list[StoredCommunity]] = {}
    for corpus, rows in eligible.items():
        draws = [
            c
            for i in range(3)
            for c in rng.sample(rows[i * len(rows) // 3 : (i + 1) * len(rows) // 3], 5)
        ]
        sampled[corpus] = [
            replace(
                c,
                label="",
                label_source="",
                label_confidence=None,
                label_model=None,
                labeled_signature=None,
                labeled_at=None,
                label_attempted_at=None,
            )
            for c in draws
        ]
    return sampled


async def _probe(config: DecisionsConfig, run: RunRecord) -> str:
    def observe(response: httpx.Response) -> None:
        run.probe.append(
            {
                "request": json.loads(response.request.content),
                "response": response.json(),
                "status": response.status_code,
            }
        )

    result = await DecisionService(config, exchange_observer=observe).choose(
        "community_label_study_probe",
        {"purpose": "Pin the study backend identity"},
        {"probe": ChoiceQuestion("Choose generated.", {"generated": "yes", "other": "no"})},
    )
    if result.backend_identity is None:
        raise ValueError("Probe returned no backend identity")
    return result.backend_identity


async def _arm(
    name: str,
    communities: Sequence[StoredCommunity],
    generation: tuple[TextGenerationService, CodeIndexConfig],
    config: DecisionsConfig | None,
    cache: Cache,
    events: StudyEvents,
) -> tuple[RecordingStorage, dict[int, CommunityLabelOutcome]]:
    events.arm = name
    storage = RecordingStorage()
    outcomes = await MemoLabeler(generation, config, cache).label_batch(
        communities[0].project_id,
        communities,
        storage=cast(CodeIndexStorage, storage),
        run_db=_run_memory,
    )
    if events.shadow:
        raise ValueError(f"{name} ran as shadow: evaluated backend changed; no sheet written")
    return storage, outcomes


def _perturb(
    community: StoredCommunity,
    donor: StoredCommunity,
    position: int,
    written: LabelWrite,
    rng: random.Random,
) -> tuple[StoredCommunity, Literal["mild", "heavy"]]:
    members = list(community.members)
    if position % 2:
        kind: Literal["mild", "heavy"] = "mild"
        count = max(1, community.member_count // 10)
        nonrepresentatives = sorted(set(members) - set(community.representatives))
        removed = set(rng.sample(nonrepresentatives, min(count, len(nonrepresentatives))))
        removed.update(rng.sample(sorted(set(members) - removed), count - len(removed)))
        members = [member for member in members if member not in removed]
    else:
        kind = "heavy"
        available = sorted(set(donor.members) - set(members))
        count = min(community.member_count // 2, len(available))
        removed = set(rng.sample(members, count))
        members = [member for member in members if member not in removed]
        members.extend(rng.sample(available, count))
    return replace(
        community,
        members=members,
        member_count=len(members),
        representatives=[member for member in community.representatives if member in members],
        member_signature=community.member_signature + ":carry",
        label=written.label,
        label_source="model",
        label_confidence=written.label_confidence,
        label_model=written.label_model,
        labeled_signature=community.member_signature,
    ), kind


def _event(run: RunRecord, arm: str, community: StoredCommunity, suffix: str) -> dict[str, Any]:
    return next(
        (
            event
            for event in reversed(run.events)
            if event["arm"] == arm
            and event["event"].endswith("." + suffix)
            and event.get("project_id") == community.project_id
            and event.get("community_id") == community.community_id
        ),
        {},
    )


async def _prepare(
    args: argparse.Namespace,
    loader: Callable[[str], Sequence[StoredCommunity]],
    generation: tuple[TextGenerationService, CodeIndexConfig] | None,
    resolve_secret: Callable[[str], str] | None,
) -> None:
    from scripts.decisions_live_capture import build_decisions_config

    sampled = _sample(loader)
    generation = generation or _load_generation()
    run = RunRecord(
        no_decisions=args.no_decisions,
        reason="Operator selected --no-decisions; OpenRouter checklist unmet"
        if args.no_decisions
        else None,
    )
    run.samples = {corpus: [c.community_id for c in rows] for corpus, rows in sampled.items()}
    cache: Cache = {}
    items: list[tuple[KeyEntry, list[str], str]] = []
    carry_copies: list[tuple[str, StoredCommunity, Literal["mild", "heavy"], LabelWrite]] = []
    rng = random.Random(SEED)
    with StudyEvents(run) as events:
        config = None
        if not args.no_decisions:
            if args.wire_api != "openrouter-decisions":
                raise ValueError("The J study arm requires --wire-api openrouter-decisions")
            config = build_decisions_config(args, resolve_secret)
            run.evaluated_model = config.model
            run.evaluated_backend = await _probe(config, run)
        pair = {"evaluated_model": run.evaluated_model, "evaluated_backend": run.evaluated_backend}
        for corpus, communities in sampled.items():
            g, g_outcomes = await _arm("G", communities, generation, None, cache, events)
            j = RecordingStorage()
            j_outcomes: dict[int, CommunityLabelOutcome] = {}
            if config is not None:
                j_config = config.model_copy(
                    update={"community_label": ChoiceConsumerConfig(mode="enforce", **pair)}
                )
                j, j_outcomes = await _arm("J", communities, generation, j_config, cache, events)
                suff_config = config.model_copy(
                    update={
                        "community_label_sufficiency": ScoreConsumerConfig(mode="shadow", **pair)
                    }
                )
                await _arm("sufficiency", communities, generation, suff_config, cache, events)
            for position, community in enumerate(communities, 1):
                cid = community.community_id
                names = {
                    "D": community.label_deterministic,
                    "G": g.writes[cid].label if cid in g.writes else community.label_deterministic,
                }
                confidence = j.writes[cid].label_confidence if cid in j.writes else None
                if config is not None:
                    names["J"] = (
                        j.writes[cid].label if cid in j.writes else community.label_deterministic
                    )
                run.main.append(
                    MainResult(
                        corpus=corpus,
                        community=cid,
                        names=names,
                        g_outcome=g_outcomes[cid],
                        j_outcome=j_outcomes.get(cid),
                        label_confidence=confidence,
                        position=_event(run, "sufficiency", community, "sufficiency").get(
                            "position"
                        ),
                    )
                )
                for name in dict.fromkeys(names.values()):
                    arms = [cast(Arm, arm) for arm, label in names.items() if label == name]
                    items.append(
                        (
                            KeyEntry(
                                corpus=corpus,
                                community=cid,
                                arms=arms,
                                label_confidence=confidence if "J" in arms else None,
                            ),
                            community.members,
                            name,
                        )
                    )
                if config is None:
                    continue
                written = g.writes.get(cid)
                if written is None or written.label_source != "model":
                    run.carry_skipped += 1
                    continue
                copy, kind = _perturb(
                    community, communities[position % len(communities)], position, written, rng
                )
                carry_copies.append((corpus, copy, kind, written))
        # Finish all main generations before generating any perturbed copy.
        for corpus, copy, kind, written in carry_copies:
            assert config is not None
            carry_config = config.model_copy(
                update={"community_label_carry": RerankConsumerConfig(mode="enforce", **pair)}
            )
            carry, _ = await _arm("carry", [copy], generation, carry_config, cache, events)
            evidence = _event(run, "carry", copy, "carry")
            regenerated = carry.writes.get(copy.community_id)
            items.append(
                (
                    KeyEntry(
                        corpus=corpus,
                        community=copy.community_id,
                        arms=["carry"],
                        label_confidence=written.label_confidence,
                        perturbation=kind,
                        carry_outcome=evidence.get("outcome"),
                        probability=evidence.get("probability"),
                        regenerated_name=regenerated.label
                        if regenerated and evidence.get("outcome") != "kept"
                        else None,
                    ),
                    copy.members,
                    written.label,
                )
            )
    run.event_counts = dict(
        Counter(f"{e['arm']}:{e['event']}:{e.get('outcome', 'batch')}" for e in run.events)
    )
    random.Random(SEED).shuffle(items)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    keys = {}
    with (out / "sheet.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SHEET_COLUMNS)
        for index, (key, members, name) in enumerate(items, 1):
            item_id = f"item-{index:03d}"
            keys[item_id] = key.model_dump()
            writer.writerow((item_id, "\n".join(members), name, "", ""))
    (out / "key.json").write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    (out / "run.json").write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _classification(population: list[tuple[bool, bool]]) -> tuple[float | None, float | None]:
    true_positive = sum(actual and predicted for actual, predicted in population)
    return (
        _ratio(true_positive, sum(predicted for _, predicted in population)),
        _ratio(true_positive, sum(actual for actual, _ in population)),
    )


def _ratings(sheet: Path, key: dict[str, KeyEntry]) -> dict[str, Rating]:
    ratings: dict[str, Rating] = {}
    with sheet.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            item = row.get("item_id", "<missing item_id>")
            if item not in key or item in ratings:
                raise ValueError(f"Unknown or duplicate item: {item}")
            values = (row.get("accuracy (0-2)"), row.get("findability (0-2)"))
            if any(value not in {"0", "1", "2"} for value in values):
                raise ValueError(f"Invalid or incomplete rating for {item}: {values}")
            ratings[item] = (int(cast(str, values[0])), int(cast(str, values[1])))
    missing = key.keys() - ratings.keys()
    if missing:
        raise ValueError(f"Missing ratings: {', '.join(sorted(missing))}")
    return ratings


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _cost(capture: Path, run: RunRecord) -> float | None:
    matches = re.findall(r"```json\s*(.*?)```", capture.read_text(encoding="utf-8"), re.DOTALL)
    if not matches:
        return None
    data = json.loads(matches[-1])
    if not isinstance(data, dict) or data.get("wire_api") != "openrouter-decisions":
        return None
    ratio = _finite_number(data.get("measured_chars_per_token"))
    if ratio is None or ratio <= 0:
        return None
    probes = data.get("probes")
    if not isinstance(probes, list):
        return None
    tokens = cost = 0.0
    for probe in probes:
        if not isinstance(probe, dict) or probe.get("status") != 200:
            continue
        usage = probe.get("usage")
        if not isinstance(usage, dict):
            return None
        count = _finite_number(usage.get("input_tokens"))
        charge = _finite_number(usage.get("cost"))
        if count is None or charge is None or count <= 0 or charge < 0:
            return None
        tokens += count
        cost += charge
    if not tokens or not run.decision_usage:
        return None
    result = (
        sum(item["estimated_tokens"] for item in run.decision_usage) * (4 / ratio) * cost / tokens
    )
    return result if math.isfinite(result) and result >= 0 else None


def _score(args: argparse.Namespace) -> None:
    key_path = Path(args.key)
    key = TypeAdapter(dict[str, KeyEntry]).validate_json(key_path.read_text(encoding="utf-8"))
    run = RunRecord.model_validate_json(key_path.with_name("run.json").read_text(encoding="utf-8"))
    ratings = _ratings(Path(args.sheet), key)
    arms: dict[tuple[str, int, str], Rating] = {}
    carry_population = []
    for item, entry in key.items():
        for arm in entry.arms:
            if arm != "carry":
                arm_identity = (entry.corpus, entry.community, arm)
                if arm_identity in arms:
                    raise ValueError(f"Duplicate arm in key for {item}")
                arms[arm_identity] = ratings[item]
        if entry.carry_outcome in {"kept", "regenerated"}:
            carry_population.append((ratings[item][0] == 2, entry.carry_outcome == "kept"))
    if len(run.main) != 30:
        raise ValueError("Scoring requires all 30 main communities")
    lift_a: list[int] = []
    lift_f: list[int] = []
    admitted: list[float] = []
    rejected: list[float] = []
    gate_population, suff_population = [], []
    g_regressions = j_regressions = schema_rejections = 0
    for community in run.main:
        identity = (community.corpus, community.community)
        try:
            d, g = arms[(*identity, "D")], arms[(*identity, "G")]
            j = arms[(*identity, "J")] if not run.no_decisions else None
        except KeyError as exc:
            raise ValueError(f"Missing study arm for {identity}") from exc
        g_regressions += sum(g) < sum(d)
        schema_rejections += community.g_outcome == "schema_rejected"
        if j is not None:
            lift_a.append(j[0] - d[0])
            lift_f.append(j[1] - d[1])
            j_regressions += sum(j) < sum(d)
        if community.j_outcome in {"gate_admitted", "gate_rejected"}:
            positive = community.j_outcome == "gate_admitted"
            gate_population.append((sum(g) >= sum(d), positive))
            if community.label_confidence is not None:
                (admitted if positive else rejected).append(community.label_confidence)
        if community.position is not None:
            suff_population.append((sum(d) >= sum(g), community.position >= run.min_score))
    gate_precision, gate_recall = _classification(gate_population)
    carry_precision, carry_recall = _classification(carry_population)
    suff_precision, suff_recall = _classification(suff_population)
    metrics: dict[str, float | None] = {
        "accuracy_lift": mean(lift_a) if lift_a else None,
        "findability_lift": mean(lift_f) if lift_f else None,
        "j_regressions": j_regressions if lift_a else None,
        "gate_precision": gate_precision,
        "gate_recall": gate_recall,
        "confidence_separation": mean(admitted) - mean(rejected) if admitted and rejected else None,
        "schema_rejection_rate": schema_rejections / 30,
        "decision_cost_usd": _cost(Path(args.capture), run) if not run.no_decisions else 0,
        "carry_precision": carry_precision,
        "carry_recall": carry_recall,
        "sufficiency_precision": suff_precision,
        "sufficiency_recall": suff_recall,
    }
    thresholds = {
        "accuracy_lift": (0.3, False),
        "findability_lift": (0.5, False),
        "j_regressions": (1, True),
        "gate_precision": (0.85, False),
        "gate_recall": (0.60, False),
        "confidence_separation": (0.15, False),
        "schema_rejection_rate": (0.10, True),
        "decision_cost_usd": (0.05, True),
        "carry_precision": (0.85, False),
        "carry_recall": (0.60, False),
        "sufficiency_precision": (0.85, False),
        "sufficiency_recall": (0.60, False),
    }
    passed = {
        name: value is not None and (value <= bound if ceiling else value >= bound)
        for name, (bound, ceiling) in thresholds.items()
        for value in [metrics[name]]
    }
    gate_passed = all(passed[name] for name in list(thresholds)[:8])
    rule = 1 if g_regressions <= 1 else (2 if gate_passed and not run.no_decisions else 3)
    config: dict[str, str] = {"code_index.community_label.enabled": str(rule != 3).lower()}
    for consumer, enabled in (
        ("community_label", rule == 2),
        ("community_label_carry", passed["carry_precision"] and passed["carry_recall"]),
        (
            "community_label_sufficiency",
            passed["sufficiency_precision"] and passed["sufficiency_recall"],
        ),
    ):
        enforce = enabled and rule != 3 and not run.no_decisions
        prefix = "ai.decisions." + consumer
        config[prefix + ".mode"] = "enforce" if enforce else "off"
        if enforce:
            if not run.evaluated_model or not run.evaluated_backend:
                raise ValueError("Missing evaluated request model/backend pair")
            config[prefix + ".evaluated_model"] = run.evaluated_model
            config[prefix + ".evaluated_backend"] = run.evaluated_backend
    lines = [
        "## Study results",
        "",
        f"Ship rule: {rule}. G regressions: {g_regressions}/30.",
        "",
        "| Metric | Value | Bar | Result |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, (bound, ceiling) in thresholds.items():
        value = metrics[name]
        rendered = "undefined" if value is None else f"{value:.6g}"
        lines.append(
            f"| {name} | {rendered} | {'<=' if ceiling else '>='} {bound} | "
            f"{'PASS' if passed[name] else 'FAIL'} |"
        )
    lines.extend(["", "### Ship configuration", "", "```yaml"])
    lines.extend(f"{name}: {value}" for name, value in config.items())
    lines.extend(["```", ""])
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")


def main(
    argv: list[str] | None = None,
    *,
    load_communities: Callable[[str], Sequence[StoredCommunity]] | None = None,
    generation: tuple[TextGenerationService, CodeIndexConfig] | None = None,
    resolve_secret: Callable[[str], str] | None = None,
) -> int:
    """Run ``prepare`` or ``score``."""
    from scripts.decisions_live_capture import add_decision_flags

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Prepare an isolated blinded study")
    add_decision_flags(prepare)
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--no-decisions", action="store_true")
    score = commands.add_parser("score", help="Score a completed blinded sheet")
    for flag in ("sheet", "key", "capture"):
        score.add_argument("--" + flag, required=True)
    score.add_argument("--out", default="label-quality.md")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            asyncio.run(
                _prepare(args, load_communities or _load_communities, generation, resolve_secret)
            )
        else:
            _score(args)
    except (ValueError, OSError, DecisionsUnavailable) as exc:
        print(f"Study failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Direct script execution exposes scripts/, whereas module execution exposes the repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
