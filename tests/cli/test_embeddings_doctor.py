from __future__ import annotations

import hashlib
import importlib
import json

import pytest
from click.testing import CliRunner

from gobby.config.app import DaemonConfig
from gobby.config.embedding_keys import AI_EMBEDDINGS_CONFIG_PREFIX

pytestmark = pytest.mark.unit

embeddings_module = importlib.import_module("gobby.cli.embeddings")


def test_doctor_json_redacts_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        embeddings_module, "_resolved_namespace", lambda: AI_EMBEDDINGS_CONFIG_PREFIX
    )
    config = DaemonConfig(
        embeddings={
            "api_base": "http://localhost:1234/v1",
            "model": "nomic-embed-text",
            "dim": 768,
            "api_key": "sk-test",
        }
    )

    result = CliRunner().invoke(embeddings_module.embeddings, ["doctor"], obj={"config": config})

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "endpoint": "http://localhost:1234/v1",
        "model": "nomic-embed-text",
        "dim": 768,
        "api_key_present": True,
        "api_key_fingerprint": hashlib.sha256(b"sk-test").hexdigest()[:8],
        "namespace_resolved": AI_EMBEDDINGS_CONFIG_PREFIX,
        "source": "config_store",
        "agrees": None,
        "drift": None,
    }


def test_doctor_exits_10_when_namespace_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_module, "_resolved_namespace", lambda: None)

    result = CliRunner().invoke(
        embeddings_module.embeddings, ["doctor"], obj={"config": DaemonConfig()}
    )

    assert result.exit_code == 10
    payload = json.loads(result.output)
    assert payload["namespace_resolved"] is None
