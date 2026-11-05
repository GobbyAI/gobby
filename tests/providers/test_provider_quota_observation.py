"""Provider quota observations: typed signals, pane hints, freshness, and persisted reads.

Fixtures are inline. Each is labeled ``recorded`` (copied from a checked provider
contract or research sample, with provider version) or ``synthetic`` (rendered by
hand from a documented literal form; never claimed as emitted provider output).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gobby.providers.capacity_service import ProviderCapacityService
from gobby.providers.codex_rate_limits import observation_from_rate_limits
from gobby.providers.quota_observations import PANE_HINT_LIMIT, decode_quota_details
from gobby.providers.quota_signals import (
    PaneOccurrenceTracker,
    PaneQuotaHint,
    classify_pane_hint,
    normalize_reporter_snapshot,
    normalize_typed_signal,
    pane_generation,
)
from gobby.providers.usage import ProviderUsageSnapshot, TransientUsageRefreshError, UsageWindow
from gobby.storage.provider_capacity import PersistedCapacityState, ProviderCapacityRecord
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import SnapshotResult

_T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
_MACHINE_ID = "dddddddd-dddd-4ddd-8ddd-000000000002"

# recorded: Codex token_count rate_limits, shape from the 2026-09 live sample
# (research/usage-tracking-context-23099.md); values are synthetic reductions of it.
_CODEX_WEEKLY: Mapping[str, object] = {
    "limit_id": "codex",
    "primary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 1790000000},
    "secondary": None,
    "credits": {"has_credits": True, "unlimited": False, "balance": "62111.98"},
    "plan_type": "pro",
    "rate_limit_reached_type": None,
}
# synthetic: two independent windows on one limit family.
_CODEX_TWO_WINDOWS: Mapping[str, object] = {
    "limit_id": "codex",
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1789990000},
    "secondary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 1790000000},
    "plan_type": "pro",
}
# synthetic: windowless reached flag.
_CODEX_REACHED: Mapping[str, object] = {
    "limit_id": "codex",
    "primary": None,
    "secondary": None,
    "rate_limit_reached_type": "rate_limit_reached",
}
# recorded: RateLimitReachedType variants from the Codex core protocol enum
# (codex-rs/protocol/src/protocol.rs, snake_case); the same names appear in codex-cli 0.166.0.
_RECORDED_REACHED_TYPES: tuple[str, ...] = (
    "rate_limit_reached",
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
)
# synthetic: no window and no reached flag, so nothing is verified.
_CODEX_NO_SIGNAL: Mapping[str, object] = {
    "limit_id": "codex",
    "primary": None,
    "secondary": None,
    "rate_limit_reached_type": None,
}
# recorded: AGY error envelope text from tests/fixtures/provider_contracts/agy/command-captures.json.
_AGY_QUOTA_TEXT = (
    "Individual quota reached. Please upgrade your subscription to increase your limits. "
    "Resets in 120h9m43s."
)
_AGY_ERROR_ENVELOPE: Mapping[str, object] = {"status": "ERROR", "error": _AGY_QUOTA_TEXT}


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _MemoryStorage:
    """In-memory stand-in for ProviderCapacityStorage with the same CAS contract."""

    machine_id = _MACHINE_ID

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._records: dict[str, ProviderCapacityRecord] = {}

    def get(self, provider: str) -> ProviderCapacityRecord | None:
        record = self._records.get(provider)
        if record is None:
            return None
        age = max(0.0, (self._clock() - record.observed_at).total_seconds())
        return replace(record, age_seconds=age)

    def upsert(
        self,
        *,
        provider: str,
        state: PersistedCapacityState,
        observed_at: datetime,
        windows: Sequence[Mapping[str, object]],
        reason: str | None,
        source_version: str,
        details: Mapping[str, object],
        expected_observed_at: datetime | None,
    ) -> bool:
        current = self._records.get(provider)
        current_revision = None if current is None else current.observed_at
        if current_revision != expected_observed_at:
            return False
        self._records[provider] = ProviderCapacityRecord(
            machine_id=self.machine_id,
            provider=provider,
            state=state,
            observed_at=observed_at,
            windows=tuple(dict(window) for window in windows),
            reason=reason,
            source_version=source_version,
            age_seconds=0.0,
            details=dict(details),
        )
        return True


class _TimeoutReporter:
    @property
    def provider(self) -> str:
        return "agy"

    async def report(self) -> ProviderUsageSnapshot:
        raise TransientUsageRefreshError("agy", "timeout", "timed out after 15 seconds")


def _service(clock: _Clock) -> ProviderCapacityService:
    return ProviderCapacityService(
        _MemoryStorage(clock),
        reporters={},
        support_resolvers={},
        clock=clock,
    )


def _terminal(*, session_id: str) -> Terminal:
    # synthetic: a tmux terminal row with the ownership fields the generation hashes.
    return Terminal(
        id="term-1",
        backend="tmux",
        ownership="gobby",
        state="running",
        machine_id="machine-1",
        project_id="project-1",
        created_at=_T0,
        updated_at=_T0,
        attempt_generation=1,
        attempt_started_at=_T0,
        unresolved_writes={},
        host_epoch="epoch-1",
        session_id=session_id,
    )


def _capture(
    lines: Sequence[str],
    *,
    truncated: bool = False,
    trailing_newline: bool = True,
) -> SnapshotResult:
    text = "\n".join(lines) + ("\n" if trailing_newline else "")
    dropped = 4096 if truncated else 0
    return SnapshotResult(
        text=text,
        truncated=truncated,
        dropped_bytes=dropped,
        total_bytes=len(text.encode("utf-8")) + dropped,
    )


async def test_claude_quota_signals() -> None:
    # Pane forms are possible-limit hints only.
    assert classify_pane_hint("claude", ["> hi", "You've hit your limit · resets 5pm"]) == (
        "usage_limit"
    )
    assert classify_pane_hint("claude", ["Usage limit reached"]) == "usage_limit"
    # Near-miss wording and bare HTTP 429 text are not hints.
    assert classify_pane_hint("claude", ["You've hit your limits for today"]) is None
    assert classify_pane_hint("claude", ["API Error: 429 rate limited"]) is None

    # An SDK-shaped typed event without emitted-envelope provenance yields nothing.
    sdk_rejected = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected", "utilization": 1.0, "resetsAt": 1790000000},
    }
    sdk_warning = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed_warning", "utilization": 0.9},
    }
    assert normalize_typed_signal("claude", sdk_rejected, observed_at=_T0, source_key="s1") is None
    assert normalize_typed_signal("claude", sdk_warning, observed_at=_T0, source_key="s1") is None

    # Statusline-style rate_limits never become quota observations.
    statusline = {"rate_limits": {"five_hour": {"used_percentage": 100}}}
    assert normalize_typed_signal("claude", statusline, observed_at=_T0, source_key="s1") is None


async def test_codex_quota_signals() -> None:
    weekly_only = observation_from_rate_limits(
        _CODEX_WEEKLY,
        source_observed_at=_T0,
        occurrence="codex-occ-1",
        source_key="codex-rollout-1",
    )
    assert weekly_only is not None
    (weekly,) = weekly_only.components
    assert weekly.state == "available"
    assert weekly.scope_known is False
    assert weekly.resets_at == datetime.fromtimestamp(1790000000, tz=UTC)
    assert weekly.window is not None
    assert (weekly.window.label, weekly.window.used, weekly.window.limit) == (
        "weekly",
        80.0,
        100.0,
    )
    assert weekly.window.unit == "percent"
    assert weekly.metadata["plan_type"] == "pro"

    # Independent windows keep separate component keys and labels.
    both = observation_from_rate_limits(
        _CODEX_TWO_WINDOWS,
        source_observed_at=_T0,
        occurrence="codex-occ-2",
        source_key="codex-rollout-1",
    )
    assert both is not None
    labels = {component.window.label for component in both.components if component.window}
    assert labels == {"five_hour", "weekly"}
    assert len({component.key for component in both.components}) == 2

    # A windowless reached flag survives normalization as an exhausted hard signal.
    reached = observation_from_rate_limits(
        _CODEX_REACHED,
        source_observed_at=_T0,
        occurrence="codex-occ-3",
        source_key="codex-rollout-1",
    )
    assert reached is not None
    (hard,) = reached.components
    assert hard.state == "exhausted"
    assert hard.window is None
    assert hard.resets_at is None
    assert (
        observation_from_rate_limits(
            _CODEX_NO_SIGNAL,
            source_observed_at=_T0,
            occurrence="codex-occ-4",
            source_key="codex-rollout-1",
        )
        is None
    )

    # The same payload inside a token_count envelope normalizes identically.
    token_count = {
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": dict(_CODEX_WEEKLY)},
    }
    via_envelope = normalize_typed_signal(
        "codex", token_count, observed_at=_T0, source_key="codex-rollout-1"
    )
    assert via_envelope is not None
    assert [c.key for c in via_envelope.components] == [c.key for c in weekly_only.components]

    # Typed usage_limit_exceeded is verified; other task errors are not quota.
    usage_limit = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "error": {"codex_error_info": "usage_limit_exceeded"},
        },
    }
    typed_hard = normalize_typed_signal("codex", usage_limit, observed_at=_T0, source_key="s")
    assert typed_hard is not None
    (typed_component,) = typed_hard.components
    assert typed_component.state == "exhausted"
    assert typed_component.window is None
    context_full = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "error": {"codex_error_info": "context_window_exceeded"},
        },
    }
    assert normalize_typed_signal("codex", context_full, observed_at=_T0, source_key="s") is None

    # Pane banners are hints only; context-full text is not a quota hint.
    assert classify_pane_hint("codex", ["You've hit your usage limit"]) == "usage_limit"
    assert classify_pane_hint("codex", ["Context window full. Compacting conversation."]) is None


async def test_grok_quota_signals() -> None:
    for line in (
        "Usage balance exhausted.",
        "usage limit reached",
        "Usage Balance Exhausted",
        "You hit your free usage limit.",
    ):
        assert classify_pane_hint("grok", [line]) == "usage_limit", line
    assert classify_pane_hint("grok", ["Out of credits"]) == "credits_exhausted"
    assert classify_pane_hint("grok", ["Run out of credits"]) == "credits_exhausted"
    # Transient rate, global, and concurrency limits are excluded.
    assert classify_pane_hint("grok", ["Rate limit exceeded, retrying in 3s"]) is None
    assert classify_pane_hint("grok", ["Global concurrency limit reached"]) is None

    # Unverified typed codes never become observations.
    for record in (
        {"error": {"code": "rate_limit_exceeded"}},
        {"error": {"code": "concurrency_limit"}},
        {"status": "usage_exhausted"},
    ):
        assert normalize_typed_signal("grok", record, observed_at=_T0, source_key="s") is None


async def test_droid_quota_signals() -> None:
    assert (
        classify_pane_hint("droid", ["Standard Usage limit reached. Try again later."])
        == "usage_limit"
    )
    assert classify_pane_hint("droid", ["Error 402: payment required"]) is None
    assert classify_pane_hint("droid", ["HTTP 429 Too Many Requests"]) is None

    # Bare 402/429, provider rate limits, and spent credits never become observations.
    for record in (
        {"status": 402},
        {"status": 429},
        {"error": {"code": "provider_rate_limited"}},
        {"factoryCredits": {"spent": 12.5}},
    ):
        assert normalize_typed_signal("droid", record, observed_at=_T0, source_key="s") is None


async def test_qwen_quota_signals() -> None:
    # Quota exhausted plus reset guidance in the same block is a hint, with no reset time.
    block = ["Quota exhausted: daily limit reached.", "Your quota will reset at 00:00 UTC."]
    assert classify_pane_hint("qwen", block) == "quota_exhausted"
    # Exhaustion without reset guidance, a prefixed wrapper, and retryable 429 text do not hint.
    assert classify_pane_hint("qwen", ["Quota exhausted: daily limit reached."]) is None
    assert classify_pane_hint("qwen", ["Error: Quota exhausted. Resets at 00:00 UTC"]) is None
    assert classify_pane_hint("qwen", ["429 Too Many Requests. Retry in 5 seconds."]) is None
    assert classify_pane_hint("qwen", ["Qwen OAuth free tier has been discontinued."]) is None

    for record in ({"status": 429}, {"error": {"code": "overloaded"}}):
        assert normalize_typed_signal("qwen", record, observed_at=_T0, source_key="s") is None


async def test_agy_quota_signals() -> None:
    # recorded ERROR envelope: a verified native format. Reset is relative to the first
    # verified occurrence time and is computed once.
    first_seen = _T0
    observation = normalize_typed_signal(
        "agy", _AGY_ERROR_ENVELOPE, observed_at=first_seen, source_key="agy-run-7"
    )
    assert observation is not None
    (component,) = observation.components
    assert component.state == "exhausted"
    assert component.window is None
    assert component.resets_at == first_seen + timedelta(hours=120, minutes=9, seconds=43)

    # The same text rendered in a pane is a possible-limit hint only.
    assert classify_pane_hint("agy", [_AGY_QUOTA_TEXT]) == "individual_quota"

    # Existing /usage reporter windows normalize as verified provider-wide numeric components.
    snapshot = ProviderUsageSnapshot(
        provider="agy",
        observed_at=_T0,
        supported=True,
        windows=(
            UsageWindow(
                label="Gemini Models — Weekly Limit Remaining",
                used=1.0,
                limit=1.0,
                unit="fraction",
                resets_at="2026-11-06T10:00:00Z",
            ),
        ),
        raw={},
    )
    reporter = normalize_reporter_snapshot(snapshot)
    (weekly,) = reporter.components
    assert weekly.state == "exhausted"
    assert weekly.window is not None
    assert weekly.window.unit == "fraction"

    # A reporter timeout is not exhaustion: with no accepted row the read stays unknown.
    clock = _Clock(_T0)
    service = ProviderCapacityService(
        _MemoryStorage(clock),
        reporters={"agy": _TimeoutReporter()},
        support_resolvers={},
        clock=clock,
    )
    timed_out = await service.refresh("agy")
    assert timed_out.state == "unknown"
    assert timed_out.windows == ()


async def test_output_only_freshness() -> None:
    clock = _Clock(_T0)
    service = _service(clock)
    hard = observation_from_rate_limits(
        _CODEX_REACHED,
        source_observed_at=_T0,
        occurrence="codex-reached-1",
        source_key="codex-rollout-1",
    )
    assert hard is not None
    accepted = await service.observe(hard)
    assert accepted.state == "exhausted"
    assert accepted.windows == ()

    # The verified windowless exhausted signal persists until 900 seconds pass, then is stale.
    clock.now = _T0 + timedelta(seconds=899)
    assert (await service.get("codex")).state == "exhausted"
    clock.now = _T0 + timedelta(seconds=901)
    stale = await service.get("codex")
    assert stale.state == "stale"
    assert stale.windows == ()

    # A hint-only row reads unknown on its own and never refreshes the quota clock.
    hint_clock = _Clock(_T0)
    hint_only = _service(hint_clock)
    hint = PaneQuotaHint(
        provider="codex",
        occurrence="pane-occ-1",
        kind="usage_limit",
        detected_at=_T0,
    )
    assert await hint_only.observe_pane_hint(hint, generation="seat-a") is True
    assert (await hint_only.get("codex")).state == "unknown"

    # A later hint does not move the exhausted component's clock back into freshness.
    clock.now = _T0 + timedelta(seconds=901)
    later_hint = PaneQuotaHint(
        provider="codex",
        occurrence="pane-occ-2",
        kind="usage_limit",
        detected_at=clock.now,
    )
    assert await service.observe_pane_hint(later_hint, generation="seat-a") is True
    assert (await service.get("codex")).state == "stale"


async def test_untrusted_or_ambiguous_signals_are_ignored() -> None:
    # Quoted JSON inside message content is not a top-level provider envelope.
    quoted = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": (
                        '{"type":"event_msg","payload":{"type":"task_complete","error":'
                        '{"codex_error_info":"usage_limit_exceeded"}}}'
                    ),
                }
            ],
        },
    }
    assert normalize_typed_signal("codex", quoted, observed_at=_T0, source_key="s") is None

    # Malformed numeric fields and zero-length windows are rejected, not defaulted.
    malformed = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": "lots", "window_minutes": 10080},
                "secondary": None,
            },
        },
    }
    assert normalize_typed_signal("codex", malformed, observed_at=_T0, source_key="s") is None
    zero_window = {
        "limit_id": "codex",
        "primary": {"used_percent": 80.0, "window_minutes": 0},
        "secondary": None,
    }
    assert (
        observation_from_rate_limits(
            zero_window,
            source_observed_at=_T0,
            occurrence="zero",
            source_key="s",
        )
        is None
    )

    # Unknown typed codes are not quota.
    unknown_code = {
        "type": "event_msg",
        "payload": {"type": "task_complete", "error": {"codex_error_info": "new_unverified"}},
    }
    assert normalize_typed_signal("codex", unknown_code, observed_at=_T0, source_key="s") is None

    # An unparseable relative reset is an ambiguous time, so nothing is admitted.
    ambiguous_reset = {"status": "ERROR", "error": "Individual quota reached. Resets in soon."}
    assert normalize_typed_signal("agy", ambiguous_reset, observed_at=_T0, source_key="s") is None

    # Credentials riding along in a payload never reach the observation.
    with_secret = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": 1790000000},
                "secondary": None,
                "access_token": "sk-test-secret",
            },
        },
    }
    observed = normalize_typed_signal("codex", with_secret, observed_at=_T0, source_key="s")
    assert observed is not None
    assert "sk-test-secret" not in repr(observed)

    # A genuine banner and a quoted copy of it produce the same uncertain hint, nothing more.
    genuine = "You've hit your usage limit"
    quoted_line = '> "You\'ve hit your usage limit"'
    assert classify_pane_hint("codex", [genuine]) == "usage_limit"
    assert classify_pane_hint("codex", [quoted_line]) == "usage_limit"


async def test_pane_occurrence_contract() -> None:
    terminal = _terminal(session_id="sess-1")
    generation = pane_generation(terminal)
    assert pane_generation(_terminal(session_id="sess-2")) != generation

    base = ["alpha", "beta", "gamma", "delta", "$ ready"]
    banner = "You've hit your usage limit"
    tracker = PaneOccurrenceTracker()

    # The first capture after attachment is a baseline: retained banners never emit.
    assert (
        tracker.observe(
            provider="codex", generation=generation, snapshot=_capture(base), detected_at=_T0
        )
        is None
    )
    # An identical redraw adds no complete lines.
    assert (
        tracker.observe(
            provider="codex", generation=generation, snapshot=_capture(base), detected_at=_T0
        )
        is None
    )
    # A new complete banner after a verified overlap yields one hint.
    scrolled = [*base[1:], banner, "$ "]
    hint = tracker.observe(
        provider="codex",
        generation=generation,
        snapshot=_capture(scrolled),
        detected_at=_T0 + timedelta(seconds=5),
    )
    assert hint is not None
    assert (hint.provider, hint.kind) == ("codex", "usage_limit")
    # Replaying the same capture adds no new lines.
    assert (
        tracker.observe(
            provider="codex",
            generation=generation,
            snapshot=_capture(scrolled),
            detected_at=_T0 + timedelta(seconds=6),
        )
        is None
    )
    # A restarted tracker rebaselines on its first capture.
    restarted = PaneOccurrenceTracker()
    assert (
        restarted.observe(
            provider="codex", generation=generation, snapshot=_capture(scrolled), detected_at=_T0
        )
        is None
    )

    # An incomplete final line is not matched; its completion later emits exactly one hint.
    incomplete_tracker = PaneOccurrenceTracker()
    incomplete_tracker.observe(
        provider="codex", generation=generation, snapshot=_capture(base), detected_at=_T0
    )
    partial = [*base[1:], banner]
    assert (
        incomplete_tracker.observe(
            provider="codex",
            generation=generation,
            snapshot=_capture(partial, trailing_newline=False),
            detected_at=_T0,
        )
        is None
    )
    completed = incomplete_tracker.observe(
        provider="codex",
        generation=generation,
        snapshot=_capture([*partial, "$ "]),
        detected_at=_T0 + timedelta(seconds=1),
    )
    assert completed is not None

    # Truncated history with a verified overlap still admits a new complete banner.
    truncated_tracker = PaneOccurrenceTracker()
    truncated_tracker.observe(
        provider="codex", generation=generation, snapshot=_capture(base), detected_at=_T0
    )
    truncated_hint = truncated_tracker.observe(
        provider="codex",
        generation=generation,
        snapshot=_capture([*base[2:], banner, "$ "], truncated=True),
        detected_at=_T0 + timedelta(seconds=2),
    )
    assert truncated_hint is not None

    # Truncated output with no overlap rebaselines and emits nothing.
    no_overlap = tracker.observe(
        provider="codex",
        generation=generation,
        snapshot=_capture(["unrelated one", "unrelated two", banner], truncated=True),
        detected_at=_T0 + timedelta(seconds=3),
    )
    assert no_overlap is None

    # A banner that only scrolled out of the overlap region is not new.
    overlap_tracker = PaneOccurrenceTracker()
    overlap_tracker.observe(
        provider="codex",
        generation=generation,
        snapshot=_capture([banner, "a", "b", "c", "$ "]),
        detected_at=_T0,
    )
    assert (
        overlap_tracker.observe(
            provider="codex",
            generation=generation,
            snapshot=_capture(["a", "b", "c", "$ ", "new output"]),
            detected_at=_T0,
        )
        is None
    )

    # A replaced ownership generation and a host-failure rebaseline both start over.
    other_generation = pane_generation(_terminal(session_id="sess-2"))
    assert (
        tracker.observe(
            provider="codex",
            generation=other_generation,
            snapshot=_capture([*base[1:], banner, "$ "]),
            detected_at=_T0,
        )
        is None
    )
    tracker.rebaseline(generation)
    assert (
        tracker.observe(
            provider="codex",
            generation=generation,
            snapshot=_capture([*base[1:], banner, "$ "]),
            detected_at=_T0,
        )
        is None
    )


async def test_pane_hint_persistence_dedupes_occurrences() -> None:
    clock = _Clock(_T0)
    service = _service(clock)
    hint = PaneQuotaHint(
        provider="codex",
        occurrence="pane-occ-dedupe",
        kind="usage_limit",
        detected_at=_T0,
    )
    assert await service.observe_pane_hint(hint, generation="seat-a") is True
    # A duplicate is recognized before any write, so it neither refreshes nor duplicates.
    assert await service.observe_pane_hint(hint, generation="seat-a") is False


async def test_pane_hints_persist_for_every_provider() -> None:
    for provider in ("claude", "codex", "grok", "droid", "qwen", "agy"):
        service = _service(_Clock(_T0))
        hint = PaneQuotaHint(
            provider=provider,
            occurrence=f"{provider}-pane-occ",
            kind="usage_limit",
            detected_at=_T0,
        )
        assert await service.observe_pane_hint(hint, generation=f"{provider}-seat") is True
        # The persisted dedupe record recognizes a repeat before any write.
        assert await service.observe_pane_hint(hint, generation=f"{provider}-seat") is False
        # A hint alone is never a quota reading.
        assert (await service.get(provider)).state == "unknown"


async def test_pane_hints_keep_one_occurrence_per_generation() -> None:
    clock = _Clock(_T0)
    storage = _MemoryStorage(clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    total = PANE_HINT_LIMIT + 3
    for index in range(total):
        hint = PaneQuotaHint(
            provider="codex",
            occurrence=f"pane-occ-{index}",
            kind="usage_limit",
            detected_at=_T0 + timedelta(seconds=index),
        )
        assert await service.observe_pane_hint(hint, generation="seat-a") is True
    record = storage.get("codex")
    assert record is not None
    _, pane_hints = decode_quota_details(record.details)
    # No occurrence history is kept; the live generation keeps only its last occurrence.
    assert pane_hints == {}
    section = record.details.get("pane_hint_alerts")
    assert isinstance(section, dict)
    generations = section.get("generations")
    assert isinstance(generations, dict)
    assert list(generations) == ["seat-a"]
    entry = generations["seat-a"]
    assert isinstance(entry, dict)
    assert entry.get("occurrence") == f"pane-occ-{total - 1}"


def test_codex_malformed_reached_and_reset_handling() -> None:
    # A reached flag that is not a non-empty string is unverified, so the whole payload is ignored.
    for reached in ("", 1, True):
        payload: Mapping[str, object] = {
            "limit_id": "codex",
            "primary": None,
            "secondary": None,
            "rate_limit_reached_type": reached,
        }
        assert (
            observation_from_rate_limits(
                payload, source_observed_at=_T0, occurrence="bad-reached", source_key="s"
            )
            is None
        )
    # A reset that is not a finite number is unknown, never a fabricated reset time.
    payload = {
        "limit_id": "codex",
        "primary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": "soon"},
        "secondary": None,
    }
    observation = observation_from_rate_limits(
        payload, source_observed_at=_T0, occurrence="bad-reset", source_key="s"
    )
    assert observation is not None
    (component,) = observation.components
    assert component.resets_at is None
    assert component.state == "available"


def test_unverified_reached_code_and_overflowing_reset_are_ignored() -> None:
    # An unknown reached code is not a verified reached type, so the payload is unverified.
    unknown_code: Mapping[str, object] = {
        "limit_id": "codex",
        "primary": None,
        "secondary": None,
        "rate_limit_reached_type": "unverified_future_code",
    }
    assert (
        observation_from_rate_limits(
            unknown_code, source_observed_at=_T0, occurrence="unknown-code", source_key="s"
        )
        is None
    )
    # Durations that overflow timedelta or move past datetime.max are malformed, not clamped.
    for text in (
        "Individual quota reached. Resets in 999999999999999999999h.",
        "Individual quota reached. Resets in 99999999h.",
    ):
        record: Mapping[str, object] = {"status": "ERROR", "error": text}
        assert normalize_typed_signal("agy", record, observed_at=_T0, source_key="s") is None
    # A reporter reset that parses but converts out of datetime range is unknown, not an error.
    edge = ProviderUsageSnapshot(
        provider="agy",
        observed_at=_T0,
        supported=True,
        windows=(
            UsageWindow(
                label="Edge",
                used=1.0,
                limit=1.0,
                unit="fraction",
                resets_at="9999-12-31T23:59:59-05:00",
            ),
        ),
        raw={},
    )
    (edge_component,) = normalize_reporter_snapshot(edge).components
    assert edge_component.resets_at is None


@pytest.mark.parametrize("reached", _RECORDED_REACHED_TYPES)
def test_recorded_reached_type_is_exhausted_without_windows(reached: str) -> None:
    # Every recorded RateLimitReachedType value is a verified hard limit with no window.
    payload: Mapping[str, object] = {
        "limit_id": "codex",
        "primary": None,
        "secondary": None,
        "rate_limit_reached_type": reached,
    }
    observation = observation_from_rate_limits(
        payload, source_observed_at=_T0, occurrence=f"reached-{reached}", source_key="s"
    )
    assert observation is not None
    (component,) = observation.components
    assert component.state == "exhausted"
    assert component.window is None


@pytest.mark.parametrize(
    "window",
    [
        {"used_percent": 10**400, "window_minutes": 300},
        {"used_percent": 10**5000, "window_minutes": 300},
        {"used_percent": float("nan"), "window_minutes": 300},
        {"used_percent": float("inf"), "window_minutes": 300},
        {"used_percent": 10.0, "window_minutes": 10**400},
        {"used_percent": 10.0, "window_minutes": 10**5000},
        {"used_percent": 10.0, "window_minutes": 300, "resets_at": 10**5000},
    ],
    ids=[
        "used-400-digits",
        "used-5000-digits",
        "used-nan",
        "used-inf",
        "minutes-400-digits",
        "minutes-5000-digits",
        "reset-5000-digits",
    ],
)
def test_codex_hostile_window_numbers_are_ignored(window: Mapping[str, object]) -> None:
    # Numbers past float or integer-digit range, or non-finite, make the payload unverifiable.
    record: Mapping[str, object] = {
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {"primary": window, "secondary": None}},
    }
    assert normalize_typed_signal("codex", record, observed_at=_T0, source_key="s") is None


@pytest.mark.parametrize(
    "resets_at",
    [10**400, float("nan"), float("inf")],
    ids=["reset-400-digits", "reset-nan", "reset-inf"],
)
def test_codex_hostile_reset_is_unknown_not_raised(resets_at: object) -> None:
    # A reset that is not a representable finite time is unknown; the window itself stays.
    record: Mapping[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": 80.0, "window_minutes": 300, "resets_at": resets_at},
                "secondary": None,
            },
        },
    }
    observation = normalize_typed_signal("codex", record, observed_at=_T0, source_key="s")
    assert observation is not None
    (component,) = observation.components
    assert component.resets_at is None
    assert component.state == "available"


@pytest.mark.parametrize(
    "used",
    [100.5, 10**100, 10**300, 1.7976931348623157e308],
    ids=["just-over-100", "1e100", "1e300", "float-max"],
)
def test_codex_out_of_range_percent_is_ignored(used: float) -> None:
    # A used percent past the 0-100 scale is not a verified reading, however finite.
    record: Mapping[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 300},
                "secondary": None,
            },
        },
    }
    assert normalize_typed_signal("codex", record, observed_at=_T0, source_key="s") is None


@pytest.mark.parametrize(
    ("used", "state"),
    [(0.0, "available"), (100.0, "exhausted")],
    ids=["zero", "exactly-100"],
)
def test_codex_percent_boundaries_are_inclusive(used: float, state: str) -> None:
    record: Mapping[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 300},
                "secondary": None,
            },
        },
    }
    observation = normalize_typed_signal("codex", record, observed_at=_T0, source_key="s")
    assert observation is not None
    (component,) = observation.components
    assert component.state == state


@pytest.mark.parametrize("digits", [400, 5000], ids=["400-digits", "5000-digits"])
def test_agy_oversized_duration_is_ignored(digits: int) -> None:
    # A duration past timedelta range or the integer digit limit is unverifiable, never raised.
    record: Mapping[str, object] = {
        "status": "ERROR",
        "error": "Individual quota reached. Resets in " + "9" * digits + "h.",
    }
    assert normalize_typed_signal("agy", record, observed_at=_T0, source_key="s") is None
