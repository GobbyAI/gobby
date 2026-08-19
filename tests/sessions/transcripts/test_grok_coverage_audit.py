"""User-anchored coverage recompute over the four audited Grok stream shapes."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.memory.digest import _extract_digest_pairs
from gobby.memory.synthetic_prompts import synthetic_body_reason
from gobby.sessions.transcripts.grok import (
    GrokTranscriptParser,
    _extract_text,
    _extract_update,
)
from gobby.utils.injected_context import strip_injected_context
from tests.sessions.transcripts.fixtures.grok_streams import (
    session_10695_shape,
    session_10711_shape,
    session_10715_shape,
    session_10725_shape,
)

pytestmark = pytest.mark.unit

_AUDIT_DIR_ENV = "GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR"

# Builder-documented response-completeness (non-empty-response anchored pairs /
# anchored pairs). Vacuous 1.0 when there are no real-prompt anchors.
# 10695: 18 completed real prompts + 20 cancelled-before-output real prompts.
# 10715: marathon + 2 mid-turn injections + 3 small turns, all with output.
# 10725: no real prompts (empty-denominator branch).
# 10711: 5 completed real prompts.
_SESSION_10695_COMPLETENESS = 18 / 38
_SESSION_10715_COMPLETENESS = 1.0
_SESSION_10725_COMPLETENESS = 1.0
_SESSION_10711_COMPLETENESS = 1.0

_SHAPE_CASES: tuple[
    tuple[str, Callable[[], list[str]], int, float],
    ...,
] = (
    ("10695", session_10695_shape, 38, _SESSION_10695_COMPLETENESS),
    ("10715", session_10715_shape, 6, _SESSION_10715_COMPLETENESS),
    ("10725", session_10725_shape, 0, _SESSION_10725_COMPLETENESS),
    ("10711", session_10711_shape, 5, _SESSION_10711_COMPLETENESS),
)


@dataclass(frozen=True)
class UserAnchoredCoverage:
    """Coverage of real user prompts by digest-pair anchors."""

    real_prompts: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...]
    anchored: int
    complete_responses: int
    coverage: float
    completeness: float


def compute_user_anchored_coverage(
    parser: GrokTranscriptParser,
    records: list[dict[str, Any]],
) -> UserAnchoredCoverage:
    """Recompute the epic user-anchored coverage metric for one transcript."""
    real_prompts = tuple(_real_user_prompts(records))
    pairs = tuple(_extract_digest_pairs(parser, records))
    pair_prompt_counts = Counter(prompt for prompt, _response in pairs)
    real_set = set(real_prompts)
    anchored = sum(1 for prompt in real_prompts if pair_prompt_counts[prompt] > 0)
    anchored_pairs = [(prompt, response) for prompt, response in pairs if prompt in real_set]
    complete_responses = sum(1 for _prompt, response in anchored_pairs if response.strip())
    if not real_prompts:
        coverage = 1.0 if anchored == 0 else 0.0
    else:
        coverage = anchored / len(real_prompts)
    completeness = 1.0 if not anchored_pairs else complete_responses / len(anchored_pairs)
    return UserAnchoredCoverage(
        real_prompts=real_prompts,
        pairs=pairs,
        anchored=anchored,
        complete_responses=complete_responses,
        coverage=coverage,
        completeness=completeness,
    )


_VENDORED_AUDIT_ROOT = Path(__file__).parent / "fixtures" / "grok_audit"


def test_vendored_grok_audit_fixtures_match_builders() -> None:
    for name, builder, _expected_real, _expected_completeness in _SHAPE_CASES:
        path = _VENDORED_AUDIT_ROOT / name / "updates.jsonl"
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8") == "\n".join(builder()) + "\n"


def test_user_anchored_coverage_on_audited_shapes() -> None:
    parser = GrokTranscriptParser(session_id="grok-coverage-audit")
    for name, builder, expected_real, expected_completeness in _SHAPE_CASES:
        records = [_load_record(line) for line in builder()]
        metric = compute_user_anchored_coverage(parser, records)
        assert len(metric.real_prompts) == expected_real, name
        assert metric.anchored == expected_real, name
        if name == "10725":
            assert metric.real_prompts == ()
            assert metric.anchored == 0
            assert metric.coverage == 1.0
            assert metric.completeness == 1.0
        else:
            assert metric.coverage == 1.0, name
            pair_prompt_counts = Counter(prompt for prompt, _response in metric.pairs)
            for prompt in metric.real_prompts:
                assert pair_prompt_counts[prompt] == 1, name
        assert metric.completeness == expected_completeness, name


@pytest.mark.skipif(
    not os.environ.get(_AUDIT_DIR_ENV),
    reason=f"set {_AUDIT_DIR_ENV} to replay audited updates.jsonl files",
)
def test_real_transcript_replay_opt_in() -> None:
    root = Path(os.environ[_AUDIT_DIR_ENV]).expanduser()
    assert root.is_dir(), f"{_AUDIT_DIR_ENV} is not a directory: {root}"
    paths = sorted(path for path in root.rglob("updates.jsonl") if path.is_file())
    assert paths, f"no updates.jsonl files under {root}"
    parser = GrokTranscriptParser(session_id="grok-coverage-replay")
    for path in paths:
        records = _load_jsonl(path)
        metric = compute_user_anchored_coverage(parser, records)
        print(
            f"{path}: coverage={metric.coverage:.4f} "
            f"real_prompts={len(metric.real_prompts)} "
            f"anchored={metric.anchored} "
            f"completeness={metric.completeness:.4f}"
        )
        assert 0.0 <= metric.coverage <= 1.0
        assert 0.0 <= metric.completeness <= 1.0


def _real_user_prompts(records: list[dict[str, Any]]) -> list[str]:
    prompts: list[str] = []
    for record in records:
        update = _extract_update(record)
        if update is None or str(update.get("sessionUpdate") or "") != "user_message_chunk":
            continue
        stripped = strip_injected_context(_extract_text(update.get("content")))
        if not stripped.strip():
            continue
        if synthetic_body_reason(stripped) is not None:
            continue
        prompts.append(stripped)
    return prompts


def _load_record(line: str) -> dict[str, Any]:
    record = json.loads(line)
    assert isinstance(record, dict)
    return record


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(_load_record(line))
    return records
