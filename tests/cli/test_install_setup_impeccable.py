from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import install_setup_impeccable as installer
from gobby.cli.install_setup_rtk import RtkCleanupReport
from gobby.cli.uninstall import uninstall
from gobby.skills.script_cache import (
    DELETION_TOMBSTONE_PREFIX,
    PROCESS_OWNER_FILE,
    BrowserCacheReadiness,
    SkillScriptsOwner,
    browser_cache_is_ready,
    read_browser_cache_readiness,
    skill_scripts_namespace_lock_target,
    skill_scripts_root_lock_target,
    stage_name,
    write_browser_cache_readiness,
    write_process_owner,
    write_skill_scripts_owner,
)
from gobby.sync.jsonl_io import export_file_lock
from gobby.utils.dependency_requirements import (
    IMPECCABLE_NODE_MIN_VERSION,
    IMPECCABLE_RELEASE,
    DependencyStatus,
    collect_dependency_report,
    required_dependency_errors,
)

_BROWSER_BUILD = "139.0.7258.66"
_PUPPETEER_VERSION = "25.5.0"


def _write_executable(path: Path, content: str = "#!/usr/bin/env node\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def impeccable_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, list[list[str]]]:
    home = tmp_path / "configured-gobby-home"
    calls: list[list[str]] = []
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(
        installer,
        "_detect_node",
        lambda: (Path("/managed/node"), IMPECCABLE_NODE_MIN_VERSION),
    )
    monkeypatch.setattr(installer, "_find_npm", lambda: Path("/managed/npm"))
    monkeypatch.setattr(installer, "_ensure_path", lambda bin_dir: None)

    def fake_owned_process(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout: float,
        owner_record: Path,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, owner_record
        calls.append(command)
        if command[1] == "ci":
            impeccable = cwd / "node_modules" / "impeccable"
            _write_executable(impeccable / "cli" / "bin" / "cli.js")
            (impeccable / "package.json").write_text(
                json.dumps(
                    {
                        "name": "impeccable",
                        "version": "3.5.0",
                        "bin": {"impeccable": "cli/bin/cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            puppeteer = cwd / "node_modules" / "puppeteer"
            _write_executable(puppeteer / "lib" / "puppeteer" / "node" / "cli.js")
            (puppeteer / "package.json").write_text(
                json.dumps(
                    {
                        "name": "puppeteer",
                        "version": _PUPPETEER_VERSION,
                        "bin": {"puppeteer": "lib/puppeteer/node/cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            core = cwd / "node_modules" / "puppeteer-core"
            revisions = core / "lib" / "puppeteer" / "revisions.js"
            revisions.parent.mkdir(parents=True, exist_ok=True)
            revisions.write_text(
                f"exports.PUPPETEER_REVISIONS = {{chrome: '{_BROWSER_BUILD}'}};\n",
                encoding="utf-8",
            )
            (core / "package.json").write_text(
                json.dumps({"name": "puppeteer-core", "version": _PUPPETEER_VERSION}),
                encoding="utf-8",
            )
            bin_dir = cwd / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "impeccable").symlink_to("../impeccable/cli/bin/cli.js")
            (bin_dir / "puppeteer").symlink_to("../puppeteer/lib/puppeteer/node/cli.js")
        else:
            assert command[-3:] == ["browsers", "install", "chrome"]
            assert env is not None
            cache_root = Path(env["PUPPETEER_CACHE_DIR"])
            (cache_root / "chrome" / f"mac_arm-{_BROWSER_BUILD}").mkdir(
                parents=True,
                exist_ok=True,
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer, "_run_owned_process", fake_owned_process)
    return home, calls


def test_release_pin_and_node_floor() -> None:
    assert IMPECCABLE_RELEASE.package == "impeccable"
    assert IMPECCABLE_RELEASE.version == "3.5.0"
    assert IMPECCABLE_NODE_MIN_VERSION == "22.12.0"
    lockfile = Path(installer.__file__).parents[1] / "install" / "impeccable-package-lock.json"
    assert hashlib.sha256(lockfile.read_bytes()).hexdigest() == IMPECCABLE_RELEASE.lockfile_sha256
    assert installer.PACKAGE_JSON == {
        "name": "gobby-managed-impeccable",
        "private": True,
        "dependencies": {"impeccable": "3.5.0"},
    }


def test_chrome_fetch_uses_compatibility_keyed_readiness(
    impeccable_runtime: tuple[Path, list[list[str]]],
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "puppeteer"
    expected = BrowserCacheReadiness(
        platform="linux",
        puppeteer_version="24.16.0",
        browser_build="139.0.7258.66",
        channel="chrome",
    )

    assert browser_cache_is_ready(cache_root, expected) is False
    write_browser_cache_readiness(cache_root, expected)

    assert read_browser_cache_readiness(cache_root) == expected
    assert browser_cache_is_ready(cache_root, expected) is True
    assert (
        browser_cache_is_ready(
            cache_root,
            BrowserCacheReadiness(
                platform="linux",
                puppeteer_version="24.16.0",
                browser_build="140.0.0.0",
                channel="chrome",
            ),
        )
        is False
    )

    home, calls = impeccable_runtime
    installer.install_impeccable_cli()
    cache_root = home / "cache" / "puppeteer"
    fetches = sum(call[-3:] == ["browsers", "install", "chrome"] for call in calls)
    for changed in (
        BrowserCacheReadiness("other", _PUPPETEER_VERSION, _BROWSER_BUILD, "chrome"),
        BrowserCacheReadiness(sys.platform, "0.0.0", _BROWSER_BUILD, "chrome"),
        BrowserCacheReadiness(sys.platform, _PUPPETEER_VERSION, "0.0.0.0", "chrome"),
        BrowserCacheReadiness(sys.platform, _PUPPETEER_VERSION, _BROWSER_BUILD, "other"),
    ):
        write_browser_cache_readiness(cache_root, changed)
        installer.install_impeccable_cli()
        current = sum(call[-3:] == ["browsers", "install", "chrome"] for call in calls)
        assert current == fetches + 1
        fetches = current


def test_install_uses_locked_ci_without_scripts(
    impeccable_runtime: tuple[Path, list[list[str]]],
) -> None:
    home, calls = impeccable_runtime

    result = installer.install_impeccable_cli()

    assert result.installed is True
    assert calls[0] == [
        "/managed/npm",
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--omit=dev",
    ]
    assert calls[1][0].endswith("node_modules/.bin/puppeteer")
    pointer = home / "tools" / "impeccable" / "3.5.0"
    assert pointer.is_symlink()
    receipt = json.loads((pointer / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["package"] == "impeccable"
    assert receipt["version"] == "3.5.0"
    launcher = home / "bin" / "impeccable"
    assert os.access(launcher, os.X_OK)
    assert str(pointer / "node_modules" / ".bin" / "impeccable") in launcher.read_text()
    assert (home / "bin" / ".impeccable-version").read_text().strip() == "3.5.0"


def test_second_install_is_noop(
    impeccable_runtime: tuple[Path, list[list[str]]],
) -> None:
    _, calls = impeccable_runtime
    installer.install_impeccable_cli()
    first_calls = list(calls)

    result = installer.install_impeccable_cli()

    assert result.installed is False
    assert calls == first_calls


def test_first_install_accepts_canonicalized_generation_path(
    impeccable_runtime: tuple[Path, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del impeccable_runtime
    canonical_parent = tmp_path / "canonical-parent"
    canonical_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(canonical_parent, target_is_directory=True)
    monkeypatch.setenv("GOBBY_HOME", str(alias_parent / "gobby-home"))

    result = installer.install_impeccable_cli()

    assert result.installed is True
    assert result.chrome_ready is True


@pytest.mark.parametrize(
    "artifact",
    [
        "package",
        "launcher",
        "stamp",
        "impeccable-wrong-file",
        "impeccable-wrong-target",
        "impeccable-escape",
        "puppeteer-wrong-file",
        "puppeteer-wrong-target",
        "puppeteer-escape",
        "puppeteer-version",
        "chrome-readiness",
    ],
)
def test_repairs_partial_corruption(
    impeccable_runtime: tuple[Path, list[list[str]]],
    tmp_path: Path,
    artifact: str,
) -> None:
    home, calls = impeccable_runtime
    installer.install_impeccable_cli()
    pointer = home / "tools" / "impeccable" / "3.5.0"
    bin_name = "puppeteer" if artifact.startswith("puppeteer-") else "impeccable"
    bin_path = pointer / "node_modules" / ".bin" / bin_name
    if artifact == "package":
        (pointer / "node_modules" / "impeccable" / "package.json").unlink()
    elif artifact == "launcher":
        (home / "bin" / "impeccable").unlink()
    elif artifact == "stamp":
        (home / "bin" / ".impeccable-version").write_text("wrong\n", encoding="utf-8")
    elif artifact.endswith("wrong-file"):
        bin_path.unlink()
        _write_executable(bin_path)
    elif artifact.endswith("wrong-target"):
        bin_path.unlink()
        wrong = pointer / "node_modules" / bin_name / "wrong.js"
        _write_executable(wrong)
        bin_path.symlink_to(wrong)
    elif artifact.endswith("escape"):
        bin_path.unlink()
        outside = tmp_path / f"outside-{bin_name}"
        _write_executable(outside)
        bin_path.symlink_to(outside)
    elif artifact == "puppeteer-version":
        manifest = pointer / "node_modules" / "puppeteer" / "package.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["version"] = "0.0.0"
        manifest.write_text(json.dumps(value), encoding="utf-8")
    else:
        (home / "cache" / "puppeteer" / ".gobby-browser-ready.json").unlink()
    npm_calls_before = sum(call[1] == "ci" for call in calls)

    result = installer.install_impeccable_cli()

    assert result.installed is True
    npm_calls_after = sum(call[1] == "ci" for call in calls)
    expected_delta = 0 if artifact in {"launcher", "stamp", "chrome-readiness"} else 1
    assert npm_calls_after - npm_calls_before == expected_delta


def test_node_floor_gate(
    impeccable_runtime: tuple[Path, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, _ = impeccable_runtime
    monkeypatch.setattr(installer, "_detect_node", lambda: (Path("/node"), "22.11.9"))
    with pytest.raises(installer.ImpeccableInstallError, match="22.12.0"):
        installer.install_impeccable_cli()
    assert not (home / "tools" / "impeccable").exists()

    monkeypatch.setattr(installer, "_detect_node", lambda: (Path("/node"), "22.12.0"))
    installer.install_impeccable_cli()
    before = sorted(path.relative_to(home) for path in home.rglob("*"))
    monkeypatch.setattr(installer, "_detect_node", lambda: (Path("/node"), "22.11.9"))
    with pytest.raises(installer.ImpeccableInstallError, match="22.12.0"):
        installer.install_impeccable_cli()
    assert sorted(path.relative_to(home) for path in home.rglob("*")) == before


def test_gobby_home_override_consistency(
    impeccable_runtime: tuple[Path, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home, _ = impeccable_runtime
    literal_home = tmp_path / "literal-user-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: literal_home))

    installer.install_impeccable_cli()

    assert (home / "tools" / "impeccable" / "3.5.0").is_symlink()
    assert (home / "bin" / "impeccable").exists()
    assert (home / "cache" / "puppeteer").exists()
    assert not (literal_home / ".gobby").exists()


def test_preflight_fails_without_impeccable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.utils.dependency_requirements.impeccable_dependency_status",
        lambda: DependencyStatus("missing", None, None, IMPECCABLE_RELEASE.version, None, "repair"),
    )
    monkeypatch.setattr("gobby.utils.dependency_requirements.requires_tmux", lambda: False)
    monkeypatch.setattr(
        "gobby.utils.dependency_requirements._command_status",
        lambda **kwargs: DependencyStatus("healthy", "1.0.0", None, None, "/bin", None),
    )
    monkeypatch.setattr(
        "gobby.utils.dependency_requirements.node_dependency_status",
        lambda: DependencyStatus("healthy", "22.18.0", "20.11.0", None, "/node", None),
    )

    report = collect_dependency_report(managed_services=False, include_srt=False)

    assert report.required["impeccable"].state == "missing"
    assert required_dependency_errors(report) == ["repair"]


def test_packaged_lockfile_is_shippable_and_resolvable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = Path(installer.__file__).parents[3]
    metadata = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = metadata["tool"]["setuptools"]["package-data"]["gobby"]
    real_lockfile = installer.impeccable_lockfile_path()
    assert "install/impeccable-package-lock.json" in patterns

    package = tmp_path / "installed" / "gobby"
    synthetic_module = package / "cli" / "install_setup_impeccable.py"
    synthetic_module.parent.mkdir(parents=True)
    synthetic_module.touch()
    synthetic_lockfile = package / "install" / "impeccable-package-lock.json"
    synthetic_lockfile.parent.mkdir()
    synthetic_lockfile.write_bytes(real_lockfile.read_bytes())
    monkeypatch.setattr(installer, "__file__", str(synthetic_module))

    assert installer.impeccable_lockfile_path() == synthetic_lockfile
    assert installer.verify_lockfile() == IMPECCABLE_RELEASE.lockfile_sha256


def test_native_win32_is_gated_and_reported(
    impeccable_runtime: tuple[Path, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, calls = impeccable_runtime
    monkeypatch.setattr(installer, "is_native_windows", lambda: True)

    with pytest.raises(installer.ImpeccableInstallError, match="native Windows"):
        installer.install_impeccable_cli()

    assert calls == []
    assert not home.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_timeout_reaps_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "late-write"
    child_code = (
        f"import pathlib,time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )

    with pytest.raises(installer.ImpeccableInstallError, match="timed out"):
        installer._run_owned_process(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env=None,
            timeout=0.1,
            owner_record=tmp_path / "owner.json",
        )

    threading.Event().wait(1.0)
    assert not marker.exists()
    assert not (tmp_path / "owner.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_owner_record_precedes_child_writes(tmp_path: Path) -> None:
    marker = tmp_path / "child-write"
    owner = tmp_path / "owner.json"
    pid = os.fork()
    if pid == 0:
        installer.__dict__["_write_process_owner"] = lambda path, pgid: os._exit(91)
        installer._run_owned_process(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
            ],
            cwd=tmp_path,
            env=None,
            timeout=5,
            owner_record=owner,
        )
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 91
    threading.Event().wait(0.2)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_orphaned_group_blocks_stage_collection(tmp_path: Path) -> None:
    root = tmp_path / "impeccable"
    root.mkdir()
    stage = root / "3.5.0-generation-orphan"
    stage.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        write_process_owner(stage / installer._OWNER_FILE, process.pid)

        installer._collect_abandoned_generations(root, root / "3.5.0")
        assert stage.exists()

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(installer, "process_owner_is_live", lambda path: False)
            installer._collect_abandoned_generations(root, root / "3.5.0")
        assert not stage.exists()
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX atomic-pointer contract")
@pytest.mark.parametrize(
    "checkpoint",
    ["generation_fsynced", "pointer_swapped", "launcher_written"],
)
def test_publication_survives_kill_at_every_point(
    impeccable_runtime: tuple[Path, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    checkpoint: str,
) -> None:
    del impeccable_runtime
    home = tmp_path / checkpoint
    monkeypatch.setenv("GOBBY_HOME", str(home))
    pid = os.fork()
    if pid == 0:

        def crash_at_checkpoint(_name: str) -> None:
            if _name == checkpoint:
                os._exit(92)

        installer._publication_checkpoint = crash_at_checkpoint
        installer.install_impeccable_cli()
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 92
    pointer = home / "tools" / "impeccable" / "3.5.0"
    if checkpoint == "generation_fsynced":
        assert not pointer.exists()
        assert not (home / "bin" / "impeccable").exists()
        assert not (home / "bin" / ".impeccable-version").exists()
    else:
        assert pointer.is_symlink()

    result = installer.install_impeccable_cli()

    assert result.path.is_symlink()
    assert (home / "bin" / "impeccable").exists()
    assert (home / "bin" / ".impeccable-version").exists()
    generations = list((home / "tools" / "impeccable").glob("3.5.0-generation-*"))
    assert len(generations) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX atomic-pointer contract")
def test_repair_crash_preserves_previous_generation(
    impeccable_runtime: tuple[Path, list[list[str]]],
) -> None:
    home, _ = impeccable_runtime
    installer.install_impeccable_cli()
    pointer = home / "tools" / "impeccable" / "3.5.0"
    previous = pointer.resolve()
    package = previous / "node_modules" / "impeccable" / "package.json"
    value = json.loads(package.read_text(encoding="utf-8"))
    value["version"] = "0.0.0"
    package.write_text(json.dumps(value), encoding="utf-8")

    pid = os.fork()
    if pid == 0:

        def crash_after_retention(_name: str) -> None:
            if _name == "retained_record_written":
                os._exit(93)

        installer._publication_checkpoint = crash_after_retention
        installer.install_impeccable_cli()
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 93
    assert pointer.resolve() == previous
    assert previous.exists()

    installer.install_impeccable_cli()

    assert pointer.resolve() != previous
    assert previous.exists()
    retained = json.loads((pointer.parent / installer._RETAINED_FILE).read_text(encoding="utf-8"))
    assert previous.name in retained["generations"]


def test_remove_impeccable_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "configured-home"
    monkeypatch.setattr(installer, "get_gobby_home", lambda: home)
    tool_root = home / "tools" / "impeccable"
    launcher = home / "bin" / "impeccable"
    stamp = home / "bin" / ".impeccable-version"
    owned_root = home / "cache" / "skill-scripts" / "65eb3be0-7f8d-4b0f-b679-4e162bd90aac"
    other_root = home / "cache" / "skill-scripts" / "other-skill-id"
    unmarked_root = home / "cache" / "skill-scripts" / "unmarked-id"
    malformed_root = home / "cache" / "skill-scripts" / "malformed-id"
    symlink_root = home / "cache" / "skill-scripts" / "symlink-id"
    symlink_target = tmp_path / "outside-cache"
    browser_cache = home / "cache" / "puppeteer"
    literal_home_state = tmp_path / "literal-home" / ".gobby" / "tools" / "impeccable"
    for path in (
        tool_root,
        owned_root,
        other_root,
        unmarked_root,
        malformed_root,
        symlink_target,
        browser_cache,
        literal_home_state,
    ):
        path.mkdir(parents=True)
    symlink_root.symlink_to(symlink_target, target_is_directory=True)
    (malformed_root / "owner.json").write_text("not-json", encoding="utf-8")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("launcher", encoding="utf-8")
    stamp.write_text("3.5.0\n", encoding="utf-8")
    write_skill_scripts_owner(
        owned_root,
        SkillScriptsOwner(owned_root.name, "impeccable", "bundled"),
    )
    write_skill_scripts_owner(
        other_root,
        SkillScriptsOwner("other-skill-id", "another-skill", "bundled"),
    )

    result = installer.remove_impeccable_runtime()

    assert result.removed == (tool_root, launcher, stamp, owned_root)
    assert len(result.skipped) == 3
    assert any("unowned" in warning and str(unmarked_root) in warning for warning in result.skipped)
    assert any(
        "unowned" in warning and str(malformed_root) in warning for warning in result.skipped
    )
    assert any("symlink" in warning and str(symlink_root) in warning for warning in result.skipped)
    assert not tool_root.exists()
    assert not launcher.exists()
    assert not stamp.exists()
    assert not owned_root.exists()
    assert other_root.exists()
    assert unmarked_root.exists()
    assert malformed_root.exists()
    assert symlink_root.is_symlink()
    assert symlink_target.exists()
    assert browser_cache.exists()
    assert literal_home_state.exists()


def test_native_win32_removes_degraded_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = home / "cache" / "skill-scripts" / "win32-impeccable-id"
    root.mkdir(parents=True)
    write_skill_scripts_owner(
        root,
        SkillScriptsOwner(root.name, "impeccable", "bundled"),
    )
    monkeypatch.setattr(installer, "get_gobby_home", lambda: home)
    monkeypatch.setattr(installer, "is_native_windows", lambda: True)

    result = installer.remove_impeccable_runtime()

    assert result.removed == (root,)
    assert not root.exists()
    assert installer.remove_impeccable_runtime().removed == ()


def test_crash_atomic_removal_recovers_tombstone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    namespace = home / "cache" / "skill-scripts"
    root = namespace / "crash-impeccable-id"
    root.mkdir(parents=True)
    write_skill_scripts_owner(root, SkillScriptsOwner(root.name, "impeccable", "bundled"))
    monkeypatch.setattr(installer, "get_gobby_home", lambda: home)

    def crash(name: str) -> None:
        assert name == "root_tombstoned"
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "_removal_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        installer.remove_impeccable_runtime()

    tombstones = [
        path for path in namespace.iterdir() if path.name.startswith(DELETION_TOMBSTONE_PREFIX)
    ]
    assert not root.exists()
    assert len(tombstones) == 1
    assert (tombstones[0] / "owner.json").is_file()

    monkeypatch.setattr(installer, "_removal_checkpoint", lambda _name: None)
    result = installer.remove_impeccable_runtime()

    assert result.removed == (tombstones[0],)
    assert not tombstones[0].exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
@pytest.mark.parametrize("writer_kind", ["installer", "materializer"])
def test_remove_waits_for_orphaned_writer_group(
    writer_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    if writer_kind == "installer":
        root = home / "tools" / "impeccable"
        stage = root / "3.5.0-generation-live"
    else:
        root = home / "cache" / "skill-scripts" / "live-impeccable-id"
        stage = root / ".gobby-stage-deps-live"
    stage.mkdir(parents=True)
    if writer_kind == "materializer":
        write_skill_scripts_owner(root, SkillScriptsOwner(root.name, "impeccable", "bundled"))
    monkeypatch.setattr(installer, "get_gobby_home", lambda: home)
    pid_file = tmp_path / "child.pid"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,signal,subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c','import signal;signal.pause()'],"
                "start_new_session=True);"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
                "signal.pause()"
            ),
            str(pid_file),
        ]
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        write_process_owner(stage / PROCESS_OWNER_FILE, child_pid)
        parent.kill()
        parent.wait(timeout=5)

        first = installer.remove_impeccable_runtime()

        assert root.exists()
        assert any("live writer" in warning for warning in first.skipped)
        os.killpg(child_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(child_pid, 0)
            except ProcessLookupError:
                break
            threading.Event().wait(0.01)

        second = installer.remove_impeccable_runtime()

        assert root in second.removed
        assert not root.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_install_and_uninstall_are_linearized(
    impeccable_runtime: tuple[Path, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, _ = impeccable_runtime
    entered = threading.Event()
    release = threading.Event()
    uninstall_started = threading.Event()
    uninstall_done = threading.Event()
    errors: list[BaseException] = []
    original = installer._run_owned_process

    def blocked_process(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout: float,
        owner_record: Path,
    ) -> subprocess.CompletedProcess[str]:
        if command[1] == "ci":
            entered.set()
            assert release.wait(timeout=5)
        return original(command, cwd=cwd, env=env, timeout=timeout, owner_record=owner_record)

    def run(function: object) -> None:
        try:
            assert callable(function)
            function()
        except BaseException as exc:
            errors.append(exc)

    def run_uninstall() -> None:
        uninstall_started.set()
        run(installer.remove_impeccable_runtime)
        uninstall_done.set()

    monkeypatch.setattr(installer, "_run_owned_process", blocked_process)
    installing = threading.Thread(target=run, args=(installer.install_impeccable_cli,))
    installing.start()
    assert entered.wait(timeout=5)
    uninstalling = threading.Thread(target=run_uninstall)
    uninstalling.start()
    assert uninstall_started.wait(timeout=5)
    assert not uninstall_done.wait(timeout=0.05)
    release.set()
    installing.join(timeout=5)
    uninstalling.join(timeout=5)

    assert not errors
    assert not installing.is_alive()
    assert not uninstalling.is_alive()
    assert not (home / "tools" / "impeccable").exists()


def test_cold_materializer_and_uninstall_are_linearized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    namespace = home / "cache" / "skill-scripts"
    root = namespace / "collision-fallback-id"
    entered = threading.Event()
    release = threading.Event()
    uninstall_started = threading.Event()
    uninstall_done = threading.Event()
    errors: list[BaseException] = []
    monkeypatch.setattr(installer, "get_gobby_home", lambda: home)

    def publish() -> None:
        try:
            with export_file_lock(skill_scripts_namespace_lock_target(namespace)):
                entered.set()
                assert release.wait(timeout=5)
                stage = namespace / stage_name("root", "cold-publisher")
                stage.mkdir()
                write_skill_scripts_owner(
                    stage,
                    SkillScriptsOwner(root.name, "impeccable", "bundled"),
                )
                os.replace(stage, root)
        except BaseException as exc:
            errors.append(exc)

    result: list[installer.ImpeccableRemovalResult] = []

    def uninstall_runtime() -> None:
        uninstall_started.set()
        result.append(installer.remove_impeccable_runtime())
        uninstall_done.set()

    publishing = threading.Thread(target=publish)
    publishing.start()
    assert entered.wait(timeout=5)
    uninstalling = threading.Thread(target=uninstall_runtime)
    uninstalling.start()
    assert uninstall_started.wait(timeout=5)
    assert not uninstall_done.wait(timeout=0.05)
    release.set()
    publishing.join(timeout=5)
    uninstalling.join(timeout=5)

    assert not errors
    assert result and root in result[0].removed
    assert not root.exists()


def test_warm_waiter_restarts_after_uninstall_deletes_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    namespace = home / "cache" / "skill-scripts"
    root = namespace / "warm-impeccable-id"
    root.mkdir(parents=True)
    write_skill_scripts_owner(root, SkillScriptsOwner(root.name, "impeccable", "bundled"))
    acquired = threading.Event()
    release = threading.Event()
    producer_done = threading.Event()
    monkeypatch.setattr(installer, "get_gobby_home", lambda: home)

    def block_after_root_lock(candidate: Path) -> list[Path]:
        if candidate == root:
            acquired.set()
            assert release.wait(timeout=5)
        return []

    def warm_publish() -> None:
        while True:
            with export_file_lock(skill_scripts_root_lock_target(root)):
                if root.exists():
                    (root / "warm-generation").mkdir()
                    producer_done.set()
                    return
            with export_file_lock(skill_scripts_namespace_lock_target(namespace)):
                if root.exists():
                    continue
                stage = namespace / stage_name("root", "warm-restart")
                stage.mkdir()
                write_skill_scripts_owner(
                    stage,
                    SkillScriptsOwner(root.name, "impeccable", "bundled"),
                )
                (stage / "warm-generation").mkdir()
                os.replace(stage, root)
                producer_done.set()
                return

    monkeypatch.setattr(installer, "live_process_owner_records", block_after_root_lock)
    uninstalling = threading.Thread(target=installer.remove_impeccable_runtime)
    uninstalling.start()
    assert acquired.wait(timeout=5)
    publishing = threading.Thread(target=warm_publish)
    publishing.start()
    assert not producer_done.wait(timeout=0.05)
    release.set()
    uninstalling.join(timeout=5)
    publishing.join(timeout=5)

    assert producer_done.is_set()
    assert root.is_dir()
    assert (root / "warm-generation").is_dir()


@pytest.mark.parametrize(
    ("arguments", "hook_removed", "cleanup_called"),
    [
        ([], True, True),
        (["impeccable"], False, True),
        (["rtk", "impeccable"], False, True),
        (["impeccable", "claude"], True, True),
        (["claude"], True, False),
    ],
)
def test_uninstall_component_matrix(
    arguments: list[str],
    hook_removed: bool,
    cleanup_called: bool,
    tmp_path: Path,
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}", encoding="utf-8")
    cleanup = installer.ImpeccableRemovalResult((), ())
    remove_claude = MagicMock(
        return_value={"success": True, "hooks_removed": [], "files_removed": []}
    )
    with (
        patch("gobby.cli.uninstall.Path.home", return_value=tmp_path),
        patch("gobby.cli.uninstall.get_cli_runtime", return_value=MagicMock()),
        patch("gobby.cli.uninstall._teardown_ui_exposure"),
        patch("gobby.cli.install_components.disable_rule_if_present", return_value=False),
        patch(
            "gobby.cli.install_components.remove_managed_rtk",
            return_value=RtkCleanupReport(removed=(), backups=(), conflicts=()),
        ),
        patch(
            "gobby.cli.install_components.remove_impeccable_runtime", return_value=cleanup
        ) as remove,
        patch.dict("gobby.cli.install_components._CLI_UNINSTALLERS", {"claude": remove_claude}),
    ):
        result = CliRunner().invoke(uninstall, [*arguments, "--yes"])

    assert result.exit_code == 0, result.output
    assert remove.called is cleanup_called
    assert remove_claude.called is hook_removed


def test_uninstall_impeccable_component_without_hooks(tmp_path: Path) -> None:
    removed = tmp_path / "tools" / "impeccable"
    cleanup = installer.ImpeccableRemovalResult((removed,), ())
    with (
        patch("gobby.cli.uninstall.Path.home", return_value=tmp_path),
        patch(
            "gobby.cli.install_components.remove_impeccable_runtime", return_value=cleanup
        ) as remove,
    ):
        result = CliRunner().invoke(uninstall, ["impeccable", "--yes"], catch_exceptions=False)

    assert result.exit_code == 0
    assert str(removed) in result.output
    remove.assert_called_once_with()


def _write_manifest(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_reconcile_impeccable_manifests_preserves_foreign_entries_and_metadata(
    tmp_path: Path,
) -> None:
    marker = "skills/impeccable/scripts/hook.mjs"
    foreign = {"type": "command", "command": "python foreign.py", "meta": {}}
    claude_local = tmp_path / ".claude/settings.local.json"
    claude_shared = tmp_path / ".claude/settings.json"
    codex = tmp_path / ".codex/hooks.json"
    cursor = tmp_path / ".cursor/hooks.json"
    copilot = tmp_path / ".github/hooks/impeccable.json"
    _write_manifest(
        claude_local,
        {
            "theme": "dark",
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit",
                        "hooks": [
                            {"command": f"node .claude/{marker}"},
                            foreign,
                        ],
                    }
                ]
            },
        },
    )
    _write_manifest(
        claude_shared,
        {"foreign": True, "hooks": {"Stop": [{"args": ["node", marker]}]}},
    )
    _write_manifest(codex, {"hooks": {"Stop": [{"hooks": [{"command": marker}]}]}})
    _write_manifest(cursor, {"version": 1, "hooks": {"preToolUse": [{"command": marker}]}})
    _write_manifest(
        copilot,
        {
            "version": 1,
            "owner": "foreign",
            "hooks": {
                "postToolUse": [
                    {"bash": marker},
                    {"powershell": marker},
                    {"bash": "node foreign.mjs"},
                ]
            },
        },
    )

    installer.reconcile_impeccable_installation(tmp_path)
    first_pass = {
        path: path.read_bytes() for path in (claude_local, claude_shared, cursor, copilot)
    }
    installer.reconcile_impeccable_installation(tmp_path)

    assert json.loads(claude_local.read_text()) == {
        "theme": "dark",
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [foreign]},
            ]
        },
    }
    assert json.loads(claude_shared.read_text()) == {"foreign": True}
    assert not codex.exists()
    assert json.loads(cursor.read_text()) == {"version": 1}
    assert json.loads(copilot.read_text()) == {
        "version": 1,
        "owner": "foreign",
        "hooks": {"postToolUse": [{"bash": "node foreign.mjs"}]},
    }
    assert all(path.read_bytes() == content for path, content in first_pass.items())


def test_reconcile_deletes_owned_only_copilot_scaffolding(tmp_path: Path) -> None:
    path = tmp_path / ".github/hooks/impeccable.json"
    _write_manifest(
        path,
        {
            "version": 1,
            "hooks": {"postToolUse": [{"bash": "node .github/skills/impeccable/scripts/hook.mjs"}]},
        },
    )

    installer.reconcile_impeccable_installation(tmp_path)

    assert not path.exists()


@pytest.mark.parametrize(
    ("relative_path", "contents"),
    [
        (".claude/settings.local.json", b"{broken\n"),
        (".claude/settings.json", b"[]\n"),
        (".codex/hooks.json", b"null\n"),
        (".cursor/hooks.json", b'"string"\n'),
        (".github/hooks/impeccable.json", b"42\n"),
    ],
)
def test_reconcile_leaves_malformed_and_non_object_manifests_byte_identical(
    tmp_path: Path,
    relative_path: str,
    contents: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)

    installer.reconcile_impeccable_installation(tmp_path)

    assert path.read_bytes() == contents
    assert "untouched" in caplog.text


def test_reconcile_merges_declined_consent_and_preserves_detector_config(tmp_path: Path) -> None:
    path = tmp_path / ".impeccable/config.local.json"
    _write_manifest(
        path,
        {
            "hook": {"consent": "accepted", "custom": True},
            "detector": {"timeout": 17},
            "foreign": ["value"],
        },
    )

    installer.reconcile_impeccable_installation(tmp_path)

    assert json.loads(path.read_text()) == {
        "hook": {"consent": "declined", "custom": True},
        "detector": {"timeout": 17},
        "foreign": ["value"],
    }


def test_vendored_hook_admin_contains_no_manifest_repair_machinery() -> None:
    script = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/skills/impeccable/scripts/hook-admin.mjs"
    ).read_text(encoding="utf-8")

    for obsolete in (
        "HOOK_MANIFEST_TARGETS",
        "repairHookManifests",
        "mergeHookManifests",
        "pruneImpeccableHookFromManifest",
        ".codex/hooks.json",
        ".github/hooks/impeccable.json",
    ):
        assert obsolete not in script
