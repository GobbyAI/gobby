"""Normalizer vectors: replay each recorded entry point and compare its scrubbed output.

A vector names a dotted ``entry`` (a module-level function under ``gobby``) and an ``input``
of positional ``args`` and keyword ``kwargs``, all JSON-native. Optional fields:

- ``setup``: a throwaway checkout built for each replay. ``git_repo`` runs ``git init``;
  ``files`` maps relative paths to text, or to ``{"repeat", "count", "suffix"}``;
  ``gitlinks`` lists submodule-style gitlink paths. The token ``{repo}`` in any input
  string is replaced with that checkout's path.
- ``prepare``: entry names called in order on the first argument before the entry runs,
  mutating it in place the way the hook pipeline normalizes a tool call.
- ``construct``: ``{"index", "class", "data"}`` builds argument ``index`` from JSON.
  Pydantic models validate ``data``; dataclasses take it as keyword arguments, with
  ``*_at`` strings parsed as datetimes.
- ``result_from_arg``: the index of an argument to return instead of the call's result,
  for functions that mutate their arguments and return None.
- ``negative_from``: the test function whose assertion the vector replays.

A function that raises is recorded as ``{"raised": <type name>}``. The expect block is
the output scrubbed by ``rules_recorder._jsonable``, so volatile values and checkout
paths compare equal across runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.contracts import rules_recorder

RECORD_ENV = "GOBBY_CORPUS_RECORD"
REPO_TOKEN = "{repo}"
GITLINK_SHA = "1" * 40


def resolve_entry(entry: str) -> Any:
    """Import the module that holds ``entry`` and return the attribute it names."""
    module_name, _, attribute = entry.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def _substitute(value: Any, repo: Path | None) -> Any:
    if repo is None:
        return value
    if isinstance(value, str):
        return value.replace(REPO_TOKEN, str(repo))
    if isinstance(value, list):
        return [_substitute(item, repo) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, repo) for key, item in value.items()}
    return value


def materialize(setup: dict[str, Any], root: Path) -> None:
    """Build the checkout a vector's ``setup`` describes under ``root``."""
    if setup.get("git_repo"):
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    for relative, content in setup.get("files", {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            text = content["repeat"] * content["count"] + content.get("suffix", "")
        else:
            text = content
        target.write_text(text)
    for relative in setup.get("gitlinks", []):
        cacheinfo = f"160000,{GITLINK_SHA},{relative}"
        subprocess.run(
            ["git", "-C", str(root), "update-index", "--add", "--cacheinfo", cacheinfo],
            check=True,
        )


def _build(spec: dict[str, Any]) -> Any:
    cls = resolve_entry(spec["class"])
    data = dict(spec["data"])
    if hasattr(cls, "model_validate"):
        return cls.model_validate(data)
    if dataclasses.is_dataclass(cls):
        for key, value in data.items():
            if key.endswith("_at") and isinstance(value, str):
                data[key] = datetime.fromisoformat(value)
    return cls(**data)


async def _await(awaitable: Any) -> Any:
    return await awaitable


def _call(vector: dict[str, Any], repo: Path | None) -> Any:
    arguments = _substitute(vector.get("input", {}), repo)
    args = list(arguments.get("args", []))
    kwargs = dict(arguments.get("kwargs", {}))
    try:
        for spec in vector.get("construct", []):
            args[spec["index"]] = _build(spec)
        for name in vector.get("prepare", []):
            resolve_entry(name)(args[0])
        result = resolve_entry(vector["entry"])(*args, **kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(_await(result))
    except Exception as exc:
        return {"raised": type(exc).__name__}
    if "result_from_arg" in vector:
        result = args[vector["result_from_arg"]]
    return rules_recorder._jsonable(result)


def replay_vector(vector: dict[str, Any]) -> Any:
    """Run a vector's entry point on its recorded input and return the scrubbed output."""
    if not vector.get("setup"):
        return _call(vector, None)
    with tempfile.TemporaryDirectory(prefix="gobby-vector-") as scratch:
        # Resolve the checkout path so it matches the form the normalizer stores (/private/var).
        root = Path(scratch).resolve() / "repo"
        root.mkdir()
        materialize(vector["setup"], root)
        return _call(vector, root)


def record_vectors(vectors_path: Path) -> None:
    """Fill each vector's ``expect`` with the observed output when recording is requested."""
    if os.environ.get(RECORD_ENV) != "1":
        return
    document = json.loads(vectors_path.read_text())
    for vector in document["vectors"]:
        vector["expect"] = replay_vector(vector)
    vectors_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
