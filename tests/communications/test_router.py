"""Tests for MessageRouter."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from gobby.communications.models import CommsRoutingRule
from gobby.communications.router import MessageRouter


@pytest.mark.asyncio
async def test_router_matching():
    # Mock store
    store = MagicMock()
    rules = [
        CommsRoutingRule(
            id="rule-1", name="Rule 1", channel_id="chan-1", event_pattern="task.*", priority=10
        ),
        CommsRoutingRule(
            id="rule-2", name="Rule 2", channel_id="chan-2", event_pattern="*", priority=0
        ),
        CommsRoutingRule(
            id="rule-3",
            name="Rule 3",
            channel_id="chan-3",
            event_pattern="session.started",
            priority=20,
        ),
        CommsRoutingRule(
            id="rule-disabled",
            name="Disabled Rule",
            channel_id="chan-4",
            event_pattern="*",
            priority=100,
            enabled=False,
        ),
    ]

    def get_rules(enabled_only=True):
        rs = [r for r in rules if not enabled_only or r.enabled]
        return sorted(rs, key=lambda x: x.priority, reverse=True)

    store.list_routing_rules.side_effect = get_rules

    router = MessageRouter(store)

    # Matches task.* and *
    matched = await router.match_channels("task.created")
    # Rule 1 (10) and Rule 2 (0) match. Rule 3 doesn't. Disabled doesn't.
    assert matched == ["chan-1", "chan-2"]

    # Matches session.started and *
    matched = await router.match_channels("session:started")  # Test normalization
    assert matched == ["chan-3", "chan-2"]

    # Matches only *
    matched = await router.match_channels("unknown.event")
    assert matched == ["chan-2"]


@pytest.mark.asyncio
async def test_router_normalizes_rule_patterns():
    store = MagicMock()
    store.list_routing_rules.return_value = [
        CommsRoutingRule(
            id="rule-1",
            name="Rule 1",
            channel_id="chan-1",
            event_pattern="SESSION:STARTED",
        )
    ]
    router = MessageRouter(store)

    assert await router.match_channels("session started") == ["chan-1"]


async def test_router_loads_rules_off_event_loop():
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    store = MagicMock()
    rules = [
        CommsRoutingRule(id="rule-1", name="Rule 1", channel_id="chan-1", event_pattern="*"),
    ]

    def list_routing_rules(enabled_only: bool = True) -> list[CommsRoutingRule]:
        assert enabled_only is True
        worker_threads.append(threading.get_ident())
        return rules

    store.list_routing_rules.side_effect = list_routing_rules
    router = MessageRouter(store)

    assert await router.match_channels("task.created") == ["chan-1"]
    assert len(worker_threads) == 1
    assert worker_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_router_filtering():
    store = MagicMock()
    rules = [
        CommsRoutingRule(
            id="rule-proj",
            name="Project Rule",
            channel_id="chan-proj",
            event_pattern="*",
            project_id="proj-1",
            priority=10,
        ),
        CommsRoutingRule(
            id="rule-sess",
            name="Session Rule",
            channel_id="chan-sess",
            event_pattern="*",
            session_id="sess-1",
            priority=5,
        ),
    ]
    store.list_routing_rules.return_value = rules

    router = MessageRouter(store)

    # Match project
    assert await router.match_channels("any", project_id="proj-1") == ["chan-proj"]
    # No project match
    assert await router.match_channels("any", project_id="proj-2") == []

    # Match session
    assert await router.match_channels("any", session_id="sess-1") == ["chan-sess"]
    # No session match
    assert await router.match_channels("any", session_id="sess-2") == []


@pytest.mark.asyncio
async def test_router_caches_rules_with_ttl():
    """Router should cache rules and not re-query store within TTL."""
    store = MagicMock()
    rules = [
        CommsRoutingRule(
            id="rule-1", name="Rule 1", channel_id="chan-1", event_pattern="*", priority=10
        ),
    ]
    store.list_routing_rules.return_value = rules

    router = MessageRouter(store)

    # First call loads from store
    await router.match_channels("task.created")
    assert store.list_routing_rules.call_count == 1

    # Second call within TTL uses cache
    await router.match_channels("task.updated")
    assert store.list_routing_rules.call_count == 1


@pytest.mark.asyncio
async def test_router_cache_expires_after_ttl():
    """Router should re-query store after TTL expires."""
    store = MagicMock()
    rules = [
        CommsRoutingRule(
            id="rule-1", name="Rule 1", channel_id="chan-1", event_pattern="*", priority=10
        ),
    ]
    store.list_routing_rules.return_value = rules

    router = MessageRouter(store)
    router._cache_ttl = 30.0

    with patch("gobby.communications.router.time") as mock_time:
        mock_time.monotonic.return_value = 100.0
        await router.match_channels("task.created")
        assert store.list_routing_rules.call_count == 1

        # Advance past TTL
        mock_time.monotonic.return_value = 131.0
        await router.match_channels("task.updated")
        assert store.list_routing_rules.call_count == 2


@pytest.mark.asyncio
async def test_router_invalidate_cache():
    """invalidate_cache() should force next call to re-query store."""
    store = MagicMock()
    rules_v1 = [
        CommsRoutingRule(
            id="rule-1", name="Rule 1", channel_id="chan-1", event_pattern="task.*", priority=10
        ),
    ]
    rules_v2 = [
        CommsRoutingRule(
            id="rule-1", name="Rule 1", channel_id="chan-1", event_pattern="task.*", priority=10
        ),
        CommsRoutingRule(
            id="rule-2", name="Rule 2", channel_id="chan-2", event_pattern="task.*", priority=5
        ),
    ]
    store.list_routing_rules.side_effect = [rules_v1, rules_v2]

    router = MessageRouter(store)

    # First call — gets v1 rules
    matched = await router.match_channels("task.created")
    assert matched == ["chan-1"]

    # Invalidate cache
    router.invalidate_cache()

    # Next call re-queries store and gets v2 rules
    matched = await router.match_channels("task.created")
    assert matched == ["chan-1", "chan-2"]
    assert store.list_routing_rules.call_count == 2
