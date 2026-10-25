"""Tests for the operator-run decisions live capture (community-label-decisions 3.1)."""

from __future__ import annotations

import ast
import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import decisions_live_capture
from tests.scripts.conftest import (
    ALIAS_MODEL,
    BACKEND_MAX_STATE_TOKENS,
    PRIVACY,
    REQUEST_MODEL,
    RESPONSE_MODEL,
    SECRET_VALUE,
    OpenRouterWire,
    decision_flags,
    png,
    resolve_secret,
    write_png,
)

pytestmark = pytest.mark.unit

# Modules that read the daemon's live configuration; the capture builds its own config.
LIVE_CONFIG_MODULES = {
    "gobby.cli.runtime",
    "gobby.config.runtime",
    "gobby.config.values",
    "gobby.storage.config_repository",
}


def _capture(tmp_path: Path) -> tuple[int, str]:
    out = tmp_path / "capture.md"
    argv = [
        *decision_flags(),
        "--identity-contract",
        "response_version",
        "--backend-max-state-tokens",
        str(BACKEND_MAX_STATE_TOKENS),
        "--alias",
        ALIAS_MODEL,
        "--box-image",
        str(write_png(tmp_path)),
        "--out",
        str(out),
    ]
    code = decisions_live_capture.main(argv, resolve_secret=resolve_secret)
    return code, out.read_text(encoding="utf-8")


def test_capture_records_every_shape_and_probe(
    tmp_path: Path, openrouter_wire: OpenRouterWire
) -> None:
    # Score fails; the shapes around it still run because each gets a fresh service.
    openrouter_wire.refused_kinds = {"score"}

    code, section = _capture(tmp_path)

    assert code == 0
    assert openrouter_wire.kinds_sent() == {"choice", "noul", "score", "box"}
    posts = openrouter_wire.posts()
    assert all(body["provider"] == PRIVACY for body in posts)
    assert {body["model"] for body in posts} == {REQUEST_MODEL, ALIAS_MODEL}
    listing = [r for r in openrouter_wire.requests if r.method == "GET"]
    assert [r.url.path for r in listing] == ["/api/v1/models"]
    sizes = [len(r.content) for r in openrouter_wire.requests if r.method == "POST"]
    ceiling = BACKEND_MAX_STATE_TOKENS * 4
    assert any(ceiling // 2 <= size <= ceiling for size in sizes), "just-under probe"
    assert any(size > ceiling for size in sizes), "over-limit probe"

    lowered = section.lower()
    assert datetime.now(UTC).date().isoformat() in section
    assert RESPONSE_MODEL in section
    assert f"response:{RESPONSE_MODEL}" in section
    for shape in ("choice", "noul", "score", "box"):
        assert shape in lowered, shape
    assert "http_status" in section, "Score finding reason"
    assert "chars-per-token" in lowered
    assert ALIAS_MODEL in section
    assert "cost" in lowered


def test_capture_output_omits_key_and_image(
    tmp_path: Path, openrouter_wire: OpenRouterWire
) -> None:
    code, section = _capture(tmp_path)

    assert code == 0
    authorized = {
        r.headers.get("authorization") for r in openrouter_wire.requests if r.method == "POST"
    }
    assert authorized == {f"Bearer {SECRET_VALUE}"}
    image = png()
    assert SECRET_VALUE not in section
    assert base64.b64encode(image).decode() not in section
    assert image.decode("latin-1") not in section

    script = Path(decisions_live_capture.__file__)
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported.isdisjoint(LIVE_CONFIG_MODULES)
