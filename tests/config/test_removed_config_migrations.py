import importlib

import pytest

from gobby.config import _loading
from gobby.config.code_index import CodeIndexConfig


@pytest.mark.parametrize(
    ("owner", "symbol"),
    (
        (_loading, "_migrate_legacy_config"),
        (_loading, "_drop_legacy_embedding_config_store_keys"),
        (_loading, "_migrate_code_index_symbol_summary_config_store_keys"),
        (_loading, "_drop_removed_config_store_keys"),
        (_loading, "_migrate_default_ui_mode_config_store_row"),
        ("gobby.config.feature_candidate_defaults", "delete_stale_default_feature_candidate_rows"),
        (CodeIndexConfig, "drop_removed_keys"),
    ),
)
def test_legacy_config_migration_helper_is_removed(owner: object, symbol: str) -> None:
    if isinstance(owner, str):
        spec = importlib.util.find_spec(owner)
        owner = importlib.import_module(owner) if spec is not None else None
    assert owner is None or not hasattr(owner, symbol)


def test_legacy_wiki_migration_module_is_removed() -> None:
    assert importlib.util.find_spec("gobby.config.wiki_migration") is None


@pytest.mark.parametrize(
    "module_name",
    (
        "gobby.build",
        "gobby.dispatch",
        "gobby.config.build",
        "gobby.storage.build_profiles",
        "gobby.storage.build_history",
    ),
)
def test_retired_build_modules_are_unavailable(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None
