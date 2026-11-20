"""Normalizer vectors: replay each recorded entry point and compare its scrubbed output.

A vector names a dotted ``entry`` (a module-level function under ``gobby``) and an ``input``
of positional ``args`` and keyword ``kwargs``, all JSON-native. Optional fields:

- ``setup``: a throwaway checkout built for each replay. ``git_repo`` runs ``git init``;
  ``files`` maps relative paths to text, or to ``{"repeat", "count", "suffix"}``;
  ``gitlinks`` lists submodule-style gitlink paths; ``nested_repo`` names a directory that
  gets a second ``git init``; ``symlinks`` maps a relative link to its relative target. The
  token ``{repo}`` in any input string is replaced with that checkout's path, and every
  recorded string holding that path names it ``{repo}`` again. A fixture replay runs with
  HOME, USERPROFILE and GOBBY_HOME pointed at an empty home beside the checkout, under a
  scratch root in the system ``/tmp``, so it reads nothing of the host home directory.
- ``prepare``: entry names called in order on the first argument before the entry runs,
  mutating it in place the way the hook pipeline normalizes a tool call.
- ``construct``: ``{"index", "class", <source>}`` builds argument ``index``. The source is
  exactly one of ``data`` (a mapping: Pydantic models validate it, dataclasses take it as
  keyword arguments with ``*_at`` strings parsed as datetimes), ``args`` (positional
  arguments, as ``pathlib.Path`` takes) or ``items`` (a list of ``data`` mappings, built
  into a list of objects).
- A derived argument is written ``{"from": {"entry", "args", "kwargs", "pick"}}``: the
  production builder ``entry`` is called on its arguments (each may itself be derived,
  resolved depth-first) and the result is taken through each ``pick`` element in turn (an
  int indexes, a string reads that attribute).
- Argument coercion: when the evaluated annotation of a parameter is a ``tuple``, ``set`` or
  ``frozenset`` (also inside ``X | None``), a list argument is passed as that type.
- ``result_from_arg``: the index of an argument to record as the result instead of the
  call's return value, for functions that mutate their arguments and return None.
- ``scan_budget``: ``{"target": "<module>.<scanner>", "factor": N}``. The scanner named by
  ``target`` is metered during the call; the expect block then carries ``result`` plus
  ``scan_within_budget`` (characters scanned at most N times the input length) and
  ``scan_observed`` (at least the input length was scanned).
- ``scope``: ``{"node_delivery": {"machine_id", "hub_machine_id", "cwd", "files"}}`` runs the
  call inside a node-origin delivery answered by a scripted node (``files`` maps a node path
  to its text), with the hub's machine id patched to ``hub_machine_id``. The expect block
  carries ``node_requests``, the checkout reads the call sent. An argument written
  ``{"scope_loop": true}`` is replaced by the scope's event loop; when the entry is
  ``delivery_scope`` itself, the scope supplies only that loop and the scripted node.
- ``within``: zero-argument dotted entries called inside the context manager the entry
  returns; the expect block carries their values as ``within``.
- ``negative_from``: the negative test the vector serves. A vector replays either the
  test's own entry call or the normalizer step that builds the state the test asserts on.
- ``metadata``: ``{"reason": ...}``, a one-line note on why the vector's entry has a parameter
  outside the builtin set, or ``{"precondition": ...}`` on a vector that records a raise its
  callers never reach. Replay ignores it.

An argument that JSON cannot carry as text is written ``{"bytes": "<text>"}``; replay passes
its UTF-8 encoding as a ``bytes`` value.

Every argument is snapshot in scrubbed form after the ``prepare`` steps and again after the
call. When the call changed one, the expect block is ``{"result": ..., "mutated_args":
{"<index or keyword>": <value after the call>}}``; the ``result_from_arg`` argument is not
listed there, since it is the result. With no envelope key the expect block is the bare
result.

A function that raises is recorded as ``{"raised": <type name>}``. Names, prepare steps,
constructed and derived arguments resolve before the call, so a missing entry point is an
error, not a recorded output. A returned context manager is entered and its yielded value is
recorded. The expect block is the output scrubbed by ``rules_recorder._jsonable``, so volatile
values and checkout paths compare equal across runs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import dataclasses
import importlib
import inspect
import json
import os
import posixpath
import subprocess
import tempfile
import threading
import types
import typing
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tests.contracts import rules_recorder

RECORD_ENV = "GOBBY_CORPUS_RECORD"
REPO_TOKEN = "{repo}"
BYTES_KEY = "bytes"
FROM_KEY = "from"
SCOPE_LOOP_KEY = "scope_loop"
GITLINK_SHA = "1" * 40
# Fixture checkouts live under the system /tmp, one of the temp roots the path-scope helpers
# always accept, so a host TMPDIR (which may sit inside a CLI scratchpad) changes nothing.
FIXTURE_TEMP_ROOT = "/tmp"
CONSTRUCT_SOURCES = ("data", "args", "items")
NODE_PROJECT_ID = "synthetic-project"
NODE_HOME = "/synthetic-home"
NODE_TEMP_DIR = "/synthetic-tmp"
DELIVERY_SCOPE_ENTRY = "gobby.hooks.checkout_reads.delivery_scope"


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
    nested = setup.get("nested_repo")
    if nested:
        (root / nested).mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(root / nested)], check=True)
    for link, target in setup.get("symlinks", {}).items():
        (root / link).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, root / link)


def _from_data(cls: Any, data: dict[str, Any]) -> Any:
    data = dict(data)
    if hasattr(cls, "model_validate"):
        return cls.model_validate(data)
    if dataclasses.is_dataclass(cls):
        for key, value in data.items():
            if key.endswith("_at") and isinstance(value, str):
                data[key] = datetime.fromisoformat(value)
    return cls(**data)


def _build(spec: dict[str, Any], repo: Path | None) -> Any:
    """Build one constructed argument from exactly one source: data, args or items."""
    spec = _substitute(spec, repo)
    cls = resolve_entry(spec["class"])
    sources = [key for key in CONSTRUCT_SOURCES if spec.get(key)]
    if len(sources) != 1:
        raise ValueError(f"construct {spec.get('index')} needs one source, has {sources}")
    source = sources[0]
    if source == "data" and isinstance(spec["data"], dict):
        return _from_data(cls, spec["data"])
    if source == "args" and isinstance(spec["args"], list):
        return cls(*spec["args"])
    if source == "items" and isinstance(spec["items"], list):
        return [_from_data(cls, item) for item in spec["items"]]
    raise ValueError(f"construct {spec.get('index')} has a malformed {source!r} source")


def _derive(spec: dict[str, Any], repo: Path | None) -> Any:
    """Call a production builder on resolved arguments and take its result through ``pick``."""
    builder = resolve_entry(spec["entry"])
    args = [_resolve(item, repo) for item in spec.get("args", [])]
    kwargs = {key: _resolve(item, repo) for key, item in spec.get("kwargs", {}).items()}
    value = builder(*args, **kwargs)
    for step in spec.get("pick", []):
        value = getattr(value, step) if isinstance(step, str) else value[step]
    return value


def _resolve(value: Any, repo: Path | None) -> Any:
    """Return an input argument as the call receives it: decoded bytes or a derived value."""
    if isinstance(value, dict) and list(value) == [BYTES_KEY]:
        return str(value[BYTES_KEY]).encode("utf-8")
    if isinstance(value, dict) and list(value) == [FROM_KEY]:
        return _derive(value[FROM_KEY], repo)
    return _substitute(value, repo)


def _collection_origin(annotation: Any) -> Any:
    """``tuple``, ``set`` or ``frozenset`` when the annotation (or a union member) is one."""
    origin = typing.get_origin(annotation) or annotation
    if origin in (tuple, set, frozenset):
        return origin
    if origin in (typing.Union, types.UnionType):
        for member in typing.get_args(annotation):
            found = _collection_origin(member)
            if found is not None:
                return found
    return None


def _coerce(entry: Callable[..., Any], args: list[Any], kwargs: dict[str, Any]) -> None:
    """Pass a list as a tuple or set where the evaluated parameter annotation asks for one."""
    try:
        hints = typing.get_type_hints(entry)
        names = list(inspect.signature(entry).parameters)
    except (NameError, TypeError, ValueError, AttributeError):
        return

    def coerced(name: str | None, value: Any) -> Any:
        origin = _collection_origin(hints.get(name)) if name else None
        if origin is not None and isinstance(value, list):
            return origin(value)
        return value

    for index, value in enumerate(args):
        args[index] = coerced(names[index] if index < len(names) else None, value)
    for key, value in kwargs.items():
        kwargs[key] = coerced(key, value)


async def _await(awaitable: Any) -> Any:
    return await awaitable


class _ScanMeter:
    """Counts the characters a metered scanner receives during one replay."""

    def __init__(self, subject: str, factor: int) -> None:
        self.subject_length = len(subject)
        self.limit = factor * len(subject)
        self.scanned = 0

    def wrap(self, original: Callable[..., Any]) -> Callable[..., Any]:
        def metered(subject: str, *args: Any, **kwargs: Any) -> Any:
            self.scanned += len(subject)
            return original(subject, *args, **kwargs)

        return metered

    def outcome(self) -> dict[str, bool]:
        return {
            "scan_within_budget": self.scanned <= self.limit,
            "scan_observed": self.scanned >= self.subject_length,
        }


class _ScriptedNode:
    """Answers checkout.read files queries from a scope's node files; logs every request."""

    def __init__(self, cwd: str, files: dict[str, str]) -> None:
        self.cwd = cwd
        self.files = files
        self.requests: list[dict[str, Any]] = []

    def _entry(self, path: str) -> dict[str, Any]:
        resolved = posixpath.normpath(posixpath.join(self.cwd, path))
        inside = resolved == self.cwd or resolved.startswith(self.cwd.rstrip("/") + "/")
        text = self.files.get(resolved) if inside else None
        kind = "outside" if not inside else ("file" if text is not None else "missing")
        data = None if text is None else base64.b64encode(text.encode("utf-8")).decode("ascii")
        known = data is not None
        return {
            "path": path,
            "resolved": resolved,
            "kind": kind,
            "checkout_root": self.cwd if known else None,
            "git_common_dir": posixpath.join(self.cwd, ".git") if known else None,
            "project_root": self.cwd if known else None,
            "project_id": NODE_PROJECT_ID if known else None,
            "data": data,
            "truncated": False,
        }

    async def send(
        self, machine_id: str, command: str, payload: dict[str, Any], timeout_ms: int = 30000
    ) -> dict[str, Any]:
        self.requests.append({"machine_id": machine_id, "command": command, "payload": payload})
        query = payload["query"]
        files = [self._entry(path) for path in query["paths"]] if query["kind"] == "files" else None
        return {
            "root": payload.get("cwd", self.cwd),
            "project_id": NODE_PROJECT_ID,
            "home": NODE_HOME,
            "temp_dir": NODE_TEMP_DIR,
            "files": files,
            "git": None,
        }


@contextlib.contextmanager
def _node_scope(
    spec: dict[str, Any], *, bind: bool
) -> Iterator[tuple[asyncio.AbstractEventLoop, list[dict[str, Any]]]]:
    """Run a scripted node on a daemon-thread loop; yield the loop and the request log."""
    if "node_delivery" not in spec:
        raise ValueError(f"a vector scope needs a node_delivery key, has {sorted(spec)}")
    delivery = spec["node_delivery"]
    checkout_reads = importlib.import_module("gobby.hooks.checkout_reads")
    node = _ScriptedNode(delivery["cwd"], dict(delivery.get("files", {})))
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(checkout_reads, "node_command_client", lambda: node)
            )
            stack.enter_context(
                mock.patch.object(
                    checkout_reads, "get_machine_id", lambda: delivery["hub_machine_id"]
                )
            )
            if bind:
                stack.enter_context(
                    checkout_reads.delivery_scope(delivery["machine_id"], loop, delivery["cwd"])
                )
            yield loop, node.requests
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _snapshot(values: list[Any], repo_root: str | None) -> list[Any]:
    return [rules_recorder._jsonable(value, repo_root=repo_root) for value in values]


def _invoke(
    entry: Callable[..., Any],
    args: list[Any],
    kwargs: dict[str, Any],
    within: list[Callable[[], Any]],
) -> tuple[Any, list[Any]]:
    """Call ``entry``; return ``(result, within values)`` with any context manager entered."""
    result = entry(*args, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    values: list[Any] = []
    if isinstance(result, contextlib.AbstractContextManager):
        with result as entered:
            values = [probe() for probe in within]
            result = entered
    return result, values


def _call(vector: dict[str, Any], repo: Path | None) -> Any:
    repo_root = None if repo is None else str(repo)
    # A copy, so a call that mutates its arguments never writes into the vector itself.
    vector = copy.deepcopy(vector)
    arguments = vector.get("input", {})
    scope = vector.get("scope")
    with contextlib.ExitStack() as stack:
        loop: asyncio.AbstractEventLoop | None = None
        requests: list[dict[str, Any]] | None = None
        if scope is not None:
            bind = vector["entry"] != DELIVERY_SCOPE_ENTRY
            loop, requests = stack.enter_context(_node_scope(scope, bind=bind))
        # Resolve everything the call needs before the try: a missing entry point or prepare
        # step, or a fixture that does not build, must fail the replay rather than be recorded.
        args = [_resolve(item, repo) for item in arguments.get("args", [])]
        kwargs = {key: _resolve(item, repo) for key, item in arguments.get("kwargs", {}).items()}
        if loop is not None:
            args = [loop if item == {SCOPE_LOOP_KEY: True} else item for item in args]
        for spec in vector.get("construct", []):
            args[spec["index"]] = _build(spec, repo)
        prepares = [resolve_entry(name) for name in vector.get("prepare", [])]
        within = [resolve_entry(name) for name in vector.get("within", [])]
        entry = resolve_entry(vector["entry"])
        _coerce(entry, args, kwargs)
        budget = vector.get("scan_budget")
        meter = _ScanMeter(str(args[0]), budget["factor"]) if budget else None
        if budget is not None and meter is not None:
            module_name, _, attribute = budget["target"].rpartition(".")
            module = importlib.import_module(module_name)
            stack.enter_context(
                mock.patch.object(module, attribute, meter.wrap(getattr(module, attribute)))
            )
        try:
            for prepare in prepares:
                prepare(args[0])
            before = _snapshot(args, repo_root)
            before_kwargs = dict(
                zip(kwargs, _snapshot(list(kwargs.values()), repo_root), strict=True)
            )
            result, within_values = _invoke(entry, args, kwargs, within)
        except Exception as exc:
            return {"raised": type(exc).__name__}
        node_requests = None if requests is None else list(requests)
    from_arg = vector.get("result_from_arg")
    if from_arg is not None:
        result = args[from_arg]
    after = _snapshot(args, repo_root)
    mutated = {
        str(index): value
        for index, value in enumerate(after)
        if index != from_arg and value != before[index]
    }
    for key, value in zip(kwargs, _snapshot(list(kwargs.values()), repo_root), strict=True):
        if value != before_kwargs[key]:
            mutated[key] = value
    envelope: dict[str, Any] = {}
    if mutated:
        envelope["mutated_args"] = mutated
    if meter is not None:
        envelope.update(meter.outcome())
    if node_requests is not None:
        envelope["node_requests"] = rules_recorder._jsonable(node_requests, repo_root=repo_root)
    if within:
        envelope["within"] = _snapshot(within_values, repo_root)
    value = rules_recorder._jsonable(result, repo_root=repo_root)
    return {"result": value, **envelope} if envelope else value


def _assert_outside_scratchpads(root: Path) -> None:
    """Refuse a scratch root the path-scope helpers would classify as an agent scratchpad."""
    path_scope = importlib.import_module("gobby.hooks._path_scope")
    markers = path_scope._SCRATCHPAD_MARKERS
    prefixes = path_scope._SCRATCHPAD_PREFIXES
    for part in root.parts:
        lowered = part.lower()
        if lowered in markers or lowered.startswith(prefixes):
            raise RuntimeError(f"fixture scratch root {root} holds a scratchpad marker {part!r}")


def replay_vector(vector: dict[str, Any]) -> Any:
    """Run a vector's entry point on its recorded input and return the scrubbed output."""
    if not vector.get("setup"):
        return _call(vector, None)
    machine_id = importlib.import_module("gobby.utils.machine_id")
    with tempfile.TemporaryDirectory(prefix="gobby-vector-", dir=FIXTURE_TEMP_ROOT) as scratch:
        # Resolve the scratch root so it matches the form the normalizer stores (symlinks
        # in the temp root resolved).
        base = Path(scratch).resolve()
        _assert_outside_scratchpads(base)
        root = base / "repo"
        root.mkdir()
        home = base / "home"
        home.mkdir()
        materialize(vector["setup"], root)
        pinned = {"HOME": str(home), "USERPROFILE": str(home), "GOBBY_HOME": str(home / ".gobby")}
        with mock.patch.dict(os.environ, pinned):
            machine_id.clear_cache()
            try:
                return _call(vector, root)
            finally:
                machine_id.clear_cache()


def record_vectors(vectors_path: Path) -> None:
    """Fill each vector's ``expect`` with the observed output when recording is requested."""
    if os.environ.get(RECORD_ENV) != "1":
        return
    document = json.loads(vectors_path.read_text())
    for vector in document["vectors"]:
        vector["expect"] = replay_vector(vector)
    vectors_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
