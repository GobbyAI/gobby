"""Crash-safe installation of Gobby's pinned Impeccable CLI runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from gobby.install.bin_freshness_promotion import stage_and_promote_binary_file
from gobby.paths import get_gobby_home
from gobby.skills.script_cache import (
    BrowserCacheReadiness,
    browser_cache_is_ready,
    browser_cache_lock,
    write_browser_cache_readiness,
)
from gobby.sync.jsonl_io import atomic_write_text, export_file_lock
from gobby.utils.dependency_requirements import (
    IMPECCABLE_NODE_MIN_VERSION,
    IMPECCABLE_RELEASE,
    is_native_windows,
)

logger = logging.getLogger(__name__)

PACKAGE_JSON = {
    "name": "gobby-managed-impeccable",
    "private": True,
    "dependencies": {IMPECCABLE_RELEASE.package: IMPECCABLE_RELEASE.version},
}

_INSTALL_TIMEOUT_SECONDS = 180.0
_FETCH_TIMEOUT_SECONDS = 180.0
_TERMINATE_GRACE_SECONDS = 5.0
_OWNER_FILE = ".gobby-process-owner.json"
_FETCH_OWNER_FILE = ".gobby-browser-fetch-owner.json"
_RETAINED_FILE = ".retained-generations.json"
_GENERATION_MARKER = "-generation-"
_REVISION_PATTERN = re.compile(r"chrome\s*:\s*['\"](?P<build>[^'\"]+)['\"]")
_VERSION_PATTERN = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+)$")
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


class ImpeccableInstallError(RuntimeError):
    """Managed Impeccable installation or verification failed."""


@dataclass(frozen=True)
class ImpeccableInstallResult:
    path: Path
    version: str
    installed: bool
    chrome_ready: bool


def impeccable_lockfile_path() -> Path:
    """Return the package-relative vendored lockfile path."""
    return Path(__file__).parents[1] / "install" / "impeccable-package-lock.json"


def verify_lockfile() -> str:
    """Verify and return the vendored lockfile digest."""
    path = impeccable_lockfile_path()
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ImpeccableInstallError(f"cannot read bundled Impeccable lockfile: {exc}") from exc
    if digest != IMPECCABLE_RELEASE.lockfile_sha256:
        raise ImpeccableInstallError("bundled Impeccable lockfile checksum mismatch")
    return digest


def install_impeccable_cli() -> ImpeccableInstallResult:
    """Install, repair, or reuse the pinned managed Impeccable CLI."""
    if is_native_windows():
        raise ImpeccableInstallError("managed Impeccable is unavailable on native Windows")
    node, node_version = _require_supported_node()
    npm = _find_npm()
    home = get_gobby_home()
    root = home / "tools" / "impeccable"
    pointer = root / IMPECCABLE_RELEASE.version
    root.mkdir(mode=0o700, parents=True, exist_ok=True)

    with export_file_lock(root / ".install"):
        _collect_abandoned_generations(root, pointer)
        generation = _verified_generation(pointer)
        if generation is not None:
            activation_repaired = _repair_activation_surfaces(home, pointer)
            chrome_ready, chrome_changed = _ensure_chrome(generation, home)
            return ImpeccableInstallResult(
                pointer,
                IMPECCABLE_RELEASE.version,
                installed=activation_repaired or chrome_changed,
                chrome_ready=chrome_ready,
            )

        generation = _install_generation(root, npm=npm, node=node, node_version=node_version)
        _activate_generation(root, pointer, generation)
        _publication_checkpoint("pointer_swapped")
        _publish_launcher(home, pointer)
        _publication_checkpoint("launcher_written")
        _publish_stamp(home)
        _publication_checkpoint("stamp_written")
        _ensure_path(home / "bin")
        chrome_ready, _ = _ensure_chrome(generation, home)
        verified = _verified_generation(pointer)
        if verified != generation:
            raise ImpeccableInstallError("published Impeccable generation failed verification")
        return ImpeccableInstallResult(
            pointer,
            IMPECCABLE_RELEASE.version,
            installed=True,
            chrome_ready=chrome_ready,
        )


def inspect_impeccable_installation() -> Path | None:
    """Return a fully verified installed pointer, or None when absent."""
    if is_native_windows():
        return None
    home = get_gobby_home()
    pointer = home / "tools" / "impeccable" / IMPECCABLE_RELEASE.version
    if not pointer.exists() and not pointer.is_symlink():
        return None
    generation = _verified_generation(pointer)
    if generation is None:
        raise ImpeccableInstallError("managed Impeccable installation is corrupt")
    if not _launcher_is_valid(home, pointer) or not _stamp_is_valid(home):
        raise ImpeccableInstallError("managed Impeccable activation is incomplete")
    return pointer


def _detect_node() -> tuple[Path, str] | None:
    raw = shutil.which("node")
    if not raw:
        return None
    node = Path(raw).resolve(strict=True)
    try:
        result = subprocess.run(
            [str(node), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_PATTERN.fullmatch(result.stdout.strip())
    if result.returncode != 0 or match is None:
        return None
    return node, match.group("version")


def _require_supported_node() -> tuple[Path, str]:
    detected = _detect_node()
    if detected is None:
        raise ImpeccableInstallError(
            f"Node {IMPECCABLE_NODE_MIN_VERSION} or newer is required for Impeccable"
        )
    node, version = detected
    if _version_tuple(version) < _version_tuple(IMPECCABLE_NODE_MIN_VERSION):
        raise ImpeccableInstallError(
            f"Node {version} is too old; Impeccable requires {IMPECCABLE_NODE_MIN_VERSION} or newer"
        )
    return node, version


def _find_npm() -> Path:
    raw = shutil.which("npm")
    if not raw:
        raise ImpeccableInstallError("npm is required to install managed Impeccable")
    return Path(raw).resolve(strict=True)


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ImpeccableInstallError(f"invalid Node version: {value}")
    major, minor, patch = (int(part) for part in match.group("version").split("."))
    return major, minor, patch


def _install_generation(
    root: Path,
    *,
    npm: Path,
    node: Path,
    node_version: str,
) -> Path:
    verify_lockfile()
    generation = root / (f"{IMPECCABLE_RELEASE.version}{_GENERATION_MARKER}{uuid.uuid4().hex}")
    generation.mkdir(mode=0o700)
    owner_record = generation / _OWNER_FILE
    try:
        _write_json(generation / "package.json", PACKAGE_JSON)
        shutil.copyfile(impeccable_lockfile_path(), generation / "package-lock.json")
        result = _run_owned_process(
            [
                str(npm),
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--omit=dev",
            ],
            cwd=generation,
            env=None,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            owner_record=owner_record,
        )
        if result.returncode != 0:
            raise ImpeccableInstallError(
                f"npm failed to install managed Impeccable: {result.stderr.strip()}"
            )
        _verify_generation_contents(generation)
        _write_json(
            generation / "receipt.json",
            IMPECCABLE_RELEASE.receipt_fields() | {"node": str(node), "node_version": node_version},
        )
        _fsync_tree(generation)
        _publication_checkpoint("generation_fsynced")
        return generation
    except BaseException:
        if not _owner_record_is_live(owner_record):
            shutil.rmtree(generation, ignore_errors=True)
        raise


def _activate_generation(root: Path, pointer: Path, generation: Path) -> None:
    retained = _read_retained_generations(root)
    outgoing = _resolved_generation(pointer, root)
    if outgoing is not None:
        retained.add(outgoing.name)
        _write_retained_generations(root, retained)
        _publication_checkpoint("retained_record_written")
    temporary = root / f".{pointer.name}-pointer-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(generation.name, target_is_directory=True)
        os.replace(temporary, pointer)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _collect_abandoned_generations(root: Path, pointer: Path) -> None:
    preserved = _read_retained_generations(root)
    current = _resolved_generation(pointer, root)
    if current is not None:
        preserved.add(current.name)
    prefix = f"{IMPECCABLE_RELEASE.version}{_GENERATION_MARKER}"
    for candidate in root.iterdir():
        if not candidate.is_dir() or not candidate.name.startswith(prefix):
            continue
        if candidate.name in preserved:
            continue
        owner = candidate / _OWNER_FILE
        if _owner_record_is_live(owner):
            logger.warning("Skipping live abandoned Impeccable generation %s", candidate)
            continue
        shutil.rmtree(candidate)


def _read_retained_generations(root: Path) -> set[str]:
    path = root / _RETAINED_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        generations = value["generations"]
        if not isinstance(generations, list) or not all(
            isinstance(item, str) for item in generations
        ):
            return set()
        return set(generations)
    except FileNotFoundError:
        return set()
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ImpeccableInstallError("retained Impeccable generation record is corrupt") from exc


def _write_retained_generations(root: Path, generations: set[str]) -> None:
    _write_json(root / _RETAINED_FILE, {"generations": sorted(generations)})
    _fsync_directory(root)


def _verified_generation(pointer: Path) -> Path | None:
    root = pointer.parent
    generation = _resolved_generation(pointer, root)
    if generation is None:
        return None
    try:
        _verify_generation_contents(generation)
        receipt = _read_json(generation / "receipt.json")
        if receipt.get("package") != IMPECCABLE_RELEASE.package:
            return None
        if receipt.get("version") != IMPECCABLE_RELEASE.version:
            return None
        if receipt.get("lockfile_sha256") != IMPECCABLE_RELEASE.lockfile_sha256:
            return None
    except (ImpeccableInstallError, OSError, TypeError, ValueError):
        return None
    return generation


def _resolved_generation(pointer: Path, root: Path) -> Path | None:
    if not pointer.is_symlink():
        return None
    try:
        resolved_root = root.resolve(strict=True)
        resolved = pointer.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    prefix = f"{IMPECCABLE_RELEASE.version}{_GENERATION_MARKER}"
    return resolved if resolved.name.startswith(prefix) else None


def _verify_generation_contents(generation: Path) -> None:
    package = _read_json(generation / "node_modules" / "impeccable" / "package.json")
    if package.get("name") != IMPECCABLE_RELEASE.package:
        raise ImpeccableInstallError("npm installed an unexpected package")
    if package.get("version") != IMPECCABLE_RELEASE.version:
        raise ImpeccableInstallError("npm installed an unexpected Impeccable version")
    _verified_bin(generation, package_name="impeccable", binary_name="impeccable")
    puppeteer = _read_json(generation / "node_modules" / "puppeteer" / "package.json")
    locked_version = _locked_package_version("puppeteer")
    if puppeteer.get("version") != locked_version:
        raise ImpeccableInstallError("installed Puppeteer diverges from the lockfile")
    _verified_bin(generation, package_name="puppeteer", binary_name="puppeteer")


def _verified_bin(generation: Path, *, package_name: str, binary_name: str) -> Path:
    package_root = generation / "node_modules" / package_name
    manifest = _read_json(package_root / "package.json")
    declared = manifest.get("bin")
    if isinstance(declared, str):
        target_value = declared
    elif isinstance(declared, dict) and isinstance(declared.get(binary_name), str):
        target_value = declared[binary_name]
    else:
        raise ImpeccableInstallError(f"{package_name} has no {binary_name} executable")
    executable = generation / "node_modules" / ".bin" / binary_name
    try:
        resolved_generation = generation.resolve(strict=True)
        resolved_executable = executable.resolve(strict=True)
        resolved_executable.relative_to(resolved_generation)
        expected = (package_root / target_value).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ImpeccableInstallError(f"invalid {binary_name} executable") from exc
    if (
        resolved_executable != expected
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ImpeccableInstallError(f"invalid {binary_name} executable identity")
    return executable


def _locked_package_version(package_name: str) -> str:
    lock = _read_json(impeccable_lockfile_path())
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ImpeccableInstallError("bundled lockfile has no package graph")
    package = packages.get(f"node_modules/{package_name}")
    if not isinstance(package, dict):
        raise ImpeccableInstallError(f"bundled lockfile has no {package_name} version")
    version = package.get("version")
    if not isinstance(version, str):
        raise ImpeccableInstallError(f"bundled lockfile has no {package_name} version")
    return version


def _repair_activation_surfaces(home: Path, pointer: Path) -> bool:
    repaired = False
    if not _launcher_is_valid(home, pointer):
        _publish_launcher(home, pointer)
        repaired = True
    if not _stamp_is_valid(home):
        _publish_stamp(home)
        repaired = True
    _ensure_path(home / "bin")
    return repaired


def _launcher_content(home: Path, pointer: Path) -> str:
    cache = shlex.quote(str(home / "cache" / "puppeteer"))
    executable = shlex.quote(str(pointer / "node_modules" / ".bin" / "impeccable"))
    return f'#!/bin/sh\nexport PUPPETEER_CACHE_DIR={cache}\nexec {executable} "$@"\n'


def _launcher_is_valid(home: Path, pointer: Path) -> bool:
    launcher = home / "bin" / "impeccable"
    try:
        return (
            launcher.is_file()
            and not launcher.is_symlink()
            and launcher.read_text(encoding="utf-8") == _launcher_content(home, pointer)
            and os.access(launcher, os.X_OK)
        )
    except OSError:
        return False


def _publish_launcher(home: Path, pointer: Path) -> None:
    bin_dir = home / "bin"
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_source = tempfile.mkstemp(prefix=".impeccable-launcher-", dir=pointer.parent)
    source = Path(raw_source)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_launcher_content(home, pointer))
            handle.flush()
            os.fsync(handle.fileno())
        source.chmod(0o755)
        stage_and_promote_binary_file(source, destination=bin_dir / "impeccable")
        _fsync_directory(bin_dir)
    finally:
        source.unlink(missing_ok=True)


def _stamp_is_valid(home: Path) -> bool:
    try:
        return (home / "bin" / ".impeccable-version").read_text(
            encoding="utf-8"
        ).strip() == IMPECCABLE_RELEASE.version
    except OSError:
        return False


def _publish_stamp(home: Path) -> None:
    path = home / "bin" / ".impeccable-version"
    atomic_write_text(path, f"{IMPECCABLE_RELEASE.version}\n")
    _fsync_directory(path.parent)


def _ensure_path(bin_dir: Path) -> None:
    from gobby.cli.install_setup import _ensure_gobby_bin_on_path

    _ensure_gobby_bin_on_path(bin_dir)


def _browser_readiness(generation: Path) -> BrowserCacheReadiness:
    puppeteer_version = _locked_package_version("puppeteer")
    revisions = (
        generation
        / "node_modules"
        / "puppeteer-core"
        / "lib"
        / "puppeteer"
        / "revisions.js"
    )
    try:
        source = revisions.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImpeccableInstallError("cannot read Puppeteer browser revision") from exc
    match = _REVISION_PATTERN.search(source)
    if match is None:
        raise ImpeccableInstallError("cannot determine Puppeteer's Chrome build")
    return BrowserCacheReadiness(
        platform=sys.platform,
        puppeteer_version=puppeteer_version,
        browser_build=match.group("build"),
        channel="chrome",
    )


def _browser_artifact_is_ready(cache_root: Path, expected: BrowserCacheReadiness) -> bool:
    chrome_root = cache_root / expected.channel
    try:
        return any(expected.browser_build in path.name for path in chrome_root.iterdir())
    except OSError:
        return False


def _ensure_chrome(generation: Path, home: Path) -> tuple[bool, bool]:
    cache_root = home / "cache" / "puppeteer"
    expected = _browser_readiness(generation)
    with browser_cache_lock(cache_root):
        fetch_owner = cache_root / _FETCH_OWNER_FILE
        if _owner_record_is_live(fetch_owner):
            logger.warning("Skipping Impeccable Chrome fetch while its prior writer is live")
            return False, False
        fetch_owner.unlink(missing_ok=True)
        if browser_cache_is_ready(cache_root, expected) and _browser_artifact_is_ready(
            cache_root, expected
        ):
            return True, False
        try:
            puppeteer = _verified_bin(
                generation,
                package_name="puppeteer",
                binary_name="puppeteer",
            )
        except ImpeccableInstallError as exc:
            logger.warning("Managed Impeccable Chrome fetch skipped: %s", exc)
            return False, True
        env = os.environ.copy()
        env["PUPPETEER_CACHE_DIR"] = str(cache_root)
        result = _run_owned_process(
            [str(puppeteer), "browsers", "install", "chrome"],
            cwd=generation,
            env=env,
            timeout=_FETCH_TIMEOUT_SECONDS,
            owner_record=fetch_owner,
        )
        if result.returncode != 0 or not _browser_artifact_is_ready(cache_root, expected):
            logger.warning("Managed Impeccable Chrome fetch failed: %s", result.stderr.strip())
            return False, True
        write_browser_cache_readiness(cache_root, expected)
        return True, True


def _run_owned_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout: float,
    owner_record: Path,
) -> subprocess.CompletedProcess[str]:
    read_fd, write_fd = os.pipe()
    wrapped = [sys.executable, "-c", _BARRIER_PROGRAM, str(read_fd), *command]
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            wrapped,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
        os.close(read_fd)
        read_fd = -1
        identity = _process_start_identity(process.pid)
        if identity is None:
            raise ImpeccableInstallError("cannot record installer process identity")
        _write_json(owner_record, {"pgid": process.pid, "leader_start": identity})
        _fsync_directory(owner_record.parent)
        os.write(write_fd, b"1")
        os.close(write_fd)
        write_fd = -1
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise ImpeccableInstallError(f"timed out running {' '.join(command)}") from exc
        except BaseException:
            _terminate_process_group(process)
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        raise
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if process is None or process.poll() is not None:
            owner_record.unlink(missing_ok=True)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def _process_start_identity(pid: int) -> str | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        fields = proc_stat.read_text(encoding="utf-8").split()
        return fields[21]
    except (IndexError, OSError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identity = result.stdout.strip()
    return identity or None


def _owner_record_is_live(path: Path) -> bool:
    try:
        value = _read_json(path)
        pgid = value["pgid"]
        recorded_identity = value["leader_start"]
        if not isinstance(pgid, int) or not isinstance(recorded_identity, str):
            return False
    except (KeyError, OSError, TypeError, ValueError, ImpeccableInstallError):
        return False
    current_identity = _process_start_identity(pgid)
    if current_identity is not None and current_identity != recorded_identity:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImpeccableInstallError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise ImpeccableInstallError(f"invalid {path.name}")
    return value


def _write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise ImpeccableInstallError(f"cannot fsync {path}") from exc
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in reversed(directories):
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ImpeccableInstallError(f"cannot fsync directory {path}") from exc


def _publication_checkpoint(_name: str) -> None:
    """Injection seam for crash-safety tests."""
