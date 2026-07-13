"""Tests for the embedding switch state machine and collection name resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.ai.embedding_switch import (
    PHASE_ABORTED,
    PHASE_BUILDING,
    PHASE_GC,
    PHASE_STAGING,
    SwitchAlreadyActiveError,
    SwitchJournal,
    SwitchJournalStateError,
    abort_switch,
    active_alias_names,
    advance_phase,
    build_physical_names,
    complete_switch,
    get_switch_status,
    record_switch_error,
    start_switch,
)
from gobby.memory.collection_names import EMBEDDING_COLLECTION_KINDS, CollectionNameResolver

pytestmark = pytest.mark.unit


class TestCollectionNameResolver:
    """Test the collection name resolver for staged switches."""

    def test_active_alias_returns_kind(self) -> None:
        resolver = CollectionNameResolver()
        assert resolver.active_alias("memories") == "memories"
        assert resolver.active_alias("tool_embeddings") == "tool_embeddings"
        assert resolver.active_alias("gobby_github_issues") == "gobby_github_issues"

    def test_physical_name_includes_run_id(self) -> None:
        resolver = CollectionNameResolver()
        name = resolver.physical_name("memories", "4096-abc123")
        assert name == "memories@4096-abc123"

    def test_parse_physical_name(self) -> None:
        resolver = CollectionNameResolver()
        result = resolver.parse_physical_name("memories@4096-abc123")
        assert result == ("memories", "4096-abc123")

    def test_parse_physical_name_returns_none_for_alias(self) -> None:
        resolver = CollectionNameResolver()
        assert resolver.parse_physical_name("memories") is None

    def test_is_physical_name(self) -> None:
        resolver = CollectionNameResolver()
        assert resolver.is_physical_name("memories@4096-abc123") is True
        assert resolver.is_physical_name("memories") is False

    def test_all_physical_names(self) -> None:
        resolver = CollectionNameResolver()
        names = resolver.all_physical_names("4096-abc")
        assert len(names) == len(EMBEDDING_COLLECTION_KINDS)
        for name in names:
            assert "@4096-abc" in name

    def test_all_active_aliases(self) -> None:
        resolver = CollectionNameResolver()
        aliases = resolver.all_active_aliases()
        assert set(aliases) == set(EMBEDDING_COLLECTION_KINDS)

    def test_default_kinds_include_github_issues(self) -> None:
        assert "gobby_github_issues" in EMBEDDING_COLLECTION_KINDS

    def test_default_kinds_exclude_skills(self) -> None:
        assert "skills" not in EMBEDDING_COLLECTION_KINDS


class TestSwitchJournal:
    """Test journal serialization."""

    def test_journal_roundtrip(self) -> None:
        journal = SwitchJournal(
            "4096-abc123",
            "qwen3-8b-q8",
            target_dim=4096,
            target_model="qwen3-embedding:8b-q8_0",
            target_query_prefix="Instruct: ...",
            target_api_base="http://localhost:11434/v1",
            provider="ollama",
            phase=PHASE_STAGING,
            started_at="2026-06-29T00:00:00Z",
            updated_at="2026-06-29T00:00:00Z",
            old_physical_names={"memories": "memories@old"},
        )
        data = journal.to_json()
        restored = SwitchJournal.from_json(data)
        assert restored.run_id == journal.run_id
        assert restored.catalog_key == journal.catalog_key
        assert restored.target_dim == journal.target_dim
        assert restored.phase == journal.phase
        assert restored.old_physical_names == {"memories": "memories@old"}


class TestSwitchStateMachine:
    """Test the switch state machine with a mock ConfigStore."""

    def _mock_store(self) -> MagicMock:
        store = MagicMock()
        store.get.return_value = None
        return store

    def test_start_switch_creates_journal(self) -> None:
        store = self._mock_store()
        journal, spec = start_switch(
            store,
            "qwen3-8b-q8",
            "ollama",
            current_dim=768,
            current_catalog_id="nomic-v1.5-f16",
        )
        assert journal.catalog_key == "qwen3-8b-q8"
        assert journal.target_dim == 4096
        assert journal.phase == PHASE_STAGING
        assert journal.old_dim == 768
        assert journal.old_catalog_id == "nomic-v1.5-f16"
        store.set.assert_called_once()

    def test_start_switch_can_clear_target_api_base(self) -> None:
        store = self._mock_store()
        journal, _ = start_switch(
            store,
            "qwen3-8b-q8",
            "openai",
            current_api_base="http://localhost:11434/v1",
            target_api_base=None,
        )
        assert journal.target_api_base is None

    def test_start_switch_rejects_unknown_key(self) -> None:
        store = self._mock_store()
        with pytest.raises(ValueError, match="Unknown embedding catalog key"):
            start_switch(store, "nonexistent", "ollama")

    def test_start_switch_rejects_active_switch(self) -> None:
        store = self._mock_store()
        # First switch starts fine
        start_switch(store, "qwen3-8b-q8", "ollama")
        # Second switch should fail — store.get returns the journal JSON
        store.get.return_value = SwitchJournal(
            "4096-abc",
            "qwen3-8b-q8",
            target_dim=4096,
            target_model="qwen3-embedding:8b-q8_0",
            target_query_prefix=None,
            target_api_base=None,
            provider="ollama",
            phase=PHASE_BUILDING,
            started_at="2026-06-29T00:00:00Z",
            updated_at="2026-06-29T00:00:00Z",
        ).to_json()
        with pytest.raises(SwitchAlreadyActiveError):
            start_switch(store, "qwen3-4b-q8", "ollama")

    def test_start_switch_rejects_invalid_journal_type(self) -> None:
        store = self._mock_store()
        store.get.return_value = {"run_id": "bad"}

        with pytest.raises(SwitchJournalStateError, match="Invalid embedding switch journal type"):
            start_switch(store, "qwen3-8b-q8", "ollama")

    def test_get_switch_status_rejects_malformed_journal(self) -> None:
        store = self._mock_store()
        store.get.return_value = "{not json"

        with pytest.raises(SwitchJournalStateError, match="Invalid embedding switch journal"):
            get_switch_status(store)

    def test_advance_phase_updates_journal(self) -> None:
        store = self._mock_store()
        journal, _ = start_switch(store, "qwen3-8b-q8", "ollama")
        journal.error = "previous failure"
        journal = advance_phase(store, journal, PHASE_BUILDING)
        assert journal.phase == PHASE_BUILDING
        assert journal.error is None
        store.set.assert_called()

    def test_record_switch_error_persists_error_without_advancing(self) -> None:
        store = self._mock_store()
        journal, _ = start_switch(store, "qwen3-8b-q8", "ollama")
        failed = record_switch_error(store, journal, "boom")
        assert failed.phase == PHASE_STAGING
        assert failed.error == "boom"
        store.set.assert_called()

    def test_complete_switch_deletes_journal(self) -> None:
        store = self._mock_store()
        journal, _ = start_switch(store, "qwen3-8b-q8", "ollama")
        complete_switch(store, journal)
        assert journal.phase == PHASE_GC
        store.delete.assert_called_once()

    def test_abort_switch_returns_journal(self) -> None:
        store = self._mock_store()
        journal, _ = start_switch(store, "qwen3-8b-q8", "ollama")
        store.get.return_value = journal.to_json()
        aborted = abort_switch(store)
        assert aborted is not None
        assert aborted.phase == PHASE_ABORTED

    def test_abort_switch_returns_none_when_no_active(self) -> None:
        store = self._mock_store()
        store.get.return_value = None
        assert abort_switch(store) is None

    def test_get_switch_status_returns_none_when_no_journal(self) -> None:
        store = self._mock_store()
        store.get.return_value = None
        assert get_switch_status(store) is None

    def test_build_physical_names(self) -> None:
        journal = SwitchJournal(
            "4096-abc",
            "qwen3-8b-q8",
            target_dim=4096,
            target_model="qwen3-embedding:8b-q8_0",
            target_query_prefix=None,
            target_api_base=None,
            provider="ollama",
            phase=PHASE_BUILDING,
            started_at="2026-06-29T00:00:00Z",
            updated_at="2026-06-29T00:00:00Z",
        )
        names = build_physical_names(journal)
        assert "memories" in names
        assert names["memories"] == "memories@4096-abc"
        assert "tool_embeddings" in names
        assert "gobby_github_issues" in names

    def test_active_alias_names(self) -> None:
        names = active_alias_names()
        assert "memories" in names
        assert names["memories"] == "memories"
        assert "gobby_github_issues" in names
