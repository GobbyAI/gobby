from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import TypeAdapter

import gobby.storage.config_repository as config_repository
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import Row

ALIASES_KEY = "ai.model_metadata_aliases"


def _values() -> Any:
    return ConfigRepository(MagicMock()).snapshot_from_rows(MagicMock(), 1, []).values


@pytest.mark.unit
def test_registry_defaults_build_type_adapters_once_per_registry() -> None:
    config_repository._json_defaults.cache_clear()
    built: list[object] = []

    def counting_adapter(annotation: object) -> TypeAdapter[object]:
        built.append(annotation)
        return TypeAdapter(annotation)

    with patch.object(config_repository, "TypeAdapter", counting_adapter):
        first = _values()
        first_read_builds = len(built)
        second = _values()

    assert first_read_builds > 0
    assert len(built) == first_read_builds
    assert dict(second) == dict(first)


@pytest.mark.unit
def test_snapshot_defaults_are_json_plain_and_isolated_between_reads() -> None:
    first = _values()
    aliases = first[ALIASES_KEY]
    assert isinstance(aliases, list)
    assert isinstance(aliases[0], dict)

    aliases[0]["mutated"] = True
    aliases.append({"mutated": True})

    second = _values()[ALIASES_KEY]
    assert len(second) == len(aliases) - 1
    assert "mutated" not in second[0]


@pytest.mark.unit
def test_stored_override_replaces_the_registry_default() -> None:
    rows: list[Row] = [{"key": ALIASES_KEY, "value": "[]", "revision": 1}]

    snapshot = ConfigRepository(MagicMock()).snapshot_from_rows(MagicMock(), 1, rows)

    assert snapshot.values[ALIASES_KEY] == []
    assert _values()[ALIASES_KEY] != []
