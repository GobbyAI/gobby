"""Tests for the shared wiki vault resolver (Python mirror of gobby_core::vault)."""

from pathlib import Path

import pytest

from gobby.utils.wiki_vault import (
    DEFAULT_VAULT_DIR,
    FALLBACK_VAULT_DIR,
    SCOPE_FILE,
    STATE_ROOT,
    existing_vault_dir,
    is_vault,
    resolve_vault_dir,
)

pytestmark = pytest.mark.unit


def _make_vault(directory: Path) -> None:
    (directory / STATE_ROOT).mkdir(parents=True)
    (directory / STATE_ROOT / SCOPE_FILE).write_text("{}\n", encoding="utf-8")


def test_is_vault_requires_the_scope_file_not_just_the_state_dir(tmp_path: Path) -> None:
    vault = tmp_path / DEFAULT_VAULT_DIR
    (vault / STATE_ROOT).mkdir(parents=True)

    assert not is_vault(vault)

    (vault / STATE_ROOT / SCOPE_FILE).write_text("{}\n", encoding="utf-8")
    assert is_vault(vault)


def test_fresh_project_resolves_to_wiki(tmp_path: Path) -> None:
    assert resolve_vault_dir(tmp_path) == tmp_path / DEFAULT_VAULT_DIR


def test_existing_wiki_vault_wins(tmp_path: Path) -> None:
    _make_vault(tmp_path / DEFAULT_VAULT_DIR)
    _make_vault(tmp_path / FALLBACK_VAULT_DIR)

    assert resolve_vault_dir(tmp_path) == tmp_path / DEFAULT_VAULT_DIR


def test_non_vault_wiki_collision_falls_back_to_gobby_wiki(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).mkdir()

    assert resolve_vault_dir(tmp_path) == tmp_path / FALLBACK_VAULT_DIR


def test_wiki_collision_as_plain_file_also_falls_back(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).write_text("not a directory\n", encoding="utf-8")

    assert resolve_vault_dir(tmp_path) == tmp_path / FALLBACK_VAULT_DIR


def test_existing_gobby_wiki_vault_wins_over_fresh_creation(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).mkdir()
    _make_vault(tmp_path / FALLBACK_VAULT_DIR)

    assert resolve_vault_dir(tmp_path) == tmp_path / FALLBACK_VAULT_DIR


def test_occupied_gobby_wiki_advances_to_numbered_fallback(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).mkdir()
    (tmp_path / FALLBACK_VAULT_DIR).mkdir()

    assert resolve_vault_dir(tmp_path) == tmp_path / f"{FALLBACK_VAULT_DIR}-001"


def test_numbered_fallback_prefers_an_existing_vault_over_a_fresh_slot(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).mkdir()
    (tmp_path / FALLBACK_VAULT_DIR).mkdir()
    _make_vault(tmp_path / f"{FALLBACK_VAULT_DIR}-001")

    assert resolve_vault_dir(tmp_path) == tmp_path / f"{FALLBACK_VAULT_DIR}-001"


def test_numbered_fallback_skips_occupied_slots(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).mkdir()
    (tmp_path / FALLBACK_VAULT_DIR).mkdir()
    (tmp_path / f"{FALLBACK_VAULT_DIR}-001").mkdir()

    assert resolve_vault_dir(tmp_path) == tmp_path / f"{FALLBACK_VAULT_DIR}-002"


def test_existing_vault_dir_returns_only_initialized_vaults(tmp_path: Path) -> None:
    assert existing_vault_dir(tmp_path) is None

    _make_vault(tmp_path / DEFAULT_VAULT_DIR)
    assert existing_vault_dir(tmp_path) == tmp_path / DEFAULT_VAULT_DIR


def test_existing_vault_dir_ignores_gobby_wiki_vault_when_wiki_is_free(tmp_path: Path) -> None:
    _make_vault(tmp_path / FALLBACK_VAULT_DIR)

    assert existing_vault_dir(tmp_path) is None


def test_existing_vault_dir_honors_fallback_vault_behind_a_collision(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_VAULT_DIR).mkdir()
    _make_vault(tmp_path / FALLBACK_VAULT_DIR)

    assert existing_vault_dir(tmp_path) == tmp_path / FALLBACK_VAULT_DIR
