"""Tests for shared dispatch constants."""

from gobby.build import observability
from gobby.config.build import BuildConfig
from gobby.dispatch.constants import MAX_ACTIVE_AGENTS


def test_max_active_agents_is_single_source() -> None:
    assert MAX_ACTIVE_AGENTS == 20
    assert BuildConfig().max_active_agents == MAX_ACTIVE_AGENTS
    assert observability.MAX_ACTIVE_AGENTS == MAX_ACTIVE_AGENTS
