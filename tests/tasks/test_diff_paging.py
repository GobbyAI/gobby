from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import gobby.tasks.diff_paging as diff_paging
from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.tasks.diff_manifest import ManifestItem, ManifestParser, encode_bytes
from gobby.tasks.diff_paging import (
    MAX_COMMITS_LIMIT,
    MAX_LIMIT_BYTES,
    MAX_MANIFEST_LIMIT,
    MIN_LIMIT_BYTES,
    DiffPage,
    DiffPagingError,
    decode_content,
    get_task_diff_page,
    read_file_at_commit,
    serialized_page_size,
)


@dataclass
class _Task:
    id: str = "task-id"
    project_id: str = "project-id"
    commits: list[str] = field(default_factory=list)
    closed_commit_sha: str | None = None


class _TaskManager:
    def __init__(self, task: _Task) -> None:
        self.task = task

    def get_task(self, task_id: str) -> _Task | None:
        return self.task if task_id == self.task.id else None


class _ProjectManager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def get(self, project_id: str) -> SimpleNamespace | None:
        if project_id != "project-id":
            return None
        return SimpleNamespace(repo_path=str(self.repo))


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip().decode("ascii")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "Gobby Tests")
    return path


def _manager(*commits: str) -> _TaskManager:
    return _TaskManager(_Task(commits=list(commits)))


def _page_all_diff(
    manager: _TaskManager,
    repo: Path,
    *,
    include_uncommitted: bool = False,
    limit_bytes: int = 7,
) -> tuple[bytes, list[DiffPage]]:
    offset = 0
    snapshot_hash: str | None = None
    view_hash: str | None = None
    chunks: list[bytes] = []
    pages: list[DiffPage] = []
    while True:
        page = get_task_diff_page(
            manager.task.id,
            manager,
            cwd=repo,
            include_uncommitted=include_uncommitted,
            offset_bytes=offset,
            limit_bytes=limit_bytes,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
        )
        pages.append(page)
        chunks.append(decode_content(page["content"]))
        assert page["byte_start"] == offset
        assert page["byte_end"] == offset + len(chunks[-1])
        if page["complete"]:
            break
        assert page["byte_end"] > offset
        offset = page["byte_end"]
        snapshot_hash = page["snapshot_hash"]
        view_hash = page["view_hash"]
    return b"".join(chunks), pages


def _find_manifest_item(page: DiffPage, raw_path: bytes) -> ManifestItem:
    for item in page["manifest"]["items"]:
        if decode_content(item["path"]) == raw_path:
            return item
    raise AssertionError(f"manifest path not found: {raw_path!r}")


def test_manifest_parser_and_encoder_are_public() -> None:
    emitted: list[tuple[ManifestItem, bytes]] = []
    parser = ManifestParser("a" * 40, lambda item, path: emitted.append((item, path)))

    parser.feed(b"M\x00src/example.py\x00")
    parser.finish()

    assert len(emitted) == 1
    assert emitted[0][1] == b"src/example.py"
    assert emitted[0][0]["path"] == {"encoding": "utf-8", "text": "src/example.py"}
    assert encode_bytes(b"\xff") == {"encoding": "base64", "data": "/w=="}


def test_lossless_sequential_pages_preserve_multibyte_boundaries(repo: Path) -> None:
    (repo / "unicode.txt").write_text("start αβγ 😀 end\n", encoding="utf-8")
    commit = _commit(repo, "unicode")
    manager = _manager(commit)

    reconstructed, pages = _page_all_diff(manager, repo, limit_bytes=7)
    expected = _git(
        repo,
        "--literal-pathspecs",
        "show",
        "--format=",
        "--no-ext-diff",
        "--no-textconv",
        "-M",
        commit,
        "--",
    )

    assert reconstructed == expected
    assert pages[-1]["byte_end"] == pages[-1]["total_bytes"]
    assert {page["snapshot_hash"] for page in pages} == {pages[0]["snapshot_hash"]}
    manifest = get_task_diff_page("task-id", manager, cwd=repo, limit_bytes=4)
    selector = _find_manifest_item(manifest, b"unicode.txt")["path_selector"]
    file_page = read_file_at_commit(
        "task-id",
        manager,
        cwd=repo,
        commit=commit,
        path_selector=selector,
        limit_bytes=7,
    )
    assert decode_content(file_page["content"]) == b"start "
    assert file_page["byte_end"] == 6


def test_invalid_utf8_manifest_and_blob_round_trip_as_base64(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_path = b"invalid-\xff.bin"
    raw_content = b"\xff\xfe\x00payload\x80"
    (repo / "blob.bin").write_bytes(raw_content)
    commit = _commit(repo, "invalid bytes")
    manager = _manager(commit)
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "raw-manifest-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        "  *\" --name-status \"*) printf 'M\\000invalid-\\377.bin\\000M\\000blob.bin\\000'; exit 0;;\n"
        "esac\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    first = get_task_diff_page(manager.task.id, manager, cwd=repo, limit_bytes=8)
    item = _find_manifest_item(first, raw_path)
    assert item["path"]["encoding"] == "base64"
    blob_item = _find_manifest_item(first, b"blob.bin")

    offset = 0
    snapshot_hash: str | None = None
    view_hash: str | None = None
    chunks: list[bytes] = []
    while True:
        page = read_file_at_commit(
            manager.task.id,
            manager,
            cwd=repo,
            commit=commit,
            path_selector=blob_item["path_selector"],
            offset_bytes=offset,
            limit_bytes=4,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
        )
        chunks.append(decode_content(page["content"]))
        if chunks[-1] and any(byte >= 0x80 for byte in chunks[-1]):
            assert page["content"]["encoding"] == "base64"
        if page["complete"]:
            break
        offset = page["byte_end"]
        snapshot_hash = page["snapshot_hash"]
        view_hash = page["view_hash"]

    assert b"".join(chunks) == raw_content


def test_nonzero_cursor_requires_both_identity_tokens(repo: Path) -> None:
    (repo / "file.txt").write_text("enough content to page\n")
    commit = _commit(repo, "page")
    manager = _manager(commit)

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page(
            manager.task.id,
            manager,
            cwd=repo,
            offset_bytes=4,
            limit_bytes=4,
        )

    assert error.value.code == "snapshot_required"


def test_worktree_mutation_changes_snapshot(repo: Path) -> None:
    (repo / "file.txt").write_text("committed\n")
    commit = _commit(repo, "base")
    manager = _manager(commit)
    (repo / "file.txt").write_text("first uncommitted state\n")
    first = get_task_diff_page(
        manager.task.id,
        manager,
        cwd=repo,
        include_uncommitted=True,
        limit_bytes=4,
    )
    (repo / "file.txt").write_text("second uncommitted state\n")

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page(
            manager.task.id,
            manager,
            cwd=repo,
            include_uncommitted=True,
            offset_bytes=first["byte_end"],
            limit_bytes=4,
            snapshot_hash=first["snapshot_hash"],
            view_hash=first["view_hash"],
        )

    assert error.value.code == "snapshot_changed"


def test_view_token_rejects_commit_switch(repo: Path) -> None:
    (repo / "file.txt").write_text("first\n")
    first_commit = _commit(repo, "first")
    (repo / "file.txt").write_text("second version with more content\n")
    second_commit = _commit(repo, "second")
    manager = _manager(first_commit, second_commit)
    first = get_task_diff_page(
        manager.task.id,
        manager,
        cwd=repo,
        commit=first_commit,
        limit_bytes=4,
    )

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page(
            manager.task.id,
            manager,
            cwd=repo,
            commit=second_commit,
            offset_bytes=first["byte_end"],
            limit_bytes=4,
            snapshot_hash=first["snapshot_hash"],
            view_hash=first["view_hash"],
        )

    assert error.value.code == "view_changed"


def test_reordering_same_shas_changes_snapshot_hash(repo: Path) -> None:
    (repo / "file.txt").write_text("first\n")
    first_commit = _commit(repo, "first")
    (repo / "file.txt").write_text("second\n")
    second_commit = _commit(repo, "second")

    first = get_task_diff_page(
        "task-id", _manager(first_commit, second_commit), cwd=repo, limit_bytes=4
    )
    reordered = get_task_diff_page(
        "task-id", _manager(second_commit, first_commit), cwd=repo, limit_bytes=4
    )

    assert first["snapshot_hash"] != reordered["snapshot_hash"]


def test_uncommitted_snapshot_and_page_use_one_git_diff_invocation(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "file.txt").write_text("committed\n")
    commit = _commit(repo, "base")
    (repo / "file.txt").write_text("uncommitted\n")
    real_git = shutil.which("git")
    assert real_git is not None
    log_path = tmp_path / "git.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$GIT_INVOCATION_LOG"\n'
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("GIT_INVOCATION_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    page = get_task_diff_page(
        "task-id",
        _manager(commit),
        cwd=repo,
        include_uncommitted=True,
        limit_bytes=8,
    )

    invocations = log_path.read_text().splitlines()
    uncommitted = [
        line for line in invocations if " diff " in f" {line} " and " HEAD " in f" {line} "
    ]
    assert len(uncommitted) == 1
    assert page["snapshot_hash"]


def test_serialized_budget_shortens_lists_and_cursor_end_chains(repo: Path) -> None:
    commits: list[str] = []
    for index in range(4):
        name = f"control-{index}-" + "\\t\\n" * 20 + ".txt"
        (repo / name).write_text(f"value {index}\n")
        commits.append(_commit(repo, f"commit {index}"))
    manager = _manager(*commits)
    full = get_task_diff_page(
        "task-id",
        manager,
        cwd=repo,
        limit_bytes=4,
        commits_limit=4,
        manifest_limit=4,
    )
    minimum = copy.deepcopy(full)
    minimum["commits"]["items"] = minimum["commits"]["items"][:1]
    minimum["commits"]["cursor_end"] = 1
    minimum["commits"]["complete"] = False
    minimum["manifest"]["items"] = minimum["manifest"]["items"][:1]
    minimum["manifest"]["cursor_end"] = 1
    minimum["manifest"]["complete"] = False
    budget = len(json.dumps(minimum, ensure_ascii=True, separators=(",", ":")).encode()) + 4

    first = get_task_diff_page(
        "task-id",
        manager,
        cwd=repo,
        limit_bytes=4,
        commits_limit=4,
        manifest_limit=4,
        max_payload_bytes=budget,
    )
    assert serialized_page_size(first) <= budget
    assert first["commits"]["cursor_end"] == len(first["commits"]["items"])
    assert first["manifest"]["cursor_end"] == len(first["manifest"]["items"])
    assert first["commits"]["cursor_end"] < first["commits"]["total"]
    assert first["manifest"]["cursor_end"] < first["manifest"]["total"]

    next_page = get_task_diff_page(
        "task-id",
        manager,
        cwd=repo,
        limit_bytes=4,
        commits_offset=first["commits"]["cursor_end"],
        commits_limit=4,
        manifest_offset=first["manifest"]["cursor_end"],
        manifest_limit=4,
        snapshot_hash=first["snapshot_hash"],
        view_hash=first["view_hash"],
        max_payload_bytes=budget,
    )
    assert next_page["commits"]["cursor_offset"] == first["commits"]["cursor_end"]
    assert next_page["manifest"]["cursor_offset"] == first["manifest"]["cursor_end"]


def test_one_mib_single_line_file_pages_without_skipping(repo: Path) -> None:
    content = b"x" * (1024 * 1024)
    (repo / "large.txt").write_bytes(content)
    commit = _commit(repo, "large")
    manager = _manager(commit)
    manifest = get_task_diff_page("task-id", manager, cwd=repo, limit_bytes=4)
    selector = _find_manifest_item(manifest, b"large.txt")["path_selector"]
    offset = 0
    snapshot_hash: str | None = None
    view_hash: str | None = None
    chunks: list[bytes] = []
    while True:
        page = read_file_at_commit(
            "task-id",
            manager,
            cwd=repo,
            commit=commit,
            path_selector=selector,
            offset_bytes=offset,
            limit_bytes=MAX_LIMIT_BYTES,
            commits_limit=0,
            manifest_limit=0,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
        )
        chunk = decode_content(page["content"])
        chunks.append(chunk)
        assert page["byte_end"] == offset + len(chunk)
        if page["complete"]:
            break
        offset = page["byte_end"]
        snapshot_hash = page["snapshot_hash"]
        view_hash = page["view_hash"]

    assert b"".join(chunks) == content


def test_deleted_manifest_path_returns_typed_path_absent(repo: Path) -> None:
    (repo / "deleted.txt").write_text("present\n")
    _commit(repo, "add")
    (repo / "deleted.txt").unlink()
    deletion_commit = _commit(repo, "delete")
    manager = _manager(deletion_commit)
    manifest = get_task_diff_page("task-id", manager, cwd=repo, limit_bytes=4)
    selector = _find_manifest_item(manifest, b"deleted.txt")["path_selector"]

    with pytest.raises(DiffPagingError) as error:
        read_file_at_commit(
            "task-id",
            manager,
            cwd=repo,
            commit=deletion_commit,
            path_selector=selector,
        )

    assert error.value.code == "path_absent_at_commit"


def test_rename_manifest_addresses_both_paths(repo: Path) -> None:
    (repo / "old-name.txt").write_text("line one\nline two\nline three\n")
    _commit(repo, "add")
    _git(repo, "mv", "old-name.txt", "new-name.txt")
    rename_commit = _commit(repo, "rename")
    manager = _manager(rename_commit)

    manifest = get_task_diff_page("task-id", manager, cwd=repo, limit_bytes=4)
    old_item = _find_manifest_item(manifest, b"old-name.txt")
    new_item = _find_manifest_item(manifest, b"new-name.txt")
    assert old_item["status"].startswith("R")
    assert old_item["role"] == "old"
    assert new_item["role"] == "new"
    assert (old_item["lines_added"], old_item["lines_deleted"]) == (0, 0)
    assert (new_item["lines_added"], new_item["lines_deleted"]) == (0, 0)

    for item, marker in ((old_item, b"-line one"), (new_item, b"+line one")):
        page = get_task_diff_page(
            "task-id",
            manager,
            cwd=repo,
            commit=rename_commit,
            path_selector=item["path_selector"],
            limit_bytes=MAX_LIMIT_BYTES,
        )
        assert page["complete"]
        assert marker in decode_content(page["content"])


def test_manifest_carries_line_counts(repo: Path) -> None:
    (repo / "measured.txt").write_text("one\ntwo\n")
    commit = _commit(repo, "measured")

    page = get_task_diff_page("task-id", _manager(commit), cwd=repo)

    item = _find_manifest_item(page, b"measured.txt")
    assert item["lines_added"] == 2
    assert item["lines_deleted"] == 0


def test_manifest_limit_zero_skips_numstat(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "metadata-free.txt").write_text("value\n")
    commit = _commit(repo, "metadata-free")
    calls: list[tuple[str | bytes, ...]] = []
    original_run_git = diff_paging._run_git

    def recording_run_git(args: list[str | bytes], **kwargs: Any) -> None:
        calls.append(tuple(args))
        original_run_git(args, **kwargs)

    monkeypatch.setattr(diff_paging, "_run_git", recording_run_git)

    get_task_diff_page(
        "task-id",
        _manager(commit),
        cwd=repo,
        manifest_limit=0,
    )

    assert all("--numstat" not in call for call in calls)


def test_manifest_offset_skips_numstat_for_noncontributing_commits(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / "first.txt").write_text("first\n")
    first = _commit(repo, "first")
    (repo / "second.txt").write_text("second\n")
    second = _commit(repo, "second")
    numstat_commits: list[str] = []
    original_numstat = diff_paging._numstat_totals

    def recording_numstat(commit: str, **kwargs: Any) -> dict[bytes, tuple[int | None, int | None]]:
        numstat_commits.append(commit)
        return original_numstat(commit, **kwargs)

    monkeypatch.setattr(diff_paging, "_numstat_totals", recording_numstat)
    initial = get_task_diff_page(
        "task-id",
        _manager(first, second),
        cwd=repo,
        manifest_limit=0,
    )

    page = get_task_diff_page(
        "task-id",
        _manager(first, second),
        cwd=repo,
        manifest_offset=1,
        manifest_limit=1,
        snapshot_hash=initial["snapshot_hash"],
        view_hash=initial["view_hash"],
    )

    assert page["manifest"]["items"][0]["commit"] == second
    assert numstat_commits == [second]


def test_unlinked_and_malformed_commits_rejected(repo: Path) -> None:
    (repo / "file.txt").write_text("content\n")
    linked = _commit(repo, "linked")
    (repo / "file.txt").write_text("more content\n")
    unlinked = _commit(repo, "unlinked")
    manager = _manager(linked)

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page("task-id", manager, cwd=repo, commit=unlinked)
    assert error.value.code == "commit_not_linked"

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page("task-id", manager, cwd=repo, commit="not a sha")
    assert error.value.code == "invalid_commit"

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page("task-id", manager, cwd=repo, commit="deadbeef" * 5)
    assert error.value.code == "invalid_commit"

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page("task-id", _manager("not a sha"), cwd=repo)
    assert error.value.code == "invalid_commit"


@pytest.mark.parametrize("limit", [MIN_LIMIT_BYTES - 1, -1, MAX_LIMIT_BYTES + 1])
def test_core_rejects_invalid_byte_limits_before_git(limit: int) -> None:
    manager = _TaskManager(_Task())

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page("task-id", manager, limit_bytes=limit)

    assert error.value.code == "invalid_paging_argument"
    assert error.value.details["parameter"] == "limit_bytes"


def _hanging_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "hanging-bin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\nexec sleep 10\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def _assert_process_absent(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_sync_mcp_path_returns_git_timeout_and_reaps_child(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hanging_git(tmp_path, monkeypatch)
    manager = _manager()
    registry = create_commit_registry(
        task_manager=cast(Any, manager),
        project_manager=cast(Any, _ProjectManager(repo)),
        get_task_diff_page_fn=get_task_diff_page,
        git_timeout_seconds=0.05,
    )

    result = registry.call_sync(
        "get_task_diff", {"task_id": "task-id", "include_uncommitted": True}
    )

    assert result["error_code"] == "git_timeout"
    assert result["details"]["reaped"] is True
    _assert_process_absent(cast(int, result["details"]["pid"]))


@pytest.mark.parametrize(
    ("server_timeout", "caller_timeout"),
    [(0.05, 0.5), (0.5, 0.05)],
)
def test_smaller_git_deadline_wins(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_timeout: float,
    caller_timeout: float,
) -> None:
    _hanging_git(tmp_path, monkeypatch)
    started = time.monotonic()

    with pytest.raises(DiffPagingError) as error:
        get_task_diff_page(
            "task-id",
            _manager(),
            cwd=repo,
            include_uncommitted=True,
            git_timeout_seconds=server_timeout,
            subprocess_deadline=time.monotonic() + caller_timeout,
        )

    assert error.value.code == "git_timeout"
    assert time.monotonic() - started < 0.3
    assert error.value.details["reaped"] is True
    _assert_process_absent(cast(int, error.value.details["pid"]))


def test_mcp_schema_enforces_all_server_maxima(repo: Path) -> None:
    registry = create_commit_registry(
        task_manager=cast(Any, _manager()),
        project_manager=cast(Any, _ProjectManager(repo)),
        get_task_diff_page_fn=get_task_diff_page,
    )
    schema = registry.get_schema("get_task_diff")
    assert schema is not None
    properties = schema["inputSchema"]["properties"]

    assert properties["limit_bytes"]["minimum"] == MIN_LIMIT_BYTES
    assert properties["limit_bytes"]["maximum"] == MAX_LIMIT_BYTES
    assert properties["commits_limit"]["maximum"] == MAX_COMMITS_LIMIT
    assert properties["manifest_limit"]["maximum"] == MAX_MANIFEST_LIMIT


def test_legacy_truncation_and_direct_diff_callers_are_removed() -> None:
    root = Path(__file__).parents[2]
    commits_source = (root / "src/gobby/tasks/commits.py").read_text()
    factory_source = (root / "src/gobby/mcp_proxy/tools/tasks/_factory.py").read_text()
    validation_source = (
        root / "src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py"
    ).read_text()

    assert "TASK_DIFF_MAX_CHARS" not in commits_source
    assert "_safe_truncate" not in commits_source
    assert "from gobby.tasks.commits import get_task_diff" not in factory_source
    assert "get_task_diff(" not in validation_source


def test_merge_commit_manifest_and_patch_use_the_first_parent_diff(repo: Path) -> None:
    (repo / "base.txt").write_text("base\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("one\ntwo\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "-")
    (repo / "main.txt").write_text("main\n")
    _commit(repo, "main")
    _git(repo, "merge", "--no-ff", "-m", "land side", "side")
    landing = _git(repo, "rev-parse", "HEAD").strip().decode("ascii")

    page = get_task_diff_page("task-id", _manager(landing), cwd=repo)
    content, _pages = _page_all_diff(_manager(landing), repo, limit_bytes=MAX_LIMIT_BYTES)

    item = _find_manifest_item(page, b"side.txt")
    assert (item["lines_added"], item["lines_deleted"]) == (2, 0)
    listed = [decode_content(entry["path"]) for entry in page["manifest"]["items"]]
    assert b"main.txt" not in listed
    assert b"+one\n+two\n" in content
    assert b"main.txt" not in content
