from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from gobby.cli import install_setup_impeccable as installer
from gobby.skills.script_cache import (
    BrowserCacheReadiness,
    browser_cache_is_ready,
    read_browser_cache_readiness,
    write_browser_cache_readiness,
)
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
    assert IMPECCABLE_NODE_MIN_VERSION == "22.18.0"
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
    monkeypatch.setattr(installer, "_detect_node", lambda: (Path("/node"), "22.17.9"))
    with pytest.raises(installer.ImpeccableInstallError, match="22.18.0"):
        installer.install_impeccable_cli()
    assert not (home / "tools" / "impeccable").exists()

    monkeypatch.setattr(installer, "_detect_node", lambda: (Path("/node"), "22.18.0"))
    installer.install_impeccable_cli()
    before = sorted(path.relative_to(home) for path in home.rglob("*"))
    monkeypatch.setattr(installer, "_detect_node", lambda: (Path("/node"), "22.17.9"))
    with pytest.raises(installer.ImpeccableInstallError, match="22.18.0"):
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


def test_preflight_succeeds_without_impeccable(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert report.optional["impeccable"].state == "missing"
    assert required_dependency_errors(report) == []


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
        installer._write_json = lambda path, value: os._exit(91)
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
        identity = installer._process_start_identity(process.pid)
        assert identity is not None
        installer._write_json(
            stage / installer._OWNER_FILE,
            {"pgid": process.pid, "leader_start": identity},
        )

        installer._collect_abandoned_generations(root, root / "3.5.0")
        assert stage.exists()

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(installer, "_process_start_identity", lambda pid: "reused")
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
