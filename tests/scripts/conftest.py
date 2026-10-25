"""Shared fakes for the community-label-decisions scripts (plan 3.1 and 3.3)."""

from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.ai import decisions

OPENROUTER_BASE = "https://openrouter.ai/api"
REQUEST_MODEL = "typesafe/jev-1.14"
RESPONSE_MODEL = "typesafe/jev-1.14-20261014"
ALIAS_MODEL = "typesafe/jev-latest"
BACKEND = f"response:{RESPONSE_MODEL}"
SECRET_NAME = "OPENROUTER_KEY"
SECRET_VALUE = "sk-test-NEVER-WRITE-9f3a"
BACKEND_MAX_STATE_TOKENS = 32000
PRIVACY = {"zdr": True, "data_collection": "deny"}
REAL_ASYNC_CLIENT = httpx.AsyncClient
REAL_CLIENT = httpx.Client


def resolve_secret(name: str) -> str:
    assert name == SECRET_NAME, name
    return SECRET_VALUE


def png(width: int = 8, height: int = 8) -> bytes:
    """A real grayscale PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    body += chunk(b"IDAT", zlib.compress(rows))
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def decision_flags() -> list[str]:
    return [
        "--wire-api",
        "openrouter-decisions",
        "--api-base",
        OPENROUTER_BASE,
        "--model",
        REQUEST_MODEL,
        "--api-key-secret",
        SECRET_NAME,
    ]


class OpenRouterWire:
    """OpenRouter decisions endpoint that answers every shape the scripts send.

    ``usage.input_tokens`` is the request length over four, and a request above
    ``BACKEND_MAX_STATE_TOKENS`` is refused, so the limit probes see a real ceiling.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.cost_per_token = 1e-8
        self.refused_kinds: set[str] = set()
        self.response_model: Callable[[dict[str, Any]], str] = lambda _sent: RESPONSE_MODEL
        self.choice: Callable[[str], tuple[str, float]] = lambda _key: ("generated", 0.9)
        self.noul: Callable[[str], float] = lambda _key: 0.9
        self.score_top = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            assert request.url.params.get("output_modalities") == "decisions", request.url
            listing = [{"id": REQUEST_MODEL, "canonical_slug": RESPONSE_MODEL}]
            return httpx.Response(200, json={"data": listing})
        sent = json.loads(request.content)
        input_tokens = len(request.content) // 4
        if input_tokens > BACKEND_MAX_STATE_TOKENS:
            return httpx.Response(400, json={"error": {"message": "context length exceeded"}})
        if {q["type"] for q in sent["questions"].values()} & self.refused_kinds:
            return httpx.Response(400, json={"error": {"message": "question refused"}})
        answers = {key: self._answer(key, q) for key, q in sent["questions"].items()}
        return httpx.Response(
            200,
            json={
                "id": f"dec_{len(self.requests):06d}",
                "model": self.response_model(sent),
                "provider": "TypeSafe",
                "answers": answers,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": len(answers),
                    "cost": input_tokens * self.cost_per_token,
                },
            },
        )

    def _answer(self, key: str, question: dict[str, Any]) -> dict[str, Any]:
        kind = question["type"]
        if kind == "choice":
            options = list(question["criteria"])
            option, confidence = self.choice(key)
            option = option if option in options else options[0]
            probabilities = dict.fromkeys(options, 0.0)
            probabilities[option] = 1.0
            return {
                "type": "choice",
                "choice": option,
                "probabilities": probabilities,
                "confidence": confidence,
            }
        if kind == "noul":
            return {"type": "noul", "noul": self.noul(key)}
        if kind == "score":
            levels = list(question["criteria"])
            chosen = len(levels) - 1 if self.score_top else 0
            return {
                "type": "score",
                "score": float(chosen),
                "confidence": 0.9,
                "legend": {str(index): level for index, level in enumerate(levels)},
                "probabilities": {
                    str(index): float(index == chosen) for index in range(len(levels))
                },
            }
        assert kind == "box", kind
        return {
            "type": "box",
            "box": {"top": 0.1, "bottom": 0.9, "left": 0.2, "right": 0.8},
            "confidence": 0.9,
        }

    def posts(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def kinds_sent(self) -> set[str]:
        return {q["type"] for body in self.posts() for q in body.get("questions", {}).values()}


@pytest.fixture
def openrouter_wire(monkeypatch: pytest.MonkeyPatch) -> OpenRouterWire:
    wire = OpenRouterWire()

    def async_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(wire.handle), **kwargs)

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return REAL_CLIENT(transport=httpx.MockTransport(wire.handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(decisions, "_service_cache", None)
    return wire


def write_png(directory: Path) -> Path:
    path = directory / "box.png"
    path.write_bytes(png())
    return path
