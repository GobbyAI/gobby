"""Tests for memory-related feature configurations."""

import pytest
from pydantic import ValidationError

from gobby.config.features import (
    KnowledgeGraphQueueConfig,
)

pytestmark = pytest.mark.unit


class TestKnowledgeGraphQueueConfig:
    """Tests for KnowledgeGraphQueueConfig."""

    def test_exists(self) -> None:
        config = KnowledgeGraphQueueConfig()
        assert config is not None

    def test_defaults(self) -> None:
        config = KnowledgeGraphQueueConfig()
        assert config.interval_minutes == 30
        assert config.batch_size == 20
        assert config.max_deterministic_attempts == 3

    def test_overridable(self) -> None:
        config = KnowledgeGraphQueueConfig(
            interval_minutes=15,
            batch_size=50,
            max_deterministic_attempts=5,
        )
        assert config.interval_minutes == 15
        assert config.batch_size == 50
        assert config.max_deterministic_attempts == 5

    def test_max_deterministic_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeGraphQueueConfig(max_deterministic_attempts=0)


class TestDaemonConfigIntegration:
    """Tests for memory feature configs in DaemonConfig."""

    def test_knowledge_graph_queue_on_daemon_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert hasattr(config, "knowledge_graph_queue")
        assert isinstance(config.knowledge_graph_queue, KnowledgeGraphQueueConfig)

    def test_no_memory_entity_extraction_on_daemon_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert not hasattr(config, "memory_entity_extraction")
