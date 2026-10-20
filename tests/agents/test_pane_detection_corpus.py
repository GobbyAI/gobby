"""Pins the Python pane matcher to the corpus the Rust engine also checks (#24381)."""

from __future__ import annotations

import asyncio
import functools
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from pydantic import ValidationError

from gobby.agents.attention_metadata import AttentionMetadataStore
from gobby.agents.attention_tracker import AgentAttentionTracker
from gobby.agents.detection.matcher import CompiledManifest, compile_manifest
from gobby.agents.idle_detector import ComposerRead, IdleDetector
from gobby.agents.loop_tracker import LoopTracker
from gobby.agents.prompt_detector import DetectedPrompt, PromptDetector
from gobby.agents.stall_classifier import StallClassifier, _RunState
from gobby.agents.watchdog.quota import detect_provider_quota
from gobby.agents.watchdog.registry import WatchdogReaderRegistry
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.storage.attention import AttentionStateManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "crates/gdaemon/tests/fixtures/pane_detection/corpus.json"
BUNDLED_DIR = REPO_ROOT / "src/gobby/install/shared/detection"
RECORD_ENV = "GOBBY_RECORD_PANE_DETECTION_CORPUS"


def _content(case: dict[str, Any]) -> str:
    if "provider" in case:
        return (BUNDLED_DIR / f"{case['provider']}.toml").read_text(encoding="utf-8")
    if "manifest" in case:
        return (REPO_ROOT / str(case["manifest"])).read_text(encoding="utf-8")
    return str(case["content"])


def _refusal_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    ctx = error.get("ctx", {})
    if "error" in ctx:
        return str(ctx["error"])
    return str(error["msg"])


def _observe(case: dict[str, Any]) -> dict[str, Any]:
    try:
        compiled = compile_manifest(_content(case))
    except ValidationError as exc:
        return {"refusal": _refusal_message(exc)}
    evaluation = compiled.match(case["pane"])
    match = evaluation.match
    return {
        "state": match.state if match is not None else None,
        "rule_id": match.rule_id if match is not None else None,
        "reason": match.reason if match is not None else None,
        "issues": [
            {"rule_id": issue.rule_id, "code": issue.code, "pattern": issue.pattern}
            for issue in evaluation.issues
        ],
    }


def test_matcher_cases_match_python() -> None:
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = document["cases"]
    if os.environ.get(RECORD_ENV) == "1":
        for case in cases:
            case["expect"] = _observe(case)
        CORPUS_PATH.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    # Record mode asserts too: the recorded expectations must hold on a fresh observation.
    for case in cases:
        assert _observe(case) == case["expect"], case["id"]


def test_record_mode_still_asserts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"cases": document["cases"][:1]}), encoding="utf-8")
    observations = iter([{"state": "idle"}, {"state": "active"}])
    monkeypatch.setattr(f"{__name__}.CORPUS_PATH", corpus)
    monkeypatch.setattr(f"{__name__}._observe", lambda _case: next(observations))
    monkeypatch.setenv(RECORD_ENV, "1")
    with pytest.raises(AssertionError):
        test_matcher_cases_match_python()


RUN_ID = "run-1"


class _BundledRegistry:
    """Serves the bundled manifests the way the hub registry does, without the database."""

    def for_provider(self, provider_id: str) -> CompiledManifest | None:
        return _bundled_manifest(provider_id)


@functools.cache
def _bundled_manifest(provider_id: str) -> CompiledManifest | None:
    path = BUNDLED_DIR / f"{provider_id}.toml"
    return compile_manifest(path.read_text(encoding="utf-8")) if path.is_file() else None


class _AttentionManager:
    def __init__(self) -> None:
        self.transitions: list[dict[str, Any]] = []

    def get(self, entry_id: str) -> None:
        return None

    async def transition_async(
        self, run_db: Callable[..., Any], entry_id: str, **kwargs: Any
    ) -> None:
        self.transitions.append(kwargs)


class _MetadataStore:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def set(self, entry_id: str, text: str, ttl_ms: int) -> None:
        self.texts.append(text)

    def clear(self, entry_id: str) -> None:
        return None


async def _run_db(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


def _start_state(start: dict[str, Any]) -> _RunState:
    return _RunState(
        consecutive_provider_hits=start.get("consecutive_hits", 0),
        last_check_at=float(start.get("last_check_at", 0.0)),
    )


def _payload(prompt: DetectedPrompt | None) -> dict[str, object] | None:
    return None if prompt is None else prompt.to_payload()


def _composer(read: ComposerRead) -> dict[str, object]:
    return {"state": read.state, "line": read.line}


def _observe_stall(case: dict[str, Any]) -> list[dict[str, object]]:
    root = StallClassifier(_BundledRegistry())
    classifier = root.for_provider(case["provider"])
    root._states[RUN_ID] = _start_state(case.get("state", {}))
    verdicts: list[dict[str, object]] = []
    for step in case["steps"]:
        with mock.patch("gobby.agents.stall_classifier.time") as clock:
            clock.monotonic.return_value = float(step["at"])
            verdict = classifier.classify(
                RUN_ID, pane_output=step.get("pane"), error=step.get("error")
            )
        verdicts.append(
            {
                "status": verdict.status.value,
                "reason": verdict.reason,
                "consecutive_hits": verdict.consecutive_hits,
            }
        )
    return verdicts


def _observe_attention(case: dict[str, Any]) -> dict[str, Any]:
    provider = case["provider"]
    state = case.get("state", {})
    prompt_root = PromptDetector(_BundledRegistry())
    if state.get("trust_dismissed"):
        prompt_root.for_provider(provider).mark_dismissed(RUN_ID)
    if "approval_dismissed_pane" in state:
        prompt_root.for_provider(provider).mark_approval_prompt_dismissed(
            RUN_ID, state["approval_dismissed_pane"]
        )
    stall_root = StallClassifier(_BundledRegistry())
    stall_root._states[RUN_ID] = _start_state(state.get("stall", {}))
    manager = _AttentionManager()
    metadata = _MetadataStore()
    tmux = SimpleNamespace(auto_enter_approval_prompts=case.get("auto_enter", True))
    # The doubles stand in for hub collaborators; the attention path reads only these fields.
    tracker = AgentAttentionTracker(
        run_db=_run_db,
        prompt_detector=prompt_root,
        stall_classifier=stall_root,
        tmux_config=cast(TmuxConfig, tmux),
        attention_manager=cast(AttentionStateManager, manager),
        attention_metadata_store=cast(AttentionMetadataStore, metadata),
        get_session_manager=None,
    )
    run = cast(
        AgentRun,
        SimpleNamespace(
            id=RUN_ID, provider=provider, child_session_id="child-1", error=case.get("error")
        ),
    )
    with mock.patch("gobby.agents.stall_classifier.time") as clock:
        clock.monotonic.return_value = float(case.get("now", 100.0))
        asyncio.run(tracker.sync(run, case["pane"]))
    blocked = [call for call in manager.transitions if call.get("state") == "blocked"]
    transition = None
    if blocked:
        call = blocked[-1]
        transition = {
            "reason": call["reason"],
            "kind": call["kind"],
            "fingerprint": call["fingerprint"],
            "payload": call["payload"],
        }
    return {"transition": transition, "dismissed": "needs attention" in metadata.texts}


def _observe_loop(case: dict[str, Any]) -> list[dict[str, object]]:
    tracker = LoopTracker(threshold=case.get("threshold", 3))
    steps: list[dict[str, object]] = []
    for _ in range(case["dismissals"]):
        count = tracker.record_dismissal(RUN_ID)
        steps.append({"count": count, "escalate": tracker.should_escalate(RUN_ID)})
    return steps


def _observe_quota(case: dict[str, Any]) -> dict[str, object] | None:
    hit = detect_provider_quota(case["pane"], case["provider"])
    return None if hit is None else {"provider": hit.provider, "reset_time": hit.reset_time}


def _observe_capacity(case: dict[str, Any]) -> str | None:
    reader = WatchdogReaderRegistry().for_provider(case["provider"])
    return None if reader is None else reader.capacity_pane_message


def _bound_prompt(case: dict[str, Any]) -> PromptDetector:
    return PromptDetector(_BundledRegistry()).for_provider(case["provider"])


def _bound_idle(case: dict[str, Any]) -> IdleDetector:
    return IdleDetector(_BundledRegistry()).for_provider(case["provider"])


DETECTOR_OBSERVERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "prompt.trust": lambda case: _bound_prompt(case).detect_trust_prompt(case["pane"]),
    "prompt.trust_keys": lambda case: list(_bound_prompt(case).trust_dismiss_keys(case["pane"])),
    "prompt.loop": lambda case: _bound_prompt(case).detect_loop_prompt(case["pane"]),
    "prompt.approval": lambda case: _bound_prompt(case).detect_approval_prompt(case["pane"]),
    "prompt.queued_message": lambda case: _bound_prompt(case).detect_queued_message_prompt(
        case["pane"]
    ),
    "prompt.queued_continuation": lambda case: _bound_prompt(
        case
    ).detect_queued_continuation_prompt(case["pane"]),
    "prompt.detect": lambda case: _payload(_bound_prompt(case).detect_prompt(case["pane"])),
    "idle.detect": lambda case: _bound_idle(case).detect(case["pane"]),
    "idle.composer": lambda case: _composer(_bound_idle(case).composer_read(case["pane"])),
    "idle.unsubmitted": lambda case: _bound_idle(case).unsubmitted_input_fingerprint(case["pane"]),
    "idle.turn": lambda case: _bound_idle(case).turn_in_flight_fingerprint(case["pane"]),
    "stall.classify": _observe_stall,
    "attention.classify": _observe_attention,
    "loop.dismiss": _observe_loop,
    "quota.detect": _observe_quota,
    "quota.capacity": _observe_capacity,
}


def _observe_detector(case: dict[str, Any]) -> Any:
    return DETECTOR_OBSERVERS[case["detector"]](case)


def test_detector_cases_match_python() -> None:
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = document["detectors"]
    if os.environ.get(RECORD_ENV) == "1":
        for case in cases:
            case["expect"] = _observe_detector(case)
        CORPUS_PATH.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    # Record mode asserts too: the recorded expectations must hold on a fresh observation.
    for case in cases:
        assert _observe_detector(case) == case["expect"], case["id"]
