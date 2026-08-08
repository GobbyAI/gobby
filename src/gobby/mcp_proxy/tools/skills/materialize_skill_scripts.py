"""Materialize database-backed skill scripts into immutable cache generations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.paths import get_gobby_home
from gobby.skills.parser import SkillParseError, validate_runtime_metadata
from gobby.skills.scanner import is_external_source, scan_skill_content
from gobby.skills.script_cache import (
    BROWSER_FETCH_OWNER_FILE,
    DEPENDENCY_READY_FILE,
    PROCESS_OWNER_FILE,
    BrowserCacheReadiness,
    SkillScriptsOwner,
    async_browser_cache_lock,
    async_export_file_lock,
    browser_cache_is_ready,
    collect_stale_stages,
    fsync_directory,
    fsync_tree,
    process_owner_is_live,
    run_blocking_safely,
    skill_scripts_namespace,
    skill_scripts_namespace_lock_target,
    skill_scripts_root,
    skill_scripts_root_lock_target,
    stage_name,
    write_browser_cache_readiness,
    write_generation_provenance,
    write_process_owner,
    write_skill_scripts_owner,
)
from gobby.storage.skills import Skill

_SUBPROCESS_TIMEOUT_SECONDS = 180.0
_TERMINATE_GRACE_SECONDS = 5.0
_REVISION_PATTERN = re.compile(r"chrome\s*:\s*['\"](?P<build>[^'\"]+)['\"]")
_NODE_VERSION_PATTERN = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+)$")
_BARRIER_PROGRAM = """
import os
import sys

fd = int(sys.argv[1])
token = os.read(fd, 1)
os.close(fd)
if not token:
    os._exit(125)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""
_LOCAL_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


class SkillMaterializationError(RuntimeError):
    """A skill cannot be safely materialized or prepared for execution."""


@dataclass(frozen=True)
class _ScriptPayload:
    path: str
    content: str
    content_hash: str


@dataclass(frozen=True)
class _RuntimeSpec:
    normalized: dict[str, object]
    node_floor: str | None
    warning: str | None


@dataclass(frozen=True)
class _NodeState:
    version: str | None
    satisfies_floor: bool | None
    warning: str | None


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the script materialization boundary."""

    @registry.tool(
        name="materialize_skill_scripts",
        description="Materialize a skill's scripts into Gobby's content-addressed cache.",
    )
    async def materialize_tool(name: str) -> dict[str, Any]:
        try:
            return await materialize_skill_scripts(ctx, name)
        except (OSError, SkillMaterializationError, ValueError) as exc:
            return {"success": False, "error": str(exc)}


async def materialize_skill_scripts(ctx: SkillsContext, name: str) -> dict[str, Any]:
    """Materialize one resolved skill revision and prepare optional runtime artifacts."""
    snapshot = await ctx.run_db(
        ctx.storage.get_skill_with_scripts,
        name=name,
        project_id=ctx.project_id,
    )
    if snapshot is None:
        raise SkillMaterializationError(f"Skill not found: {name}")
    skill_value = snapshot.get("skill")
    if not isinstance(skill_value, Skill):
        raise SkillMaterializationError("Skill storage returned an invalid snapshot")
    skill = skill_value
    payloads = _parse_payloads(snapshot.get("files"))
    runtime = _runtime_spec(skill)
    external = is_external_source(skill.source_type)

    if external:
        result = await run_blocking_safely(
            scan_skill_content,
            skill.content,
            skill.name,
            {payload.path: payload.content for payload in payloads},
        )
        if not bool(result.get("is_safe")):
            severity = result.get("max_severity", "UNKNOWN")
            raise SkillMaterializationError(
                f"Skill '{skill.name}' failed script safety scan ({severity})"
            )

    home = get_gobby_home().resolve()
    cache_root = skill_scripts_root(home, skill.id)
    digest = _generation_digest(payloads, runtime.normalized, external)
    generation = cache_root / digest
    local_lock = _local_lock(f"{skill.id}:{digest}")
    async with local_lock:
        stage_warnings = await _publish_scripts_generation(
            home=home,
            skill=skill,
            payloads=payloads,
            generation=generation,
            runtime=runtime,
            external=external,
        )
        node = await _node_state(runtime.node_floor)
        dependency_installed, dependency_warning = await _ensure_dependencies(
            cache_root=cache_root,
            generation=generation,
            external=external,
            node=node,
        )

    browser_ready, browser_warning = await _ensure_browser(
        home=home,
        scripts_dir=generation / "scripts",
        dependencies_ready=dependency_installed,
        external=external,
    )
    parser_warning = _join_warnings(
        runtime.warning,
        node.warning,
        dependency_warning,
        *stage_warnings,
    )
    browser_cache = (home / "cache" / "puppeteer").resolve()
    return {
        "scripts_dir": str((generation / "scripts").resolve()),
        "files_written": len(payloads),
        "environment": {"PUPPETEER_CACHE_DIR": str(browser_cache)},
        "parser_deps": {"installed": dependency_installed, "warning": parser_warning},
        "browser": {"ready": browser_ready, "warning": browser_warning},
        "node": {
            "version": node.version,
            "satisfies_floor": node.satisfies_floor,
        },
    }


def _local_lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _LOCAL_LOCKS.setdefault(loop, {})
    return locks.setdefault(key, asyncio.Lock())


def _parse_payloads(value: object) -> list[_ScriptPayload]:
    if not isinstance(value, list):
        raise SkillMaterializationError("Skill storage returned an invalid scripts inventory")
    payloads: list[_ScriptPayload] = []
    for item in value:
        if not isinstance(item, dict):
            raise SkillMaterializationError("Skill storage returned an invalid script record")
        path = item.get("path")
        content = item.get("content")
        content_hash = item.get("content_hash")
        if not all(isinstance(field, str) for field in (path, content, content_hash)):
            raise SkillMaterializationError("Skill storage returned an incomplete script record")
        path = cast(str, path)
        content = cast(str, content)
        content_hash = cast(str, content_hash)
        _validate_script_path(path)
        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual_hash != content_hash:
            raise SkillMaterializationError(f"Stored content hash mismatch for {path}")
        payloads.append(_ScriptPayload(path, content, content_hash))
    payloads.sort(key=lambda payload: payload.path)
    return payloads


def _validate_script_path(path: str) -> None:
    candidate = PurePosixPath(path)
    if (
        not path.startswith("scripts/")
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "\\" in path
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise SkillMaterializationError(f"Unsafe skill script path: {path}")


def _runtime_spec(skill: Skill) -> _RuntimeSpec:
    try:
        validate_runtime_metadata(skill.metadata)
    except SkillParseError as exc:
        return _RuntimeSpec({}, None, f"Ignoring invalid runtime metadata: {exc}")
    metadata = skill.metadata or {}
    gobby = metadata.get("gobby")
    if not isinstance(gobby, dict):
        return _RuntimeSpec({}, None, None)
    value = gobby.get("runtime")
    if not isinstance(value, dict):
        return _RuntimeSpec({}, None, None)
    runtime: dict[str, object] = {}
    node = value.get("node")
    if isinstance(node, str):
        runtime["node"] = node
    release = value.get("skill_release")
    if isinstance(release, str):
        runtime["skill_release"] = release
    cli = value.get("cli")
    if isinstance(cli, dict):
        runtime["cli"] = {
            key: cli[key] for key in ("npm", "version", "bin") if isinstance(cli.get(key), str)
        }
    return _RuntimeSpec(runtime, cast(str | None, runtime.get("node")), None)


def _generation_digest(
    payloads: list[_ScriptPayload], runtime: dict[str, object], external: bool
) -> str:
    value = {
        "files": {payload.path: payload.content_hash for payload in payloads},
        "runtime": runtime,
        "external_source": external,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _publish_scripts_generation(
    *,
    home: Path,
    skill: Skill,
    payloads: list[_ScriptPayload],
    generation: Path,
    runtime: _RuntimeSpec,
    external: bool,
) -> list[str]:
    namespace = skill_scripts_namespace(home)
    root = generation.parent
    provenance = {
        "schema_version": 1,
        "skill_id": skill.id,
        "skill_name": skill.name,
        "source_type": skill.source_type,
        "external_source": external,
        "runtime": runtime.normalized,
        "files": {payload.path: payload.content_hash for payload in payloads},
    }
    while True:
        if root.exists():
            async with async_export_file_lock(skill_scripts_root_lock_target(root)):
                if not root.exists():
                    continue
                warnings = await _validate_root_and_collect(root, skill)
                if not generation.exists():
                    await run_blocking_safely(
                        _publish_warm_generation,
                        root,
                        generation,
                        payloads,
                        provenance,
                    )
                return warnings
        async with async_export_file_lock(skill_scripts_namespace_lock_target(namespace)):
            live = await run_blocking_safely(collect_stale_stages, namespace)
            if root.exists():
                continue
            await run_blocking_safely(
                _publish_first_root,
                namespace,
                root,
                generation.name,
                SkillScriptsOwner(skill.id, skill.name, skill.source_type),
                payloads,
                provenance,
            )
            return [_live_stage_warning(path) for path in live]


async def _validate_root_and_collect(root: Path, skill: Skill) -> list[str]:
    from gobby.skills.script_cache import read_skill_scripts_owner

    owner = await run_blocking_safely(read_skill_scripts_owner, root)
    if owner is None or owner.skill_id != skill.id or owner.skill_name != skill.name:
        raise SkillMaterializationError(f"Unowned skill scripts cache root: {root}")
    live = await run_blocking_safely(collect_stale_stages, root)
    return [_live_stage_warning(path) for path in live]


def _publish_first_root(
    namespace: Path,
    root: Path,
    digest: str,
    owner: SkillScriptsOwner,
    payloads: list[_ScriptPayload],
    provenance: dict[str, object],
) -> None:
    namespace.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = namespace / stage_name("root", f"{root.name}-{uuid.uuid4().hex}")
    try:
        stage.mkdir(mode=0o700)
        write_skill_scripts_owner(stage, owner)
        _write_generation(stage / digest, payloads, provenance)
        fsync_tree(stage)
        _publication_checkpoint("before-first-root-rename")
        os.replace(stage, root)
        fsync_directory(namespace)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _publish_warm_generation(
    root: Path,
    generation: Path,
    payloads: list[_ScriptPayload],
    provenance: dict[str, object],
) -> None:
    stage = root / stage_name("scripts", uuid.uuid4().hex)
    try:
        _write_generation(stage, payloads, provenance)
        fsync_tree(stage)
        _publication_checkpoint("before-scripts-rename")
        os.replace(stage, generation)
        fsync_directory(root)
    except FileExistsError:
        shutil.rmtree(stage, ignore_errors=True)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _write_generation(
    generation: Path,
    payloads: list[_ScriptPayload],
    provenance: dict[str, object],
) -> None:
    scripts = generation / "scripts"
    scripts.mkdir(mode=0o700, parents=True)
    for payload in payloads:
        relative = PurePosixPath(payload.path).relative_to("scripts")
        target = scripts.joinpath(*relative.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(payload.content, encoding="utf-8")
    write_generation_provenance(generation, provenance)


async def _node_state(floor: str | None) -> _NodeState:
    node = shutil.which("node")
    if node is None:
        return _NodeState(None, None, "Node executable is unavailable")
    version = await run_blocking_safely(_read_node_version, node)
    if version is None:
        return _NodeState(None, None, "Node version could not be determined")
    if floor is None:
        return _NodeState(version, None, None)
    floor_version = floor.removeprefix(">=")
    satisfies = _version_tuple(version) >= _version_tuple(floor_version)
    warning = None if satisfies else f"Node {version} does not satisfy {floor}"
    return _NodeState(version, satisfies, warning)


def _read_node_version(node: str) -> str | None:
    try:
        result = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _NODE_VERSION_PATTERN.fullmatch(result.stdout.strip())
    return match.group("version") if result.returncode == 0 and match else None


def _version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


async def _ensure_dependencies(
    *,
    cache_root: Path,
    generation: Path,
    external: bool,
    node: _NodeState,
) -> tuple[bool, str | None]:
    scripts = generation / "scripts"
    manifest = scripts / "package.json"
    lockfile = scripts / "package-lock.json"
    target = scripts / "node_modules"
    if external:
        return False, "External-source skills cannot install executable dependencies"
    if sys.platform == "win32":
        return False, "Managed script dependencies are unavailable on native win32"
    if not manifest.is_file():
        return False, None
    if not lockfile.is_file():
        return False, "Script package-lock.json is unavailable"
    if node.version is None:
        return False, "Node is unavailable; parser dependencies were not installed"
    npm = shutil.which("npm")
    if npm is None:
        return False, "npm is unavailable; scripts remain usable in line-based mode"
    async with async_export_file_lock(skill_scripts_root_lock_target(cache_root)):
        if not generation.exists():
            raise SkillMaterializationError("Materialized generation disappeared")
        live = await run_blocking_safely(collect_stale_stages, cache_root)
        if live:
            return False, _join_warnings(*(_live_stage_warning(path) for path in live))
        lock_hash = await run_blocking_safely(_file_hash, lockfile)
        if await run_blocking_safely(_dependencies_ready, target, lock_hash):
            return True, None
        if target.exists():
            await run_blocking_safely(shutil.rmtree, target)
        stage = cache_root / stage_name("deps", uuid.uuid4().hex)
        try:
            await run_blocking_safely(_prepare_dependency_stage, stage, manifest, lockfile)
            env = os.environ.copy()
            env["PUPPETEER_CACHE_DIR"] = str((get_gobby_home() / "cache" / "puppeteer").resolve())
            result = await _run_owned_subprocess(
                [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=stage,
                env=env,
                owner_record=stage / PROCESS_OWNER_FILE,
            )
            staged_modules = stage / "node_modules"
            if result.returncode != 0 or not staged_modules.is_dir():
                warning = result.stderr.strip() or "npm ci failed"
                return False, f"Parser dependency install failed: {warning}"
            await run_blocking_safely(_mark_dependencies_ready, staged_modules, lock_hash)
            await run_blocking_safely(fsync_tree, staged_modules)
            _publication_checkpoint("before-dependency-attach")
            await run_blocking_safely(os.replace, staged_modules, target)
            await run_blocking_safely(fsync_directory, scripts)
            return True, None
        except asyncio.CancelledError:
            raise
        except (OSError, SkillMaterializationError) as exc:
            return False, f"Parser dependency install failed: {exc}"
        finally:
            await run_blocking_safely(shutil.rmtree, stage, True)


def _prepare_dependency_stage(stage: Path, manifest: Path, lockfile: Path) -> None:
    stage.mkdir(mode=0o700)
    shutil.copy2(manifest, stage / manifest.name)
    shutil.copy2(lockfile, stage / lockfile.name)


def _mark_dependencies_ready(node_modules: Path, lock_hash: str) -> None:
    (node_modules / DEPENDENCY_READY_FILE).write_text(
        json.dumps({"schema_version": 1, "lock_hash": lock_hash}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _dependencies_ready(node_modules: Path, lock_hash: str) -> bool:
    try:
        value = json.loads((node_modules / DEPENDENCY_READY_FILE).read_text(encoding="utf-8"))
        return node_modules.is_dir() and value == {
            "schema_version": 1,
            "lock_hash": lock_hash,
        }
    except (OSError, TypeError, ValueError):
        return False


async def _ensure_browser(
    *,
    home: Path,
    scripts_dir: Path,
    dependencies_ready: bool,
    external: bool,
) -> tuple[bool, str | None]:
    if external:
        return False, "External-source skills cannot execute browser installers"
    if sys.platform == "win32":
        return False, "Managed browser setup is unavailable on native win32"
    if not dependencies_ready:
        return False, "Browser setup requires installed parser dependencies"
    try:
        expected = await run_blocking_safely(_browser_expectation, scripts_dir)
    except SkillMaterializationError as exc:
        return False, str(exc)
    executable = scripts_dir / "node_modules" / ".bin" / "puppeteer"
    if not executable.exists():
        return False, "Local Puppeteer executable is unavailable; browser fetch skipped"
    cache_root = home / "cache" / "puppeteer"
    async with async_browser_cache_lock(cache_root):
        owner_record = cache_root / BROWSER_FETCH_OWNER_FILE
        if await run_blocking_safely(process_owner_is_live, owner_record):
            return False, "A prior browser fetch process is still active"
        owner_record.unlink(missing_ok=True)
        if await run_blocking_safely(
            _browser_artifact_ready, cache_root, expected
        ) and await run_blocking_safely(browser_cache_is_ready, cache_root, expected):
            return True, None
        env = os.environ.copy()
        env["PUPPETEER_CACHE_DIR"] = str(cache_root.resolve())
        result = await _run_owned_subprocess(
            [str(executable), "browsers", "install", "chrome"],
            cwd=scripts_dir,
            env=env,
            owner_record=owner_record,
        )
        if result.returncode != 0:
            warning = result.stderr.strip() or "Puppeteer browser fetch failed"
            return False, warning
        if not await run_blocking_safely(_browser_artifact_ready, cache_root, expected):
            return False, "Puppeteer reported success without a compatible browser artifact"
        await run_blocking_safely(write_browser_cache_readiness, cache_root, expected)
        return True, None


def _browser_expectation(scripts_dir: Path) -> BrowserCacheReadiness:
    package = scripts_dir / "node_modules" / "puppeteer" / "package.json"
    revisions = (
        scripts_dir / "node_modules" / "puppeteer-core" / "lib" / "puppeteer" / "revisions.js"
    )
    try:
        package_value = json.loads(package.read_text(encoding="utf-8"))
        version = package_value["version"]
        source = revisions.read_text(encoding="utf-8")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SkillMaterializationError("Cannot inspect local Puppeteer runtime") from exc
    match = _REVISION_PATTERN.search(source)
    if not isinstance(version, str) or not version or match is None:
        raise SkillMaterializationError("Cannot determine local Puppeteer compatibility")
    return BrowserCacheReadiness(sys.platform, version, match.group("build"), "chrome")


def _browser_artifact_ready(cache_root: Path, expected: BrowserCacheReadiness) -> bool:
    try:
        builds = [
            path
            for path in (cache_root / expected.channel).iterdir()
            if expected.browser_build in path.name and path.is_dir()
        ]
        return any(
            candidate.is_file() and os.access(candidate, os.X_OK)
            for build in builds
            for candidate in build.rglob("*")
        )
    except OSError:
        return False


async def _run_owned_subprocess(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    owner_record: Path,
) -> _CommandResult:
    read_fd, write_fd = os.pipe()
    wrapped = [sys.executable, "-c", _BARRIER_PROGRAM, str(read_fd), *command]
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *wrapped,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
        os.close(read_fd)
        read_fd = -1
        await run_blocking_safely(write_process_owner, owner_record, process.pid)
        os.write(write_fd, b"1")
        os.close(write_fd)
        write_fd = -1
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            await _terminate_process_group(process, communication)
            raise SkillMaterializationError(f"Timed out running {' '.join(command)}") from exc
        except asyncio.CancelledError:
            await _terminate_process_group(process, communication)
            raise
        return _CommandResult(
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    except BaseException:
        if process is not None and process.returncode is None:
            await _terminate_process_group(process, communication)
        raise
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if process is None or process.returncode is not None:
            owner_record.unlink(missing_ok=True)


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes]] | None,
) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    if communication is not None:
        await communication


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _join_warnings(*values: str | None) -> str | None:
    warnings = [value for value in values if value]
    return "; ".join(dict.fromkeys(warnings)) or None


def _live_stage_warning(path: Path) -> str:
    return f"Preserved active cache stage: {path.name}"


def _publication_checkpoint(_name: str) -> None:
    """Fault-injection seam for crash-atomic publication tests."""
