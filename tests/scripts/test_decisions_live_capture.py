"""Tests for the operator-run decisions live capture (community-label-decisions 3.1)."""

from __future__ import annotations

import ast
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
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


def _capture(
    tmp_path: Path, wire_api: str = "openrouter-decisions", *extra: str
) -> tuple[int, str]:
    out = tmp_path / "capture.md"
    argv = [
        *decision_flags(),
        "--wire-api",
        wire_api,
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
        *extra,
    ]
    code = decisions_live_capture.main(argv, resolve_secret=resolve_secret)
    return code, out.read_text(encoding="utf-8")


@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
def test_capture_long_context_is_explicit_on_shapes_and_probes(
    tmp_path: Path, openrouter_wire: OpenRouterWire, wire_api: str
) -> None:
    code, section = _capture(tmp_path, wire_api, "--long-context")
    assert code == 0
    requests = [r for r in openrouter_wire.requests if r.method == "POST"]
    assert len(requests) == 10
    assert all(r.headers.get("TypeSafe-Danger-Zone-Long-Context") == "true" for r in requests)
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    assert capture["long_context"] is True
    assert capture["backend_max_question_tokens"] == 262144


@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
def test_capture_retains_bounded_redacted_score_parse_diagnostics(
    tmp_path: Path,
    openrouter_wire: OpenRouterWire,
    monkeypatch: pytest.MonkeyPatch,
    wire_api: str,
) -> None:
    original = openrouter_wire.handle
    image = base64.b64encode(png()).decode()

    def inconsistent(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "POST" and "clarity" in json.loads(request.content)["questions"]:
            payload = response.json()
            answer = payload["answers"]["clarity"]
            answer.update(score=1.95, probabilities={"0": 0.0, "1": 0.04, "2": 0.96})
            answer["debug"] = f"{SECRET_VALUE} {image} " + "x" * 5000
            return httpx.Response(200, json=payload)
        return response

    monkeypatch.setattr(openrouter_wire, "handle", inconsistent)
    code, section = _capture(tmp_path, wire_api)
    assert code == 0
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    score = next(s for s in capture["shapes"] if s["shape"] == "score")
    assert score["finding"]["reason"] == "parse"
    detail = score.get("parse_diagnostics")
    assert detail is not None, "failed validation must retain the numeric answer"
    assert len(detail["answers"]) <= 4096
    comparison = detail["score_comparison"]["clarity"]
    assert comparison["score"] == 1.95
    assert comparison["probabilities"] == {"0": 0.0, "1": 0.04, "2": 0.96}
    assert comparison["expected"] == pytest.approx(1.96)
    assert comparison["abs_tolerance"] == 0.002
    assert comparison["rel_tolerance"] == 1e-9
    assert SECRET_VALUE not in section
    assert image not in section
    assert all("parse_diagnostics" not in s for s in capture["shapes"] if s["shape"] != "score")


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
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    probes = {probe["probe"]: probe for probe in capture["probes"]}
    assert probes["just-under"]["status"] == 200
    assert probes["over-limit"]["status"] == 400
    assert capture["measured_chars_per_token"] == pytest.approx(4.0)

    lowered = section.lower()
    run_dates = {datetime.now(UTC).date().isoformat(), datetime.now().date().isoformat()}
    assert any(d in section for d in run_dates)
    assert RESPONSE_MODEL in section
    assert f"response:{RESPONSE_MODEL}" in section
    for shape in ("choice", "noul", "score", "box"):
        assert shape in lowered, shape
    assert "http_status" in section, "Score finding reason"
    assert "chars-per-token" in lowered
    assert ALIAS_MODEL in section
    assert "cost" in lowered


@pytest.mark.parametrize("token_counts", [(1024, 1024), (2048, 1024), (1024, None)])
def test_capture_reports_unmeasured_ratio_for_unusable_calibration(
    tmp_path: Path,
    openrouter_wire: OpenRouterWire,
    monkeypatch: pytest.MonkeyPatch,
    token_counts: tuple[int, int | None],
) -> None:
    handle = openrouter_wire.handle

    def calibration_response(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.method == "POST" and response.is_success:
            body = json.loads(request.content)
            size = len(body["state"].get("text", ""))
            if size in (4096, 8192):
                payload = response.json()
                tokens = token_counts[(4096, 8192).index(size)]
                payload["usage"] = {} if tokens is None else {"input_tokens": tokens}
                return httpx.Response(200, json=payload)
        return response

    monkeypatch.setattr(openrouter_wire, "handle", calibration_response)

    code, section = _capture(tmp_path)

    assert code == 0
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    assert capture["measured_chars_per_token"] is None
    probes = {probe["probe"]: probe for probe in capture["probes"]}
    assert probes["just-under"]["status"] == 200
    assert probes["over-limit"]["status"] == 400


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


@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
def test_capture_box_failure_does_not_block_raw_probes(
    tmp_path: Path, openrouter_wire: OpenRouterWire, wire_api: str
) -> None:
    openrouter_wire.refused_kinds = {"box"}

    code, section = _capture(tmp_path, wire_api)

    assert code == 0
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    box = next(shape for shape in capture["shapes"] if shape["shape"] == "box")
    assert box["finding"] == {"reason": "http_status", "detail": "HTTP 400"}
    probe_requests = [body for body in openrouter_wire.posts() if "probe" in body["questions"]]
    assert len(probe_requests) == 6
    assert [probe["status"] for probe in capture["probes"]] == [200, 200, 200, 400, 200, 200]
    assert capture["measured_chars_per_token"] == pytest.approx(4.0)
    assert capture["alias_matches_pinned_response"] is True


@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
def test_capture_records_bounded_redacted_http_errors(
    tmp_path: Path,
    openrouter_wire: OpenRouterWire,
    monkeypatch: pytest.MonkeyPatch,
    wire_api: str,
) -> None:
    original = openrouter_wire.handle
    image = base64.b64encode(png()).decode()

    def fail(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            questions = json.loads(request.content)["questions"]
            if "square" in questions or "probe" in questions:
                openrouter_wire.requests.append(request)
                status = 400 if "square" in questions else 500
                text = f"backend explanation {SECRET_VALUE} {image} " + "x" * 2000
                return httpx.Response(
                    status,
                    text=text,
                    headers={"content-type": "text/plain", "x-private": SECRET_VALUE},
                )
        return original(request)

    monkeypatch.setattr(openrouter_wire, "handle", fail)
    code, section = _capture(tmp_path, wire_api)

    assert code == 0
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    box = next(shape for shape in capture["shapes"] if shape["shape"] == "box")
    failures = [*box["exchanges"], *capture["probes"]]
    assert [failure["status"] for failure in failures] == [400, 500, 500, 500, 500, 500, 500]
    for failure in failures:
        detail = failure.get("response_error")
        assert detail is not None, "HTTP failure must retain a bounded diagnostic body"
        assert set(detail) == {"content_type", "body"}
        assert detail["content_type"] == "text/plain"
        assert detail["body"].startswith("backend explanation [redacted]")
        assert len(detail["body"]) == 1024
    assert SECRET_VALUE not in section
    assert image not in section
    assert "x-private" not in section


def test_capture_transport_failure_has_no_http_status(
    tmp_path: Path, openrouter_wire: OpenRouterWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = openrouter_wire.handle

    def timeout(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "probe" in json.loads(request.content)["questions"]:
            raise httpx.ReadTimeout("local timeout", request=request)
        return original(request)

    monkeypatch.setattr(openrouter_wire, "handle", timeout)
    code, section = _capture(tmp_path)

    assert code == 0
    capture = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    assert len(capture["probes"]) == 6
    for probe in capture["probes"]:
        assert "status" not in probe
        assert probe["finding"] == "ReadTimeout: probe did not return valid JSON"


def test_catalog_lists_only_matching_public_identifiers(
    openrouter_wire: OpenRouterWire,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def catalog(request: httpx.Request) -> httpx.Response:
        openrouter_wire.requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://openrouter.ai/api/v1/models?output_modalities=decisions"
        assert request.headers["authorization"] == f"Bearer {SECRET_VALUE}"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": REQUEST_MODEL, "canonical_slug": RESPONSE_MODEL, "key": SECRET_VALUE},
                    {"id": RESPONSE_MODEL, "canonical_slug": RESPONSE_MODEL},
                    {"id": ALIAS_MODEL, "canonical_slug": RESPONSE_MODEL, "extra": SECRET_VALUE},
                    {"id": "typesafe/jev-1.140", "canonical_slug": "typesafe/jev-1.140-dated"},
                    {"id": "other/model", "canonical_slug": "other/model-dated"},
                ]
            },
        )

    monkeypatch.setattr(openrouter_wire, "handle", catalog)
    code: int | str | None
    try:
        code = decisions_live_capture.main(
            [*decision_flags(), "--list-catalog"], resolve_secret=resolve_secret
        )
    except SystemExit as error:
        code = error.code
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert len(openrouter_wire.requests) == 1
    assert json.loads(captured.out) == [
        {"id": REQUEST_MODEL, "canonical_slug": RESPONSE_MODEL},
        {"id": RESPONSE_MODEL, "canonical_slug": RESPONSE_MODEL},
        {"id": ALIAS_MODEL, "canonical_slug": RESPONSE_MODEL},
    ]
    assert captured.err == ""
    assert SECRET_VALUE not in captured.out


def test_catalog_http_failure_does_not_print_credentials(
    openrouter_wire: OpenRouterWire,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        openrouter_wire.requests.append(request)
        assert request.method == "GET"
        return httpx.Response(500, text=f"provider echoed {SECRET_VALUE}")

    monkeypatch.setattr(openrouter_wire, "handle", unavailable)
    code: int | str | None
    try:
        code = decisions_live_capture.main(
            [*decision_flags(), "--list-catalog"], resolve_secret=resolve_secret
        )
    except SystemExit as error:
        code = error.code
    captured = capsys.readouterr()
    assert code == 1
    assert len(openrouter_wire.requests) == 1
    assert captured.out == ""
    assert "HTTP 500" in captured.err
    assert SECRET_VALUE not in captured.err
