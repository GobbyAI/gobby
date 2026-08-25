"""Integration tests for content-addressed skill script materialization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.skills import materialization as materializer
from gobby.skills import script_cache
from gobby.skills.materialization import (
    MaterializationTestCommandResult,
    materialization_test_support,
)
from gobby.skills.script_cache import (
    BrowserCacheReadiness,
    async_export_file_lock,
    browser_cache_is_ready,
    collect_stale_stages,
    read_skill_scripts_owner,
    skill_scripts_root,
    stage_name,
    write_browser_cache_readiness,
    write_process_owner,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager, Skill, SkillFile

pytestmark = pytest.mark.integration

MaterializeTool = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


def _script_file(skill_id: str, path: str, content: str) -> SkillFile:
    encoded = content.encode("utf-8")
    return SkillFile(
        id="",
        skill_id=skill_id,
        path=path,
        file_type="script",
        content=content,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
    )


def _create_skill(
    db: HubDatabase,
    *,
    name: str,
    files: dict[str, str],
    metadata: dict[str, Any] | None = None,
    source_type: str = "filesystem",
    project_id: str | None = None,
) -> tuple[LocalSkillManager, Skill]:
    storage = LocalSkillManager(db)
    skill = storage.create_skill(
        name=name,
        description=f"Materialization fixture for {name}",
        content=f"# {name}\n\nSafe fixture.",
        metadata=metadata,
        source_type=cast(Any, source_type),
        source_path=f"/read-only/{name}/SKILL.md",
        project_id=project_id,
    )
    storage.set_skill_files(
        skill.id,
        [_script_file(skill.id, path, content) for path, content in files.items()],
    )
    return storage, skill


def _tool(db: HubDatabase, *, project_id: str | None = None) -> MaterializeTool:
    value = create_skills_registry(db, project_id=project_id).get_tool("materialize_skill_scripts")
    assert value is not None
    return cast(MaterializeTool, value)


def _package_files() -> dict[str, str]:
    package = {"name": "fixture", "private": True, "dependencies": {}}
    lock = {
        "name": "fixture",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {"": {"name": "fixture"}},
    }
    return {
        "scripts/package.json": json.dumps(package),
        "scripts/package-lock.json": json.dumps(lock),
        "scripts/detect.mjs": "export const ready = true;\n",
    }


def _set_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_materializes_from_db_bytes_only(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    storage, skill = _create_skill(
        temp_db,
        name="db-only-materialize",
        files={"scripts/detect.mjs": "export const source = 'database';\n"},
    )
    source = Path(cast(str, skill.source_path)).parent / "scripts" / "detect.mjs"
    assert not source.exists()

    result = await _tool(temp_db)(name=skill.name)

    scripts_dir = Path(cast(str, result["scripts_dir"]))
    assert (scripts_dir / "detect.mjs").read_text() == "export const source = 'database';\n"
    assert result["files_written"] == 1
    assert scripts_dir.is_relative_to(home)
    assert storage.get_skill_with_scripts(name=skill.name, project_id=None) is not None


def test_rejects_unsafe_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _set_home(monkeypatch, tmp_path)

    with pytest.raises(materializer.SkillMaterializationError, match="Unsafe"):
        materialization_test_support.validate_script_path("scripts/../../outside.js")

    assert not home.exists()


@pytest.mark.asyncio
async def test_old_generation_survives_new_publish(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    storage, skill = _create_skill(
        temp_db,
        name="retained-generation",
        files={"scripts/value.js": "export default 'old';\n"},
    )
    tool = _tool(temp_db)
    old = await tool(name=skill.name)
    old_dir = Path(cast(str, old["scripts_dir"]))
    storage.set_skill_files(
        skill.id,
        [_script_file(skill.id, "scripts/value.js", "export default 'new';\n")],
    )

    new = await tool(name=skill.name)

    new_dir = Path(cast(str, new["scripts_dir"]))
    assert new_dir != old_dir
    assert (old_dir / "value.js").read_text() == "export default 'old';\n"
    assert (new_dir / "value.js").read_text() == "export default 'new';\n"


@pytest.mark.asyncio
async def test_metadata_only_revision_publishes_new_generation(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    storage, skill = _create_skill(
        temp_db,
        name="metadata-revision",
        files={"scripts/value.js": "export default 1;\n"},
        metadata={"gobby": {"runtime": {"node": ">=22.18.0", "skill_release": "1.0.0"}}},
    )
    tool = _tool(temp_db)
    first = await tool(name=skill.name)
    storage.update_skill(
        skill.id,
        metadata={"gobby": {"runtime": {"node": ">=23.0.0", "skill_release": "2.0.0"}}},
    )

    second = await tool(name=skill.name)

    first_dir = Path(cast(str, first["scripts_dir"]))
    second_dir = Path(cast(str, second["scripts_dir"]))
    assert first_dir != second_dir
    assert first_dir.exists()
    provenance = json.loads((second_dir.parent / "provenance.json").read_text())
    assert provenance["runtime"]["skill_release"] == "2.0.0"


@pytest.mark.asyncio
async def test_trust_transition_publishes_distinct_generation(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    storage, skill = _create_skill(
        temp_db,
        name="trust-transition",
        source_type="github",
        files={"scripts/value.js": "export default 1;\n"},
    )
    monkeypatch.setattr(
        materialization_test_support,
        "scan_skill_content",
        lambda *_args, **_kwargs: {"is_safe": True},
    )
    tool = _tool(temp_db)
    external = await tool(name=skill.name)
    storage.update_skill(skill.id, source_type="filesystem")

    trusted = await tool(name=skill.name)

    assert external["scripts_dir"] != trusted["scripts_dir"]
    assert not (Path(cast(str, external["scripts_dir"])) / "node_modules").exists()


@pytest.mark.asyncio
async def test_degrades_without_npm(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(temp_db, name="npm-missing", files=_package_files())
    original = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: None if name == "npm" else original(name),
    )

    result = await _tool(temp_db)(name=skill.name)

    assert Path(cast(str, result["scripts_dir"])).is_dir()
    assert result["parser_deps"]["installed"] is False
    assert "npm is unavailable" in result["parser_deps"]["warning"]


@pytest.mark.asyncio
async def test_missing_node_reports_independently(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(temp_db, name="node-missing", files=_package_files())
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = await _tool(temp_db)(name=skill.name)

    assert result["node"] == {"version": None, "satisfies_floor": None}
    assert result["parser_deps"]["installed"] is False
    assert Path(cast(str, result["scripts_dir"])).is_dir()


@pytest.mark.asyncio
async def test_malformed_legacy_runtime_treated_as_absent(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="legacy-runtime",
        files={"scripts/value.js": "export default 1;\n"},
    )
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE skills SET metadata = %s WHERE id = %s",
            (json.dumps({"gobby": {"runtime": {"node": "latest"}}}), skill.id),
        )

    result = await _tool(temp_db)(name=skill.name)

    assert result["node"]["satisfies_floor"] is None
    assert "Ignoring invalid runtime metadata" in result["parser_deps"]["warning"]


@pytest.mark.asyncio
async def test_owner_marker_written(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="owner-marker",
        files={"scripts/value.js": "export default 1;\n"},
    )

    await _tool(temp_db)(name=skill.name)

    owner = read_skill_scripts_owner(skill_scripts_root(home, skill.id))
    assert owner is not None
    assert (owner.skill_id, owner.skill_name) == (skill.id, skill.name)


@pytest.mark.asyncio
async def test_scan_gate_covers_all_script_files(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="external-scan",
        source_type="github",
        files={
            "scripts/safe.js": "export default 1;\n",
            "scripts/unsafe.js": "malicious();\n",
        },
    )
    scanned: list[dict[str, str]] = []

    def scan(_content: str, _name: str, files: dict[str, str]) -> dict[str, object]:
        scanned.append(files)
        return {"is_safe": False, "max_severity": "HIGH"}

    monkeypatch.setattr(materialization_test_support, "scan_skill_content", scan)

    result = await _tool(temp_db)(name=skill.name)

    assert result["success"] is False
    assert list(scanned) == [
        {
            "scripts/safe.js": "export default 1;\n",
            "scripts/unsafe.js": "malicious();\n",
        }
    ]
    assert not skill_scripts_root(home, skill.id).exists()


@pytest.mark.asyncio
async def test_returns_browser_cache_environment(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="browser-environment",
        files={"scripts/value.js": "export default 1;\n"},
    )

    result = await _tool(temp_db)(name=skill.name)

    cache = Path(result["environment"]["PUPPETEER_CACHE_DIR"])
    assert cache.is_absolute()
    assert cache == (home / "cache" / "puppeteer").resolve()


@pytest.mark.asyncio
async def test_external_source_never_installs_dependencies(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="external-no-exec",
        source_type="github",
        files=_package_files(),
    )
    monkeypatch.setattr(
        materializer,
        "scan_skill_content",
        lambda *_args, **_kwargs: {"is_safe": True},
    )

    async def forbidden(*_args: object, **_kwargs: object) -> MaterializationTestCommandResult:
        raise AssertionError("external skill attempted executable setup")

    monkeypatch.setattr(materialization_test_support, "run_owned_subprocess", forbidden)

    result = await _tool(temp_db)(name=skill.name)

    assert result["parser_deps"]["installed"] is False
    assert result["browser"]["ready"] is False
    assert "External-source" in result["parser_deps"]["warning"]


@pytest.mark.asyncio
async def test_native_win32_materializes_scripts_only(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(temp_db, name="windows-cache", files=_package_files())
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    async def forbidden(*_args: object, **_kwargs: object) -> MaterializationTestCommandResult:
        raise AssertionError("win32 attempted executable setup")

    monkeypatch.setattr(materialization_test_support, "run_owned_subprocess", forbidden)

    result = await _tool(temp_db)(name=skill.name)

    assert Path(cast(str, result["scripts_dir"])).is_dir()
    assert result["parser_deps"]["installed"] is False
    assert "win32" in result["parser_deps"]["warning"]


@pytest.mark.asyncio
async def test_first_root_crash_leaves_no_unmarked_root(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="first-root-crash",
        files={"scripts/value.js": "export default 1;\n"},
    )
    original = materialization_test_support.publication_checkpoint

    def crash(name: str) -> None:
        if name == "before-first-root-rename":
            raise OSError("injected crash")

    monkeypatch.setattr(materialization_test_support, "publication_checkpoint", crash)
    failed = await _tool(temp_db)(name=skill.name)
    root = skill_scripts_root(home, skill.id)
    assert failed["success"] is False
    assert not root.exists()

    monkeypatch.setattr(materialization_test_support, "publication_checkpoint", original)
    result = await _tool(temp_db)(name=skill.name)

    assert Path(cast(str, result["scripts_dir"])).is_dir()
    assert read_skill_scripts_owner(root) is not None


@pytest.mark.asyncio
async def test_concurrent_materialize_single_install(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(temp_db, name="concurrent-install", files=_package_files())
    original = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/fixture/npm" if name == "npm" else original(name),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def install(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        owner_record: Path,
    ) -> MaterializationTestCommandResult:
        nonlocal calls
        del env, owner_record
        calls += 1
        (cwd / "node_modules").mkdir()
        started.set()
        await release.wait()
        return MaterializationTestCommandResult(0, "", "")

    monkeypatch.setattr(materialization_test_support, "run_owned_subprocess", install)
    tool = _tool(temp_db)
    first: asyncio.Task[dict[str, Any]] = asyncio.create_task(tool(name=skill.name))
    await started.wait()
    second: asyncio.Task[dict[str, Any]] = asyncio.create_task(tool(name=skill.name))
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls == 1
    assert first_result["scripts_dir"] == second_result["scripts_dir"]
    assert first_result["parser_deps"]["installed"] is True


@pytest.mark.asyncio
async def test_local_lock_serializes_same_key_and_cleans_registry() -> None:
    loop = asyncio.get_running_loop()
    key = "serialize-and-clean"
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    entries: list[str] = []

    async def hold_first() -> None:
        async with materializer._local_lock(key):
            entries.append("first")
            first_entered.set()
            await release_first.wait()

    async def wait_second() -> None:
        second_started.set()
        async with materializer._local_lock(key):
            entries.append("second")

    first = asyncio.create_task(hold_first())
    await first_entered.wait()
    second = asyncio.create_task(wait_second())
    await second_started.wait()

    state = materializer._LOCAL_LOCKS[loop][key]
    assert state.users == 2
    assert entries == ["first"]

    release_first.set()
    await asyncio.gather(first, second)

    assert entries == ["first", "second"]
    assert loop not in materializer._LOCAL_LOCKS


@pytest.mark.asyncio
async def test_local_lock_cancelled_waiter_retains_shared_entry_until_holder_exits() -> None:
    loop = asyncio.get_running_loop()
    key = "cancelled-waiter"
    waiter_started = asyncio.Event()

    async with materializer._local_lock(key):
        state = materializer._LOCAL_LOCKS[loop][key]

        async def wait_for_lock() -> None:
            waiter_started.set()
            async with materializer._local_lock(key):
                raise AssertionError("cancelled waiter acquired lock")

        waiter = asyncio.create_task(wait_for_lock())
        await waiter_started.wait()
        assert state.users == 2

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert materializer._LOCAL_LOCKS[loop][key] is state
        assert state.users == 1

    assert loop not in materializer._LOCAL_LOCKS


@pytest.mark.asyncio
async def test_failed_install_retry_attaches_deps(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(temp_db, name="retry-install", files=_package_files())
    original = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/fixture/npm" if name == "npm" else original(name),
    )
    attempts = 0

    async def install(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        owner_record: Path,
    ) -> MaterializationTestCommandResult:
        nonlocal attempts
        del env, owner_record
        attempts += 1
        if attempts == 1:
            return MaterializationTestCommandResult(1, "", "offline")
        (cwd / "node_modules").mkdir()
        return MaterializationTestCommandResult(0, "", "")

    monkeypatch.setattr(materialization_test_support, "run_owned_subprocess", install)
    tool = _tool(temp_db)
    first = await tool(name=skill.name)
    scripts = Path(cast(str, first["scripts_dir"]))
    before = (scripts / "detect.mjs").read_bytes()

    second = await tool(name=skill.name)

    assert first["parser_deps"]["installed"] is False
    assert second["parser_deps"]["installed"] is True
    assert (scripts / "detect.mjs").read_bytes() == before
    assert attempts == 2


@pytest.mark.asyncio
async def test_materializer_missing_local_puppeteer_never_invokes_npx(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    files = {
        "scripts/node_modules/puppeteer/package.json": json.dumps({"version": "25.5.0"}),
        "scripts/node_modules/puppeteer-core/lib/puppeteer/revisions.js": (
            "export const PUPPETEER_REVISIONS = { chrome: '151.0.0.0' };\n"
        ),
    }
    _, skill = _create_skill(temp_db, name="no-local-puppeteer", files=files)

    async def dependencies(**_kwargs: object) -> tuple[bool, str | None]:
        return True, None

    async def forbidden(*_args: object, **_kwargs: object) -> MaterializationTestCommandResult:
        raise AssertionError("missing local binary attempted a subprocess")

    monkeypatch.setattr(materialization_test_support, "ensure_dependencies", dependencies)
    monkeypatch.setattr(materialization_test_support, "run_owned_subprocess", forbidden)

    result = await _tool(temp_db)(name=skill.name)

    assert result["browser"]["ready"] is False
    assert "Local Puppeteer executable is unavailable" in result["browser"]["warning"]


@pytest.mark.asyncio
async def test_browser_readiness_is_compatibility_keyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    scripts = tmp_path / "scripts"
    (scripts / "node_modules" / "puppeteer").mkdir(parents=True)
    (scripts / "node_modules" / "puppeteer" / "package.json").write_text(
        json.dumps({"version": "25.5.0"})
    )
    revisions = scripts / "node_modules" / "puppeteer-core" / "lib" / "puppeteer"
    revisions.mkdir(parents=True)
    (revisions / "revisions.js").write_text("export const x = { chrome: '151.0.0.0' };\n")
    binary = scripts / "node_modules" / ".bin" / "puppeteer"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture")
    cache = home / "cache" / "puppeteer"
    old = BrowserCacheReadiness(sys.platform, "24.0.0", "150.0.0.0", "chrome")
    write_browser_cache_readiness(cache, old)
    calls = 0

    async def fetch(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        owner_record: Path,
    ) -> MaterializationTestCommandResult:
        nonlocal calls
        del cwd, owner_record
        calls += 1
        artifact = Path(env["PUPPETEER_CACHE_DIR"]) / "chrome" / "chrome-151.0.0.0"
        artifact.mkdir(parents=True)
        binary = artifact / "chrome"
        binary.write_text("fixture")
        binary.chmod(0o700)
        return MaterializationTestCommandResult(0, "", "")

    monkeypatch.setattr(materialization_test_support, "run_owned_subprocess", fetch)

    ready, warning = await materialization_test_support.ensure_browser(
        home=home,
        scripts_dir=scripts,
        dependencies_ready=True,
        external=False,
    )

    expected = BrowserCacheReadiness(sys.platform, "25.5.0", "151.0.0.0", "chrome")
    assert (ready, warning, calls) == (True, None, 1)
    assert browser_cache_is_ready(cache, expected)

    shutil.rmtree(cache / "chrome" / "chrome-151.0.0.0")
    repaired, repaired_warning = await materialization_test_support.ensure_browser(
        home=home,
        scripts_dir=scripts,
        dependencies_ready=True,
        external=False,
    )
    assert (repaired, repaired_warning, calls) == (True, None, 2)


@pytest.mark.asyncio
async def test_lock_waiter_cancellation_leaves_lock_free(tmp_path: Path) -> None:
    target = tmp_path / "cache" / "root"
    waiter_started = asyncio.Event()

    async with async_export_file_lock(target):

        async def wait_for_lock() -> None:
            waiter_started.set()
            async with async_export_file_lock(target):
                raise AssertionError("cancelled waiter acquired lock")

        waiter = asyncio.create_task(wait_for_lock())
        await waiter_started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async with asyncio.timeout(1):
        async with async_export_file_lock(target):
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_process_start_identity_does_not_shell_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("process identity attempted to shell out")

    monkeypatch.setattr(subprocess, "run", forbidden)

    assert script_cache.process_start_identity(os.getpid()) is not None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
async def test_timeout_reaps_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pgid_file = tmp_path / "pgid"
    program = (
        "import os, signal; "
        f"open({str(pgid_file)!r}, 'w').write(str(os.getpgrp())); "
        "os.fork(); signal.pause()"
    )
    monkeypatch.setattr(materialization_test_support, "subprocess_timeout_seconds", 0.25)

    with pytest.raises(materializer.SkillMaterializationError, match="Timed out"):
        await materialization_test_support.run_owned_subprocess(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            env=os.environ.copy(),
            owner_record=tmp_path / "owner.json",
        )

    pgid = int(pgid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_orphaned_group_blocks_stage_collection(tmp_path: Path) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    stage = parent / stage_name("deps", "live")
    stage.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    try:
        write_process_owner(stage / ".gobby-process-owner.json", process.pid)

        live = collect_stale_stages(parent)

        assert live == [stage]
        assert stage.exists()
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_repeated_crashes_leave_no_stage_accumulation(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="repeated-crash",
        files={"scripts/value.js": "export default 1;\n"},
    )

    def crash(_name: str) -> None:
        raise OSError("injected publication crash")

    monkeypatch.setattr(materialization_test_support, "publication_checkpoint", crash)
    tool = _tool(temp_db)
    for _ in range(2):
        result = await tool(name=skill.name)
        assert result["success"] is False

    namespace = skill_scripts_root(home, skill.id).parent
    assert not [path for path in namespace.iterdir() if path.name.startswith(".gobby-stage-")]


@pytest.mark.asyncio
async def test_cold_materialization_keeps_event_loop_responsive(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="responsive-cold-cache",
        files={"scripts/value.js": "export default 1;\n"},
    )
    entered = threading.Event()
    release = threading.Event()
    original = materialization_test_support.write_generation

    def blocked_write(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original(*args, **kwargs)

    monkeypatch.setattr(materialization_test_support, "write_generation", blocked_write)
    task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_tool(temp_db)(name=skill.name))
    assert await asyncio.to_thread(entered.wait, 5)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await heartbeat.wait()
    release.set()

    result = await task

    assert Path(cast(str, result["scripts_dir"])).is_dir()


@pytest.mark.asyncio
async def test_materialization_reads_one_revision(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="single-snapshot",
        files={"scripts/value.js": "export default 1;\n"},
    )
    calls = 0

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await asyncio.to_thread(func, *args, **kwargs)

    value = create_skills_registry(temp_db, run_db=run_db).get_tool("materialize_skill_scripts")
    assert value is not None
    tool = cast(MaterializeTool, value)

    result = await tool(name=skill.name)

    assert Path(cast(str, result["scripts_dir"])).is_dir()
    assert calls == 1


@pytest.mark.asyncio
async def test_name_resolution_matches_get_skill(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_home(monkeypatch, tmp_path)
    project_id = cast(str, sample_project["id"])
    _, global_skill = _create_skill(
        temp_db,
        name="resolution-parity",
        files={"scripts/value.js": "export default 'global';\n"},
    )
    project_storage, project_skill = _create_skill(
        temp_db,
        name="resolution-parity",
        project_id=project_id,
        files={"scripts/value.js": "export default 'project';\n"},
    )
    tool = _tool(temp_db, project_id=project_id)

    project_result = await tool(name=project_skill.name)
    assert (Path(project_result["scripts_dir"]) / "value.js").read_text() == (
        "export default 'project';\n"
    )

    project_storage.update_skill(
        project_skill.id,
        source_path="/tmp/gobby/install/shared/skills/resolution-parity/SKILL.md",
    )
    global_result = await tool(name=global_skill.name)

    assert (Path(global_result["scripts_dir"]) / "value.js").read_text() == (
        "export default 'global';\n"
    )


@pytest.mark.asyncio
async def test_missing_cached_script_is_republished(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="cache-recovery",
        files={"scripts/hook.mjs": "export const recovered = true;\n"},
    )
    tool = _tool(temp_db)
    first = await tool(name=skill.name)
    cached_script = Path(first["scripts_dir"]) / "hook.mjs"
    cached_script.unlink()

    second = await tool(name=skill.name)

    assert second["scripts_dir"] == first["scripts_dir"]
    assert cached_script.read_text() == "export const recovered = true;\n"


@pytest.mark.asyncio
async def test_timed_out_waiter_leaves_shared_materialization_running(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="single-flight-timeout",
        files={"scripts/hook.mjs": "export {};\n"},
    )
    service = materializer.SkillScriptMaterializer(temp_db)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def gated_run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return func(*args, **kwargs)

    waiter = asyncio.create_task(service.resolve(skill.name, project_id=None, run_db=gated_run_db))
    await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(waiter, timeout=0.01)

    release.set()
    result = await service.resolve(skill.name, project_id=None, run_db=gated_run_db)

    assert calls == 1
    assert (result.scripts_dir / "hook.mjs").is_file()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inflight_materializations_are_scoped_to_the_current_event_loop(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_home(monkeypatch, tmp_path)
    _, skill = _create_skill(
        temp_db,
        name="multi-loop-single-flight",
        files={"scripts/hook.mjs": "export {};\n"},
    )
    service = materializer.SkillScriptMaterializer(temp_db)
    release = threading.Event()
    both_started = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    async def gated_run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        with calls_lock:
            calls += 1
            if calls == 2:
                both_started.set()
        assert await asyncio.to_thread(release.wait, 2)
        return func(*args, **kwargs)

    async def resolve_in_new_loop() -> materializer.SkillMaterializationResult:
        return await service.resolve(skill.name, project_id=None, run_db=gated_run_db)

    thread_result = asyncio.create_task(
        asyncio.to_thread(lambda: asyncio.run(resolve_in_new_loop()))
    )
    main_result = asyncio.create_task(
        service.resolve(skill.name, project_id=None, run_db=gated_run_db)
    )
    try:
        assert await asyncio.to_thread(both_started.wait, 2)
    finally:
        release.set()

    first, second = await asyncio.gather(main_result, thread_result)

    assert calls == 2
    assert first.scripts_dir == second.scripts_dir
