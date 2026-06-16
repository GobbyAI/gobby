"""Tests for code-index configuration compatibility."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


def test_code_index_config_drops_deprecated_vector_batch_size() -> None:
    config = CodeIndexConfig.model_validate(
        {
            "sync_worker_vector_batch_size": 12,
            "sync_worker_batch_size": 3,
        }
    )

    assert config.sync_worker_batch_size == 3
    assert "sync_worker_vector_batch_size" not in config.model_dump()


def test_code_index_config_still_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CodeIndexConfig.model_validate({"unknown_field": 1})
