"""Lossless, bounded paging for task diffs and committed file content."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from gobby.tasks.diff_manifest import (
    Base64Content as Base64Content,
)
from gobby.tasks.diff_manifest import (
    DiffPagingError as DiffPagingError,
)
from gobby.tasks.diff_manifest import (
    EncodedContent as EncodedContent,
)
from gobby.tasks.diff_manifest import (
    ManifestItem as ManifestItem,
)
from gobby.tasks.diff_manifest import (
    ManifestParser as ManifestParser,
)
from gobby.tasks.diff_manifest import (
    Utf8Content as Utf8Content,
)
from gobby.tasks.diff_manifest import (
    encode_bytes as encode_bytes,
)
from gobby.tasks.diff_manifest import (
    parse_numstat,
)

MIN_LIMIT_BYTES = 4
MAX_LIMIT_BYTES = 30_000
MAX_COMMITS_LIMIT = 100
MAX_MANIFEST_LIMIT = 200
MAX_CURSOR_OFFSET = (1 << 63) - 1
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_GIT_TIMEOUT_SECONDS = 5.0

_GIT_READ_CHUNK_BYTES = 64 * 1024
_MAX_GIT_ERROR_BYTES = 8 * 1024
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


class TaskManagerProtocol(Protocol):
    def get_task(self, task_id: str) -> object | None: ...


class _Digest(Protocol):
    def update(self, data: bytes) -> object: ...

    def hexdigest(self) -> str: ...


class CommitCursorPage(TypedDict):
    items: list[str]
    cursor_offset: int
    cursor_limit: int
    cursor_end: int
    total: int
    complete: bool


class ManifestCursorPage(TypedDict):
    items: list[ManifestItem]
    cursor_offset: int
    cursor_limit: int
    cursor_end: int
    total: int
    complete: bool


class DiffPage(TypedDict):
    content: EncodedContent
    byte_start: int
    byte_end: int
    total_bytes: int
    complete: bool
    commits: CommitCursorPage
    manifest: ManifestCursorPage
    snapshot_hash: str
    view_hash: str


class _WindowCollector:
    def __init__(self, offset: int, limit: int) -> None:
        self.offset = offset
        self.limit = limit
        self.total = 0
        self.data = bytearray()

    def feed(self, chunk: bytes) -> None:
        chunk_start = self.total
        chunk_end = chunk_start + len(chunk)
        start = max(chunk_start, self.offset)
        end = min(chunk_end, self.offset + self.limit)
        if start < end:
            self.data.extend(chunk[start - chunk_start : end - chunk_start])
        self.total = chunk_end


def _task_commits(task: object) -> list[str]:
    commits = list(getattr(task, "commits", None) or [])
    if not commits:
        closed_commit = getattr(task, "closed_commit_sha", None)
        if closed_commit:
            commits = [closed_commit]
    seen: set[str] = set()
    ordered: list[str] = []
    for commit in commits:
        value = str(commit)
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _validate_int(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiffPagingError("invalid_paging_argument", f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise DiffPagingError(
            "invalid_paging_argument",
            f"{name} must be between {minimum} and {maximum}",
            parameter=name,
            minimum=minimum,
            maximum=maximum,
            actual=value,
        )


def _validate_paging(
    *,
    offset_bytes: int,
    limit_bytes: int,
    commits_offset: int,
    commits_limit: int,
    manifest_offset: int,
    manifest_limit: int,
    max_payload_bytes: int,
) -> None:
    _validate_int("offset_bytes", offset_bytes, minimum=0, maximum=MAX_CURSOR_OFFSET)
    _validate_int("limit_bytes", limit_bytes, minimum=MIN_LIMIT_BYTES, maximum=MAX_LIMIT_BYTES)
    _validate_int("commits_offset", commits_offset, minimum=0, maximum=MAX_CURSOR_OFFSET)
    _validate_int("commits_limit", commits_limit, minimum=0, maximum=MAX_COMMITS_LIMIT)
    _validate_int("manifest_offset", manifest_offset, minimum=0, maximum=MAX_CURSOR_OFFSET)
    _validate_int("manifest_limit", manifest_limit, minimum=0, maximum=MAX_MANIFEST_LIMIT)
    _validate_int("max_payload_bytes", max_payload_bytes, minimum=1, maximum=MAX_CURSOR_OFFSET)


def _remaining_timeout(*, subprocess_deadline: float | None, git_timeout_seconds: float) -> float:
    if git_timeout_seconds <= 0:
        raise DiffPagingError(
            "invalid_paging_argument",
            "git_timeout_seconds must be positive",
            parameter="git_timeout_seconds",
        )
    deadline = time.monotonic() + git_timeout_seconds
    if subprocess_deadline is not None:
        deadline = min(deadline, subprocess_deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DiffPagingError("git_timeout", "git subprocess deadline expired")
    return remaining


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    process.wait()


def _run_git(
    args: Sequence[str | bytes],
    *,
    cwd: Path,
    consume: Callable[[bytes], object] | None,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
    check: bool = True,
) -> tuple[int, bytes]:
    argv = [b"git", *(os.fsencode(arg) if isinstance(arg, str) else arg for arg in args)]
    timeout = _remaining_timeout(
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except OSError as exc:
            raise DiffPagingError("git_failed", f"failed to start git: {exc}") from exc
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            pid = process.pid
            _kill_and_reap(process)
            raise DiffPagingError(
                "git_timeout",
                "git subprocess exceeded its deadline",
                timeout_seconds=timeout,
                pid=pid,
                reaped=process.poll() is not None,
            ) from exc
        stderr_file.seek(0)
        stderr = stderr_file.read(_MAX_GIT_ERROR_BYTES)
        if check and process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise DiffPagingError(
                "git_failed",
                message or f"git exited with status {process.returncode}",
                returncode=process.returncode,
            )
        if process.returncode == 0 and consume is not None:
            stdout_file.seek(0)
            while chunk := stdout_file.read(_GIT_READ_CHUNK_BYTES):
                consume(chunk)
        return process.returncode, stderr


def _read_git(
    args: Sequence[str | bytes],
    *,
    cwd: Path,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> bytes:
    chunks: list[bytes] = []
    _run_git(
        args,
        cwd=cwd,
        consume=chunks.append,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
    return b"".join(chunks)


def _canonicalize_commits(
    commits: Sequence[str],
    *,
    cwd: Path,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> list[str]:
    canonical: list[str] = []
    for commit in commits:
        if not _COMMIT_RE.fullmatch(commit):
            raise DiffPagingError("invalid_commit", f"invalid commit SHA: {commit!r}")
        try:
            stdout = _read_git(
                ["rev-parse", "--verify", f"{commit}^{{commit}}"],
                cwd=cwd,
                subprocess_deadline=subprocess_deadline,
                git_timeout_seconds=git_timeout_seconds,
            )
        except DiffPagingError as exc:
            if exc.code == "git_failed":
                raise DiffPagingError(
                    "invalid_commit", f"commit does not resolve: {commit}"
                ) from exc
            raise
        resolved = stdout.strip().decode("ascii", errors="strict")
        if not re.fullmatch(r"[0-9a-f]{40,64}", resolved):
            raise DiffPagingError("invalid_commit", f"git returned an invalid SHA for {commit}")
        canonical.append(resolved)
    return canonical


def decode_content(content: EncodedContent) -> bytes:
    if content["encoding"] == "utf-8":
        return content["text"].encode("utf-8")
    return base64.b64decode(content["data"], validate=True)


def _encode_window(value: bytes) -> tuple[EncodedContent, int]:
    if not value:
        return {"encoding": "utf-8", "text": ""}, 0
    try:
        return {"encoding": "utf-8", "text": value.decode("utf-8")}, len(value)
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(value) and exc.start > 0:
            prefix = value[: exc.start]
            try:
                return {"encoding": "utf-8", "text": prefix.decode("utf-8")}, len(prefix)
            except UnicodeDecodeError:
                pass
        return encode_bytes(value), len(value)


def _numstat_totals(
    commit: str,
    *,
    cwd: Path,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> dict[bytes, tuple[int | None, int | None]]:
    data = _read_git(
        [
            "--literal-pathspecs",
            "show",
            "--diff-merges=first-parent",
            "--format=",
            "--numstat",
            "-z",
            "-M",
            commit,
            "--",
        ],
        cwd=cwd,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
    return parse_numstat(data)


def _manifest_page_candidates(
    commits: Sequence[str],
    *,
    offset: int,
    limit: int,
    wanted_selector: str | None,
    cwd: Path,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> tuple[list[ManifestItem], int, tuple[str, bytes, str] | None]:
    items: list[ManifestItem] = []
    total = 0
    matched: tuple[str, bytes, str] | None = None
    current_numstat: dict[bytes, tuple[int | None, int | None]] | None = None
    numstat_loaded = False

    def emit(item: ManifestItem, raw_path: bytes) -> None:
        nonlocal current_numstat, matched, numstat_loaded, total
        if offset <= total < offset + limit:
            if not numstat_loaded:
                current_numstat = _numstat_totals(
                    item["commit"],
                    cwd=cwd,
                    subprocess_deadline=subprocess_deadline,
                    git_timeout_seconds=git_timeout_seconds,
                )
                numstat_loaded = True
            assert current_numstat is not None
            magnitude = current_numstat.get(raw_path)
            if magnitude is not None:
                item["lines_added"], item["lines_deleted"] = magnitude
            items.append(item)
        if wanted_selector is not None and item["path_selector"] == wanted_selector:
            matched = (item["commit"], raw_path, item["status"])
        total += 1

    for commit in commits:
        current_numstat = None
        numstat_loaded = False
        parser = ManifestParser(commit, emit)
        _run_git(
            [
                "--literal-pathspecs",
                "show",
                "--diff-merges=first-parent",
                "--format=",
                "--name-status",
                "-z",
                "-M",
                commit,
                "--",
            ],
            cwd=cwd,
            consume=parser.feed,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )
        parser.finish()
    return items, total, matched


def _snapshot_hasher(commits: Sequence[str], include_uncommitted: bool) -> _Digest:
    digest = hashlib.sha256(b"gobby-diff-snapshot-v1\0")
    for commit in commits:
        value = commit.encode("ascii")
        digest.update(len(value).to_bytes(2, "big"))
        digest.update(value)
    digest.update(b"\1" if include_uncommitted else b"\0")
    return digest


def _view_hash(
    snapshot_hash: str,
    *,
    view_kind: str,
    commit: str | None,
    path_selector: str | None,
    include_uncommitted: bool,
) -> str:
    payload = {
        "snapshot_hash": snapshot_hash,
        "view_kind": view_kind,
        "commit": commit,
        "path_selector": path_selector,
        "include_uncommitted": include_uncommitted,
        "rendering": "git-no-ext-diff-no-textconv-v1",
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _validate_tokens(
    *,
    any_cursor_nonzero: bool,
    supplied_snapshot_hash: str | None,
    supplied_view_hash: str | None,
    actual_snapshot_hash: str,
    actual_view_hash: str,
) -> None:
    if any_cursor_nonzero and (not supplied_snapshot_hash or not supplied_view_hash):
        raise DiffPagingError(
            "snapshot_required",
            "snapshot_hash and view_hash are required when any cursor is nonzero",
        )
    if supplied_snapshot_hash is not None and supplied_snapshot_hash != actual_snapshot_hash:
        raise DiffPagingError("snapshot_changed", "the task diff snapshot changed between pages")
    if supplied_view_hash is not None and supplied_view_hash != actual_view_hash:
        raise DiffPagingError("view_changed", "the paging cursor belongs to a different view")


def _stream_diff_view(
    *,
    commits: Sequence[str],
    selected_commit: str | None,
    raw_path: bytes | None,
    include_uncommitted: bool,
    window: _WindowCollector,
    snapshot_digest: _Digest,
    cwd: Path,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> None:
    has_output = False

    def stream_source(args: Sequence[str | bytes]) -> None:
        nonlocal has_output
        source_started = False

        def consume(chunk: bytes) -> None:
            nonlocal has_output, source_started
            if not source_started:
                if has_output:
                    window.feed(b"\n")
                source_started = True
                has_output = True
            window.feed(chunk)

        _run_git(
            args,
            cwd=cwd,
            consume=consume,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )

    selected = [selected_commit] if selected_commit is not None else list(commits)
    for selected_sha in selected:
        args: list[str | bytes] = [
            "--literal-pathspecs",
            "show",
            "--diff-merges=first-parent",
            "--format=",
            "--no-ext-diff",
            "--no-textconv",
            "-M",
            selected_sha,
            "--",
        ]
        if raw_path is not None:
            args.append(raw_path)
        stream_source(args)

    if include_uncommitted:
        label = (
            b"\n--- uncommitted changes ---\n" if has_output else b"--- uncommitted changes ---\n"
        )
        uncommitted_started = False

        def consume_uncommitted(chunk: bytes) -> None:
            nonlocal has_output, uncommitted_started
            snapshot_digest.update(chunk)
            if selected_commit is not None:
                return
            if not uncommitted_started:
                window.feed(label)
                uncommitted_started = True
                has_output = True
            window.feed(chunk)

        _run_git(
            [
                "--literal-pathspecs",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
            ],
            cwd=cwd,
            consume=consume_uncommitted,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )


def _stream_file_view(
    *,
    commit: str,
    raw_path: bytes,
    include_uncommitted: bool,
    snapshot_digest: _Digest,
    window: _WindowCollector,
    cwd: Path,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> None:
    object_name = commit.encode("ascii") + b":" + raw_path
    returncode, _ = _run_git(
        ["cat-file", "-e", object_name],
        cwd=cwd,
        consume=None,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
        check=False,
    )
    if returncode != 0:
        raise DiffPagingError(
            "path_absent_at_commit",
            "the selected path does not exist at the requested commit",
            commit=commit,
        )
    _run_git(
        ["cat-file", "blob", object_name],
        cwd=cwd,
        consume=window.feed,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
    if include_uncommitted:
        _run_git(
            [
                "--literal-pathspecs",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
            ],
            cwd=cwd,
            consume=snapshot_digest.update,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )


def serialized_page_size(page: DiffPage) -> int:
    return len(json.dumps(page, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _make_page(
    *,
    raw_content: bytes,
    raw_prefix_length: int,
    byte_start: int,
    total_bytes: int,
    commits: Sequence[str],
    commits_offset: int,
    commits_limit: int,
    commits_total: int,
    manifest: Sequence[ManifestItem],
    manifest_offset: int,
    manifest_limit: int,
    manifest_total: int,
    snapshot_hash: str,
    view_hash: str,
) -> DiffPage:
    content, served_bytes = _encode_window(raw_content[:raw_prefix_length])
    commits_items = list(commits)
    manifest_items = list(manifest)
    byte_end = byte_start + served_bytes
    commits_end = commits_offset + len(commits_items)
    manifest_end = manifest_offset + len(manifest_items)
    return {
        "content": content,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "total_bytes": total_bytes,
        "complete": byte_end >= total_bytes,
        "commits": {
            "items": commits_items,
            "cursor_offset": commits_offset,
            "cursor_limit": commits_limit,
            "cursor_end": commits_end,
            "total": commits_total,
            "complete": commits_end >= commits_total,
        },
        "manifest": {
            "items": manifest_items,
            "cursor_offset": manifest_offset,
            "cursor_limit": manifest_limit,
            "cursor_end": manifest_end,
            "total": manifest_total,
            "complete": manifest_end >= manifest_total,
        },
        "snapshot_hash": snapshot_hash,
        "view_hash": view_hash,
    }


def _fit_page(
    *,
    raw_content: bytes,
    byte_start: int,
    total_bytes: int,
    commit_candidates: Sequence[str],
    commits_offset: int,
    commits_limit: int,
    commits_total: int,
    manifest_candidates: Sequence[ManifestItem],
    manifest_offset: int,
    manifest_limit: int,
    manifest_total: int,
    snapshot_hash: str,
    view_hash: str,
    max_payload_bytes: int,
) -> DiffPage:
    def build(raw_length: int, commit_count: int, manifest_count: int) -> DiffPage:
        return _make_page(
            raw_content=raw_content,
            raw_prefix_length=raw_length,
            byte_start=byte_start,
            total_bytes=total_bytes,
            commits=commit_candidates[:commit_count],
            commits_offset=commits_offset,
            commits_limit=commits_limit,
            commits_total=commits_total,
            manifest=manifest_candidates[:manifest_count],
            manifest_offset=manifest_offset,
            manifest_limit=manifest_limit,
            manifest_total=manifest_total,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
        )

    minimum_raw = 0
    if raw_content:
        for candidate in range(1, min(4, len(raw_content)) + 1):
            _, served = _encode_window(raw_content[:candidate])
            if served:
                minimum_raw = candidate
                break
        if minimum_raw == 0:
            minimum_raw = min(4, len(raw_content))
    minimum_commits = 1 if commit_candidates else 0
    minimum_manifest = 1 if manifest_candidates else 0
    page = build(minimum_raw, minimum_commits, minimum_manifest)
    minimum_size = serialized_page_size(page)
    if minimum_size > max_payload_bytes:
        raise DiffPagingError(
            "result_budget_too_small",
            "serialized page budget cannot fit the minimum advancing page",
            minimum_bytes=minimum_size,
            max_payload_bytes=max_payload_bytes,
        )

    low = minimum_raw
    high = len(raw_content)
    while low < high:
        middle = (low + high + 1) // 2
        if (
            serialized_page_size(build(middle, minimum_commits, minimum_manifest))
            <= max_payload_bytes
        ):
            low = middle
        else:
            high = middle - 1
    raw_length = low
    commit_count = minimum_commits
    manifest_count = minimum_manifest
    while commit_count < len(commit_candidates):
        if (
            serialized_page_size(build(raw_length, commit_count + 1, manifest_count))
            > max_payload_bytes
        ):
            break
        commit_count += 1
    while manifest_count < len(manifest_candidates):
        if (
            serialized_page_size(build(raw_length, commit_count, manifest_count + 1))
            > max_payload_bytes
        ):
            break
        manifest_count += 1
    return build(raw_length, commit_count, manifest_count)


def _get_page(
    task_id: str,
    task_manager: TaskManagerProtocol,
    *,
    view_kind: Literal["task_diff", "commit_diff", "path_diff", "file"],
    include_uncommitted: bool,
    cwd: str | Path | None,
    commit: str | None,
    path_selector: str | None,
    offset_bytes: int,
    limit_bytes: int,
    commits_offset: int,
    commits_limit: int,
    manifest_offset: int,
    manifest_limit: int,
    snapshot_hash: str | None,
    view_hash: str | None,
    max_payload_bytes: int,
    subprocess_deadline: float | None,
    git_timeout_seconds: float,
) -> DiffPage:
    _validate_paging(
        offset_bytes=offset_bytes,
        limit_bytes=limit_bytes,
        commits_offset=commits_offset,
        commits_limit=commits_limit,
        manifest_offset=manifest_offset,
        manifest_limit=manifest_limit,
        max_payload_bytes=max_payload_bytes,
    )
    task = task_manager.get_task(task_id)
    if task is None:
        raise DiffPagingError("task_not_found", f"task {task_id} not found")
    repo = Path(cwd) if cwd is not None else Path.cwd()
    canonical_commits = _canonicalize_commits(
        _task_commits(task),
        cwd=repo,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
    selected_commit: str | None = None
    if commit is not None:
        requested = _canonicalize_commits(
            [commit],
            cwd=repo,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )[0]
        if requested not in canonical_commits:
            raise DiffPagingError("commit_not_linked", "commit is not linked to the task")
        selected_commit = requested

    manifest_candidates, manifest_total, path_match = _manifest_page_candidates(
        canonical_commits,
        offset=manifest_offset,
        limit=manifest_limit,
        wanted_selector=path_selector,
        cwd=repo,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
    raw_path: bytes | None = None
    if path_selector is not None:
        if path_match is None:
            raise DiffPagingError(
                "path_not_in_manifest",
                "path_selector is not present in the linked-commit manifest",
            )
        manifest_commit, raw_path, _ = path_match
        if selected_commit is not None and manifest_commit != selected_commit:
            raise DiffPagingError(
                "path_not_in_manifest",
                "path_selector does not belong to the requested commit",
            )
        selected_commit = manifest_commit
    if view_kind in {"path_diff", "file"} and (selected_commit is None or raw_path is None):
        raise DiffPagingError(
            "invalid_paging_argument",
            "commit and path_selector are required for path and file views",
        )

    snapshot_digest = _snapshot_hasher(canonical_commits, include_uncommitted)
    window = _WindowCollector(offset_bytes, limit_bytes)
    if view_kind == "file":
        assert selected_commit is not None and raw_path is not None
        _stream_file_view(
            commit=selected_commit,
            raw_path=raw_path,
            include_uncommitted=include_uncommitted,
            snapshot_digest=snapshot_digest,
            window=window,
            cwd=repo,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )
    else:
        _stream_diff_view(
            commits=canonical_commits,
            selected_commit=selected_commit,
            raw_path=raw_path,
            include_uncommitted=include_uncommitted,
            window=window,
            snapshot_digest=snapshot_digest,
            cwd=repo,
            subprocess_deadline=subprocess_deadline,
            git_timeout_seconds=git_timeout_seconds,
        )
    actual_snapshot_hash = snapshot_digest.hexdigest()
    actual_view_hash = _view_hash(
        actual_snapshot_hash,
        view_kind=view_kind,
        commit=selected_commit,
        path_selector=path_selector,
        include_uncommitted=include_uncommitted,
    )
    _validate_tokens(
        any_cursor_nonzero=bool(offset_bytes or commits_offset or manifest_offset),
        supplied_snapshot_hash=snapshot_hash,
        supplied_view_hash=view_hash,
        actual_snapshot_hash=actual_snapshot_hash,
        actual_view_hash=actual_view_hash,
    )
    commit_candidates = canonical_commits[commits_offset : commits_offset + commits_limit]
    return _fit_page(
        raw_content=bytes(window.data),
        byte_start=offset_bytes,
        total_bytes=window.total,
        commit_candidates=commit_candidates,
        commits_offset=commits_offset,
        commits_limit=commits_limit,
        commits_total=len(canonical_commits),
        manifest_candidates=manifest_candidates,
        manifest_offset=manifest_offset,
        manifest_limit=manifest_limit,
        manifest_total=manifest_total,
        snapshot_hash=actual_snapshot_hash,
        view_hash=actual_view_hash,
        max_payload_bytes=max_payload_bytes,
    )


def get_task_diff_page(
    task_id: str,
    task_manager: TaskManagerProtocol,
    *,
    include_uncommitted: bool = False,
    cwd: str | Path | None = None,
    commit: str | None = None,
    path_selector: str | None = None,
    offset_bytes: int = 0,
    limit_bytes: int = MAX_LIMIT_BYTES,
    commits_offset: int = 0,
    commits_limit: int = MAX_COMMITS_LIMIT,
    manifest_offset: int = 0,
    manifest_limit: int = MAX_MANIFEST_LIMIT,
    snapshot_hash: str | None = None,
    view_hash: str | None = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    subprocess_deadline: float | None = None,
    git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> DiffPage:
    """Return one lossless page of a task, commit, or selected-path diff."""
    if path_selector is not None:
        kind: Literal["task_diff", "commit_diff", "path_diff"] = "path_diff"
    elif commit is not None:
        kind = "commit_diff"
    else:
        kind = "task_diff"
    return _get_page(
        task_id,
        task_manager,
        view_kind=kind,
        include_uncommitted=include_uncommitted,
        cwd=cwd,
        commit=commit,
        path_selector=path_selector,
        offset_bytes=offset_bytes,
        limit_bytes=limit_bytes,
        commits_offset=commits_offset,
        commits_limit=commits_limit,
        manifest_offset=manifest_offset,
        manifest_limit=manifest_limit,
        snapshot_hash=snapshot_hash,
        view_hash=view_hash,
        max_payload_bytes=max_payload_bytes,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )


def read_file_at_commit(
    task_id: str,
    task_manager: TaskManagerProtocol,
    *,
    commit: str,
    path_selector: str,
    include_uncommitted: bool = False,
    cwd: str | Path | None = None,
    offset_bytes: int = 0,
    limit_bytes: int = MAX_LIMIT_BYTES,
    commits_offset: int = 0,
    commits_limit: int = MAX_COMMITS_LIMIT,
    manifest_offset: int = 0,
    manifest_limit: int = MAX_MANIFEST_LIMIT,
    snapshot_hash: str | None = None,
    view_hash: str | None = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    subprocess_deadline: float | None = None,
    git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> DiffPage:
    """Return one byte page of a linked commit's selected file."""
    return _get_page(
        task_id,
        task_manager,
        view_kind="file",
        include_uncommitted=include_uncommitted,
        cwd=cwd,
        commit=commit,
        path_selector=path_selector,
        offset_bytes=offset_bytes,
        limit_bytes=limit_bytes,
        commits_offset=commits_offset,
        commits_limit=commits_limit,
        manifest_offset=manifest_offset,
        manifest_limit=manifest_limit,
        snapshot_hash=snapshot_hash,
        view_hash=view_hash,
        max_payload_bytes=max_payload_bytes,
        subprocess_deadline=subprocess_deadline,
        git_timeout_seconds=git_timeout_seconds,
    )
