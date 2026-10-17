from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import click
import httpx
import pytest
from click.testing import CliRunner, Result

from gobby.agents.cargo_target import cargo_release_dir
from gobby.cli import cli, daemon_lifecycle, install_setup_gclient
from gobby.install import bin_freshness_promotion, bin_set_coherence
from gobby.install.bin_release_identity import release_identity_path
from gobby.runner_pid_file import ProbeState
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked
from gobby.utils.native_bin import IDENTITY_STAMP_NAME

cutover_module = importlib.import_module("gobby.cli.cutover")

pytestmark = pytest.mark.unit


def test_cutover_refuses_node_bootstrap_before_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node_home = tmp_path / "node-home"
    node_home.mkdir()
    bootstrap_path = node_home / "bootstrap.yaml"
    bootstrap_path.write_text(
        "datastore_mode: remote\n"
        "hub_daemon_url: http://hub.example.test:60887\n"
        "database_url: postgresql://gobby:secret@100.64.0.10:5432/gobby\n",
        encoding="utf-8",
    )
    bootstrap_path.chmod(0o600)
    monkeypatch.setenv("GOBBY_HOME", str(node_home))
    reached: list[str] = []

    def forbid(name: str) -> Callable[..., None]:
        def forbidden(*_args: object, **_kwargs: object) -> None:
            reached.append(name)
            raise AssertionError(f"a node cutover must not reach {name}")

        return forbidden

    monkeypatch.setattr(cutover_module, "_build_artifacts", forbid("build"))
    monkeypatch.setattr(cutover_module, "_run", forbid("subprocess"))
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", forbid("promote"))
    monkeypatch.setattr(cutover_module, "start", click.Command("start", callback=forbid("start")))
    monkeypatch.setattr(subprocess, "run", forbid("subprocess"))
    result = _invoke_cli(tmp_path, monkeypatch, cutover_module.run_cutover, prepare_artifacts=False)
    assert result.exit_code != 0, result.output
    assert (
        "datastore_mode: remote is a node; a node runs no Python daemon and never takes "
        "the hub lease. See docs/guides/shared-stack.md (Client setup)."
    ) in result.output
    assert reached == []


SET_MEMBERS = ("gcode", "gdaemon", "ghook")
VERSION = "0.5.0"
_PROVE_CATALOG_FRESHNESS = cutover_module._prove_catalog_freshness
_NATIVE_CARGO_TARGET_DIR = os.environ.get("CARGO_TARGET_DIR")
_NATIVE_SANDBOX_CARGO_TARGET = os.environ.get("GOBBY_SANDBOX_CARGO_TARGET")
_NATIVE_SANDBOX_CARGO_WORKSPACE = os.environ.get("GOBBY_SANDBOX_CARGO_WORKSPACE")


@pytest.fixture
def native_cargo_target(monkeypatch: pytest.MonkeyPatch, postgres_database_url: str) -> None:
    """Restore native build routing and bind proofs to the isolated test database."""
    monkeypatch.setenv("GOBBY_SCHEMA_TEST_DATABASE_URL", postgres_database_url)
    for name, value in (
        ("CARGO_TARGET_DIR", _NATIVE_CARGO_TARGET_DIR),
        ("GOBBY_SANDBOX_CARGO_TARGET", _NATIVE_SANDBOX_CARGO_TARGET),
        ("GOBBY_SANDBOX_CARGO_WORKSPACE", _NATIVE_SANDBOX_CARGO_WORKSPACE),
    ):
        if value:
            monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def _isolated_catalog_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary unit tests replace the native boundary; its tests restore it."""
    monkeypatch.setattr(cutover_module, "_prove_catalog_freshness", lambda _root, _candidate: None)


def test_catalog_freshness_command_binds_exact_candidate_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(467)
    candidate = tmp_path / "gdaemon"
    monkeypatch.setattr(cutover_module, "probe_set_member_identity", lambda *_a: identity)
    monkeypatch.setenv("UPDATE_GCORE_SCHEMA_MANIFEST", "1")
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout="test result: ok. 1 passed; 0 failed; 0 ignored; 16 filtered out;"
        )
    )
    monkeypatch.setattr(cutover_module, "_run", run)
    _PROVE_CATALOG_FRESHNESS(tmp_path, candidate)
    assert run.call_args.args[0] == [
        "cargo",
        "test",
        "--locked",
        "-p",
        "gobby-core",
        "--features",
        "postgres",
        "--test",
        "catalog_manifest_freshness",
        "catalog_manifest_is_fresh_for_embedded_assets",
        "--",
        "--exact",
    ]
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert json.loads(run.call_args.kwargs["env"]["GOBBY_EXPECTED_SCHEMA_IDENTITY"]) == identity
    assert run.call_args.kwargs["timeout"] == 600
    assert "UPDATE_GCORE_SCHEMA_MANIFEST" not in run.call_args.kwargs["env"]


def test_catalog_freshness_refuses_a_zero_test_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cutover_module, "probe_set_member_identity", lambda *_a: _identity(467))
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout="test result: ok. 0 passed; 0 failed; 0 ignored;"
        )
    )
    monkeypatch.setattr(cutover_module, "_run", run)
    with pytest.raises(cutover_module.CutoverError, match="freshness test did not run") as error:
        _PROVE_CATALOG_FRESHNESS(tmp_path, tmp_path / "gdaemon")
    assert "refusing to promote" in str(error.value)
    assert "GOBBY_SCHEMA_TEST_DATABASE_URL" in str(error.value)


@pytest.mark.integration
def test_catalog_freshness_accepts_matching_checkout_candidate(native_cargo_target: None) -> None:
    repo = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["cargo", "build", "--locked", "-p", "gobby-daemon"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    candidate = cargo_release_dir(repo).parent / "debug/gdaemon"
    assert candidate.is_file(), "the checkout build must produce its routed gdaemon candidate"
    _PROVE_CATALOG_FRESHNESS(repo, candidate)


@pytest.mark.integration
def test_stale_embedded_catalog_refuses_cutover_before_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_cargo_target: None
) -> None:
    """Compile a stale candidate and use real native freshness against isolated60892."""
    repo = Path(__file__).resolve().parents[2]
    # Reuse dependencies across temporary crate roots instead of cold-building each run.
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cargo_release_dir(repo).parent / "catalog-fixture"))
    fixture = tmp_path / "candidate-core"
    fixture.mkdir()
    core = repo / "crates/gcore"
    for name in ("src", "assets"):
        shutil.copytree(core / name, fixture / name)
    shutil.copyfile(core / "Cargo.toml", fixture / "Cargo.toml")
    shutil.copyfile(repo / "Cargo.lock", fixture / "Cargo.lock")
    (fixture / "tests").mkdir()
    shutil.copyfile(
        core / "tests/catalog_manifest_freshness.rs",
        fixture / "tests/catalog_manifest_freshness.rs",
    )
    manifest_path = fixture / "assets/schema/catalog.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    removed = manifest["functions"].pop()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (fixture / "examples").mkdir()
    (fixture / "examples/catalog_candidate_24042.rs").write_text(
        'fn main() { if std::env::args().nth(1).as_deref() == Some("--version") '
        '{ println!("gdaemon 1.0.0"); return; } println!("{}", serde_json::to_string('
        "&gobby_core::schema::SchemaIdentityContract::embedded()).unwrap()); }\n"
    )
    subprocess.run(
        [
            "cargo",
            "build",
            "--offline",
            "--features",
            "postgres",
            "--example",
            "catalog_candidate_24042",
        ],
        cwd=fixture,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    candidate = cargo_release_dir(fixture).parent / "debug/examples/catalog_candidate_24042"
    assert candidate.is_file(), "the native fixture build must produce its routed catalog candidate"
    monkeypatch.setattr(
        cutover_module, "_build_artifacts", lambda _root: _set_only({"gdaemon": candidate})
    )
    monkeypatch.setattr(
        cutover_module,
        "_prove_catalog_freshness",
        lambda _root, candidate: _PROVE_CATALOG_FRESHNESS(fixture, candidate),
    )
    promotion, restart = Mock(), Mock()
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", promotion)
    with pytest.raises(cutover_module.CutoverError) as error:
        cutover_module.run_cutover(
            repo,
            tmp_path / "bin",
            stop_daemon=lambda: True,
            start_daemon=restart,
            start_refusal=lambda _candidate: None,
        )
    assert "catalog manifest is stale" in str(error.value)
    assert removed["name"] in str(error.value)
    promotion.assert_not_called()
    restart.assert_not_called()
    assert not (tmp_path / "bin").exists()


def _identity(version: int) -> dict[str, int | str]:
    return {
        "baseline_version": 1,
        "latest_version": version,
        "baseline_checksum": "a" * 64,
        "latest_checksum": f"{version:064x}",
        "assets_root_hash": "b" * 64,
        "runner_protocol": 3,
    }


def _write_stub(path: Path, identity: dict[str, int | str], version: str = VERSION) -> None:
    payload = json.dumps(identity, sort_keys=True)
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then printf '%s\\n' '{path.name} {version}'; "
        f"else printf '%s\\n' '{payload}'; fi\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _artifacts(root: Path, identity: dict[str, int | str]) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for member in SET_MEMBERS:
        crate = root / "crates" / member
        crate.mkdir(parents=True, exist_ok=True)
        (crate / "Cargo.toml").write_text(f'[package]\nname = "{member}"\nversion = "{VERSION}"\n')
    release = root / "target" / "release"
    release.mkdir(parents=True, exist_ok=True)
    artifacts = {member: release / member for member in SET_MEMBERS}
    for artifact in artifacts.values():
        _write_stub(artifact, identity)
    return artifacts


def _set_only(artifacts: dict[str, Path]) -> object:
    return cutover_module._Build(artifacts, "gclient is not built in this test")


def _client_artifact(root: Path) -> Path:
    crate = root / "crates" / "gclient"
    crate.mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(f'[package]\nname = "gobby-client"\nversion = "{VERSION}"\n')
    artifact = root / "target" / "release" / "gclient"
    _write_stub(artifact, {})
    return artifact


def _with_client(root: Path, artifacts: dict[str, Path]) -> object:
    client = cutover_module._Client(
        _client_artifact(root), cutover_module.workspace_source_identity(root, "gclient")
    )
    return cutover_module._Build(artifacts, client)


def _sign_into(monkeypatch: pytest.MonkeyPatch, signed: list[str]) -> None:
    def sign(binary: Path) -> None:
        signed.append(binary.name)

    monkeypatch.setattr(bin_set_coherence, "_codesign_workspace_binary", sign)
    monkeypatch.setattr(install_setup_gclient, "_codesign_workspace_binary", sign)


def _run_live_cutover(root: Path, bin_dir: Path, started: list[bytes]) -> str | None:
    result: str | None = cutover_module.run_cutover(
        root,
        bin_dir,
        stop_daemon=lambda: True,
        start_daemon=lambda: started.append((bin_dir / "gdaemon").read_bytes()),
        start_refusal=lambda _candidate: None,
    )
    return result


def test_run_cutover_builds_and_promotes_through_shared_set_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    identity = _identity(420)
    artifacts = _artifacts(root, identity)
    build = _with_client(root, artifacts)
    client_artifact = root / "target" / "release" / "gclient"
    _git(root, "add", "-A")
    _git(root, "commit", "--no-gpg-sign", "-q", "-m", "initial")
    old_client = bin_dir / "gclient"
    _write_stub(old_client, {}, "0.4.0")
    os.utime(old_client, (1_000_000_000, 1_000_000_000))
    old_inode = old_client.stat().st_ino
    monkeypatch.setattr(bin_freshness_promotion, "_workspace_crates_root", lambda: root / "crates")
    assert bin_freshness_promotion.native_bin_predates_source("gclient", bin_dir=bin_dir) is True
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: build)
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    monkeypatch.setattr(
        cutover_module,
        "resolve_native_bin",
        lambda name: str(bin_dir / name) if name == "gdaemon" else None,
    )
    signed: list[str] = []
    _sign_into(monkeypatch, signed)
    promotion = Mock(wraps=bin_set_coherence.promote_workspace_binary_set)
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", promotion)
    restarted: list[bytes] = []

    assert _run_live_cutover(root, bin_dir, restarted) is None

    assert promotion.call_count == 1
    assert tuple(promotion.call_args.args[0]) == SET_MEMBERS
    assert signed == [*SET_MEMBERS, "gclient"]
    assert (bin_dir / "gclient").read_bytes() == client_artifact.read_bytes()
    assert (bin_dir / "gclient").stat().st_ino != old_inode
    assert bin_freshness_promotion.read_source_hash(
        bin_dir, "gclient"
    ) == bin_freshness_promotion.file_sha256(client_artifact)
    assert (bin_dir / ".gclient-version").read_text(encoding="utf-8") == f"{VERSION}\n"
    assert bin_freshness_promotion.native_bin_predates_source("gclient", bin_dir=bin_dir) is False
    for member, artifact in artifacts.items():
        assert (bin_dir / member).read_bytes() == artifact.read_bytes()
        sidecar = json.loads((bin_dir / f".{member}-install.json").read_text(encoding="utf-8"))
        assert sidecar["install_method"] == "workspace-cutover"
        assert sidecar["target"] == "test-target"
    assert (
        json.loads((bin_dir / ".gdaemon-schema-identity.json").read_text(encoding="utf-8"))
        == identity
    )
    assert restarted == [(bin_dir / "gdaemon").read_bytes()]


def test_run_cutover_fails_closed_when_resolved_gdaemon_differs_from_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    identity = _identity(420)
    artifacts = _artifacts(root, identity)
    stale_gdaemon = tmp_path / "stale-gdaemon"
    _write_stub(stale_gdaemon, _identity(419))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: _set_only(artifacts))
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    monkeypatch.setattr(cutover_module, "resolve_native_bin", lambda _name: str(stale_gdaemon))
    monkeypatch.setattr(bin_set_coherence, "_codesign_workspace_binary", lambda _path: None)

    with pytest.raises(cutover_module.CutoverError) as exc_info:
        cutover_module.run_cutover(
            root,
            bin_dir,
            stop_daemon=lambda: True,
            start_daemon=pytest.fail,
            start_refusal=lambda _candidate: None,
        )

    message = str(exc_info.value)
    assert str(stale_gdaemon) in message
    assert "v419" in message
    assert "v420" in message
    assert "rebuild and install all three together" in message
    assert (bin_dir / "gdaemon").read_bytes() == artifacts["gdaemon"].read_bytes()
    assert "restored prior install" not in message


_SET_BUILD = [
    "cargo",
    "build",
    "--release",
    "--locked",
    "-p",
    "gobby-code",
    "-p",
    "gobby-daemon",
    "-p",
    "gobby-hooks",
]


def _record_cargo(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], *, failing: tuple[int, ...] = ()
) -> None:
    """Record cargo runs (the 1-based `failing` runs exit 101); other commands run for real."""
    real_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "cargo":
            return real_run(args, **kwargs)
        calls.append(args)
        code = 101 if len(calls) in failing else 0
        return subprocess.CompletedProcess(args=args, returncode=code, stdout="", stderr="E0425")

    monkeypatch.setattr(cutover_module.subprocess, "run", run)


def test_build_uses_one_locked_release_command_for_the_set_and_gclient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path, _identity(420))
    client = _client_artifact(tmp_path)
    calls: list[list[str]] = []
    _record_cargo(monkeypatch, calls)

    build = cutover_module._build_artifacts(tmp_path)

    assert calls == [[*_SET_BUILD, "-p", "gobby-client"]]
    assert build.artifacts == artifacts
    assert build.client.artifact == client
    assert build.client.source == cutover_module.workspace_source_identity(tmp_path, "gclient")


def test_failed_shared_build_rebuilds_the_set_alone_and_reports_gclient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path, _identity(420))
    _client_artifact(tmp_path)
    calls: list[list[str]] = []
    _record_cargo(monkeypatch, calls, failing=(1,))

    build = cutover_module._build_artifacts(tmp_path)

    assert calls == [[*_SET_BUILD, "-p", "gobby-client"], _SET_BUILD]
    assert build.artifacts == artifacts
    assert build.client == "release build failed: E0425"


def test_set_build_failure_after_shared_failure_still_fails_the_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _artifacts(tmp_path, _identity(420))
    _client_artifact(tmp_path)
    calls: list[list[str]] = []
    _record_cargo(monkeypatch, calls, failing=(1, 2))

    with pytest.raises(cutover_module.CutoverError, match="release build failed: E0425"):
        cutover_module._build_artifacts(tmp_path)
    assert calls == [[*_SET_BUILD, "-p", "gobby-client"], _SET_BUILD]


def test_missing_gclient_artifact_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path, _identity(420))
    _client_artifact(tmp_path).unlink()
    _record_cargo(monkeypatch, [])

    build = cutover_module._build_artifacts(tmp_path)

    assert build.artifacts == artifacts
    assert build.client == f"release build omitted {tmp_path / 'target' / 'release' / 'gclient'}"


def test_gclient_promotion_refusal_is_reported_and_leaves_the_set_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    artifacts = _artifacts(root, _identity(420))
    build = _with_client(root, artifacts)
    installed_client = bin_dir / "gclient"
    _write_stub(installed_client, {"installed": True})
    before = installed_client.read_bytes()
    release_identity_path(bin_dir, "gclient").write_text(
        json.dumps(
            {"version": VERSION, "source_identity": "older-tree", "artifact_sha256": "0" * 64}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: build)
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    monkeypatch.setattr(
        cutover_module,
        "resolve_native_bin",
        lambda name: str(bin_dir / name) if name == "gdaemon" else None,
    )
    _sign_into(monkeypatch, [])
    restarted: list[bytes] = []

    failure = _run_live_cutover(root, bin_dir, restarted)

    assert failure == (
        f"gclient: changed or unverified source under installed version {VERSION}; "
        "bump its patch version before promotion"
    )
    assert restarted == [artifacts["gdaemon"].read_bytes()]
    for member, artifact in artifacts.items():
        assert (bin_dir / member).read_bytes() == artifact.read_bytes()
    assert installed_client.read_bytes() == before


def test_unbuilt_gclient_is_returned_after_the_set_goes_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    artifacts = _artifacts(root, _identity(420))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: _set_only(artifacts))
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    monkeypatch.setattr(
        cutover_module,
        "resolve_native_bin",
        lambda name: str(bin_dir / name) if name == "gdaemon" else None,
    )
    _sign_into(monkeypatch, [])
    restarted: list[bytes] = []

    assert _run_live_cutover(root, bin_dir, restarted) == "gclient is not built in this test"
    assert restarted == [artifacts["gdaemon"].read_bytes()]
    assert not (bin_dir / "gclient").exists()


def _invoke_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_cutover: Callable[..., str | None],
    *,
    dirty: list[str] | None = None,
    identity: dict[str, int | str] | None = None,
    extra_args: tuple[str, ...] = (),
    prepare_artifacts: bool = True,
) -> Result:
    workspace = tmp_path / "workspace"
    (workspace / "crates").mkdir(parents=True)
    (workspace / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    pin = workspace / "src" / "gobby" / "storage" / "schema_expected_identity.json"
    pin.parent.mkdir(parents=True)
    pin.write_text(json.dumps(identity or _identity(420)), encoding="utf-8")
    if prepare_artifacts:
        _artifacts(workspace, identity or _identity(420))
    monkeypatch.setattr(cutover_module, "run_cutover", run_cutover)
    monkeypatch.setattr(cutover_module, "_dirty_schema_inputs", lambda _root: list(dirty or []))
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(tmp_path / "managed-bin"))
    return CliRunner().invoke(cli, ["cutover", "--path", str(workspace), *extra_args])


def test_cli_targets_the_native_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: list[Path] = []

    def run_cutover(_root: Path, bin_dir: Path, **_kwargs: object) -> None:
        observed.append(bin_dir)

    result = _invoke_cli(tmp_path, monkeypatch, run_cutover)

    assert result.exit_code == 0, result.output
    assert observed == [tmp_path / "managed-bin"]
    assert result.stdout.splitlines()[-1] == "gclient matches this build."


def test_cli_reports_gclient_failure_after_the_set_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run_cutover(*_args: object, **_kwargs: object) -> str:
        return "release build failed: E0425"

    result = _invoke_cli(tmp_path, monkeypatch, run_cutover)

    assert result.exit_code == 0, result.output
    assert "Cutover complete: gcode, gdaemon, ghook" in result.stdout
    assert result.stderr == (
        "gclient was not promoted: release build failed: E0425. The daemon set is live; "
        "after resolving this, run `uv run gobby install gclient`.\n"
    )


def test_cli_preflights_candidate_against_selected_workspace_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = _identity(443)
    candidate = tmp_path / "candidate-gdaemon"
    observed: list[dict[str, int | str]] = []

    def restart_start_refusal(
        _ctx: object,
        _candidate: Path,
        *,
        expected_identity: dict[str, int | str],
    ) -> None:
        observed.append(expected_identity)

    def run_cutover(_root: Path, _bin_dir: Path, **kwargs: object) -> None:
        start_refusal = kwargs["start_refusal"]
        assert callable(start_refusal)
        assert start_refusal(candidate) is None

    monkeypatch.setattr(cutover_module, "restart_start_refusal", restart_start_refusal)
    monkeypatch.setattr(
        cutover_module, "_stop_admission_refusal", lambda _ctx, *, wait, logged_stale_markers: None
    )
    result = _invoke_cli(tmp_path, monkeypatch, run_cutover, identity=expected)

    assert result.exit_code == 0, result.output
    assert observed == [expected]


def test_cli_stops_before_starting_the_selected_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    def stop(
        _ctx: click.Context,
        docker_flag: bool,
        *,
        shutdown_intent: str,
        wait: bool,
        logged_stale_markers: set[tuple[str, str | None, str]],
    ) -> bool:
        assert (docker_flag, shutdown_intent, wait) == (False, "restart", True)
        events.append("stop")
        return True

    def start(*, verbose: bool) -> None:
        assert verbose is False
        events.append("start")

    def run_cutover(
        _root: Path,
        _bin_dir: Path,
        *,
        stop_daemon: Callable[[], bool],
        start_daemon: Callable[[], None],
        **_kwargs: object,
    ) -> None:
        assert stop_daemon()
        start_daemon()

    monkeypatch.setattr(cutover_module, "_do_stop", stop)
    monkeypatch.setattr(cutover_module, "start", start)
    result = _invoke_cli(tmp_path, monkeypatch, run_cutover)

    assert result.exit_code == 0, result.output
    assert events == ["stop", "start"]


def test_cli_reports_daemon_start_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing_start(**_kwargs: object) -> None:
        raise SystemExit(7)

    monkeypatch.setattr(cutover_module, "start", failing_start)

    def run_cutover(
        _root: Path, _bin_dir: Path, *, start_daemon: Callable[[], None], **_kwargs: object
    ) -> None:
        start_daemon()

    result = _invoke_cli(tmp_path, monkeypatch, run_cutover)

    assert result.exit_code == 1
    assert "daemon start failed (exit 7)" in result.output


def test_cutover_has_no_private_replacement_or_rollback_machinery() -> None:
    assert not hasattr(cutover_module, "_ReplacementSet")
    assert not hasattr(cutover_module, "_stage_sidecars")
    assert "cutover" in cli.commands


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Gobby Tests", "-c", "user.email=tests@example.com", *args],
        cwd=repo,
        check=True,
        timeout=30,
        capture_output=True,
    )


def _schema_repo(tmp_path: Path) -> Path:
    """A real git repository carrying both schema inputs and unrelated crate sources."""
    repo = tmp_path / "repo"
    (repo / "crates" / "gcore" / "assets" / "schema").mkdir(parents=True)
    (repo / "crates" / "gcore" / "src" / "schema").mkdir(parents=True)
    (repo / "crates" / "gcode").mkdir(parents=True)
    pin = repo / cutover_module._PIN_PATH
    pin.parent.mkdir(parents=True)
    pin.write_text("{}\n", encoding="utf-8")
    (repo / "crates" / "gcore" / "assets" / "schema" / "baseline.sql").write_text("-- baseline\n")
    (repo / "crates" / "gcore" / "src" / "schema" / "runner.rs").write_text("// runner\n")
    (repo / "crates" / "gcode" / "lib.rs").write_text("// gcode\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "initial")
    return repo


def test_run_cutover_refuses_before_promotion_when_candidate_plan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal after promotion would already have replaced the installed set."""
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    artifacts = _artifacts(root, _identity(420))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: _set_only(artifacts))
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    promotion = Mock(side_effect=AssertionError("promotion must not run"))
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", promotion)

    with pytest.raises(cutover_module.CutoverError) as exc_info:
        cutover_module.run_cutover(
            root,
            bin_dir,
            stop_daemon=lambda: True,
            start_daemon=pytest.fail,
            start_refusal=lambda _candidate: "unrecognized schema lineage",
        )

    message = str(exc_info.value)
    assert "refusing to promote" in message
    assert "unrecognized schema lineage" in message
    assert not bin_dir.exists(), "nothing may be promoted on refusal"
    promotion.assert_not_called()


def test_cli_refuses_dirty_schema_inputs_and_names_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run_cutover(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the build must not run from dirty schema inputs")

    result = _invoke_cli(
        tmp_path, monkeypatch, run_cutover, dirty=["crates/gcore/src/schema/assets.rs"]
    )

    assert result.exit_code == 1, result.output
    assert "uncommitted schema inputs" in result.output
    assert "crates/gcore/src/schema/assets.rs" in result.output
    assert "--allow-dirty" in result.output


def test_cli_allow_dirty_skips_the_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: list[Path] = []

    def run_cutover(_root: Path, bin_dir: Path, **_kwargs: object) -> None:
        observed.append(bin_dir)

    result = _invoke_cli(
        tmp_path,
        monkeypatch,
        run_cutover,
        dirty=["crates/gcore/src/schema/assets.rs"],
        extra_args=("--allow-dirty",),
    )

    assert result.exit_code == 0, result.output
    assert observed == [tmp_path / "managed-bin"]


def test_dirty_schema_inputs_is_scoped_to_schema_paths(tmp_path: Path) -> None:
    repo = _schema_repo(tmp_path)
    assert cutover_module._dirty_schema_inputs(repo) == []

    (repo / "crates" / "gcode" / "lib.rs").write_text("// edited\n")
    assert cutover_module._dirty_schema_inputs(repo) == [], "non-schema dirt must not block"

    pin = repo / cutover_module._PIN_PATH
    pin.write_text('{"latest_version": 1}\n', encoding="utf-8")
    assert cutover_module._dirty_schema_inputs(repo) == [str(cutover_module._PIN_PATH)]

    pin.write_text("{}\n", encoding="utf-8")
    migration = repo / "crates" / "gcore" / "assets" / "schema" / "migrations" / "440_x.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    assert cutover_module._dirty_schema_inputs(repo) == [
        "crates/gcore/assets/schema/migrations/440_x.sql"
    ]


def test_dirty_schema_inputs_fails_closed_without_git(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    with pytest.raises(cutover_module.CutoverError):
        cutover_module._dirty_schema_inputs(outside)


class _FakeRuntime:
    def read_only_operational_config(self) -> SimpleNamespace:
        return SimpleNamespace(daemon_port=60887)


def _stub_cutover_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events: list[str]
) -> None:
    _artifacts(tmp_path, _identity(420))

    def promote(*_args: object, **_kwargs: object) -> None:
        events.append("promote")

    monkeypatch.setattr(
        cutover_module, "_build_artifacts", lambda root: _set_only(_artifacts(root, _identity(420)))
    )
    monkeypatch.setattr(cutover_module, "restart_start_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", promote)
    monkeypatch.setattr(cutover_module, "_verify_restart_target", lambda _bin_dir: None)
    monkeypatch.setattr(cutover_module, "get_cli_runtime", lambda _ctx: _FakeRuntime())


@pytest.mark.parametrize("blocker", ["protected_run", "handoff"])
def test_cutover_refuses_before_promotion_when_stop_admission_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blocker: str
) -> None:
    events: list[str] = []
    real_run_cutover = cutover_module.run_cutover
    _stub_cutover_pipeline(monkeypatch, tmp_path, events)

    def clear_protected_runs(_port: int, *, force: bool, wait: bool, **_kwargs: object) -> bool:
        assert (force, wait) == (False, False)
        return blocker != "protected_run"

    @contextmanager
    def protect_pending_handoffs(
        _runtime: object, *, wait: bool, **_kwargs: object
    ) -> Iterator[None]:
        assert wait is False
        raise HandoffShutdownBlocked("Unresolved set_handoff in session(s): gobby#1")
        yield

    def restart(**_kwargs: object) -> None:
        events.append("restart")

    monkeypatch.setattr(cutover_module, "clear_protected_runs", clear_protected_runs)
    monkeypatch.setattr(cutover_module, "protect_pending_handoffs", protect_pending_handoffs)
    monkeypatch.setattr(cutover_module, "start", restart)
    result = _invoke_cli(tmp_path, monkeypatch, real_run_cutover)

    assert result.exit_code == 1
    assert "refusing to promote" in result.output
    expected = "cron run is active" if blocker == "protected_run" else "Unresolved set_handoff"
    assert expected in result.output
    assert "re-run with --wait" in result.output
    assert events == []


def test_cutover_waits_for_stop_before_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    warning_sets: list[set[tuple[str, str | None, str]]] = []
    real_run_cutover = cutover_module.run_cutover
    _stub_cutover_pipeline(monkeypatch, tmp_path, events)

    def clear_protected_runs(_port: int, *, force: bool, wait: bool, **_kwargs: object) -> bool:
        events.append(f"protected_runs wait={wait}")
        return True

    @contextmanager
    def protect_pending_handoffs(
        _runtime: object,
        *,
        wait: bool,
        logged_stale_markers: set[tuple[str, str | None, str]],
        **_kwargs: object,
    ) -> Iterator[None]:
        events.append(f"handoffs wait={wait}")
        warning_sets.append(logged_stale_markers)
        yield

    def stop(
        _ctx: click.Context,
        _docker: bool,
        *,
        wait: bool,
        logged_stale_markers: set[tuple[str, str | None, str]],
        **_kwargs: object,
    ) -> bool:
        events.append(f"stop wait={wait}")
        warning_sets.append(logged_stale_markers)
        return True

    def restart(**_kwargs: object) -> None:
        events.append("start")

    monkeypatch.setattr(cutover_module, "_do_stop", stop)

    monkeypatch.setattr(cutover_module, "clear_protected_runs", clear_protected_runs)
    monkeypatch.setattr(cutover_module, "protect_pending_handoffs", protect_pending_handoffs)
    monkeypatch.setattr(cutover_module, "start", restart)
    for invocation in ("first", "second"):
        result = _invoke_cli(
            tmp_path / invocation, monkeypatch, real_run_cutover, extra_args=("--wait",)
        )
        assert result.exit_code == 0, result.output
    assert (
        events
        == [
            "protected_runs wait=True",
            "handoffs wait=True",
            "stop wait=True",
            "promote",
            "start",
        ]
        * 2
    )
    assert len(warning_sets) == 4
    assert warning_sets[0] is warning_sets[1]
    assert warning_sets[2] is warning_sets[3]
    assert warning_sets[0] is not warning_sets[2]


@pytest.mark.parametrize("status", [409, 401, 500])
def test_route_stop_refusal_leaves_installed_set_and_stamp_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = {name: bin_dir / name for name in SET_MEMBERS}
    for path in installed.values():
        _write_stub(path, _identity(419), "0.4.0")
    stamp = bin_dir / ".gdaemon-schema-identity.json"
    stamp.write_text(json.dumps(_identity(419)), encoding="utf-8")
    paths = [*installed.values(), stamp]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    candidate = _artifacts(tmp_path, _identity(420))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: _set_only(candidate))
    monkeypatch.setattr(daemon_lifecycle, "is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(
        daemon_lifecycle,
        "probe_daemon_lock",
        lambda _path: SimpleNamespace(state=ProbeState.DAEMON),
    )
    monkeypatch.setattr(daemon_lifecycle, "_is_daemon_healthy", lambda _port: True)
    monkeypatch.setattr(daemon_lifecycle, "daemon_auth_headers", lambda: {})
    response = httpx.Response(status, json={"status": "handoff_pending"})
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    started = Mock()

    with pytest.raises(cutover_module.CutoverError, match="installed binaries unchanged"):
        cutover_module.run_cutover(
            tmp_path,
            bin_dir,
            stop_daemon=lambda: daemon_lifecycle._request_admin_stop(
                12345, force=True, drain_terminals=False, restart_supervisor=True
            )
            is True,
            start_daemon=started,
            start_refusal=lambda _candidate: None,
        )

    assert post.call_count == 1
    assert post.call_args.kwargs["params"]["restart_supervisor"] == "true"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
    started.assert_not_called()


@pytest.mark.parametrize("failure", [SystemExit(7), click.ClickException("startup refused")])
def test_cli_post_stop_start_failure_reports_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    events: list[str] = []
    _stub_cutover_pipeline(monkeypatch, tmp_path, events)

    def stop(*_args: object, **_kwargs: object) -> bool:
        events.append("stop")
        return True

    def start(**_kwargs: object) -> None:
        events.append("start")
        raise failure

    monkeypatch.setattr(cutover_module, "_do_stop", stop)
    monkeypatch.setattr(cutover_module, "start", start)
    monkeypatch.setattr(cutover_module, "_stop_admission_refusal", lambda *_args, **_kwargs: None)
    result = _invoke_cli(tmp_path, monkeypatch, cutover_module.run_cutover)

    assert result.exit_code == 1
    assert "The daemon is stopped" in result.output, result.exception
    assert "After resolving this failure, run `gobby start`" in result.output
    assert ("exit 7" if isinstance(failure, SystemExit) else "startup refused") in result.output
    assert events == ["stop", "promote", "start"]
    assert "Cutover complete" not in result.output


def test_cli_post_stop_invalid_utf8_pin_reports_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    verify_target = cutover_module._verify_restart_target
    _stub_cutover_pipeline(monkeypatch, tmp_path, events)
    monkeypatch.setattr(cutover_module, "_verify_restart_target", verify_target)
    bin_dir = tmp_path / "managed-bin"
    bin_dir.mkdir()
    (bin_dir / IDENTITY_STAMP_NAME).write_bytes(b"\xff")

    def stop(*_args: object, **_kwargs: object) -> bool:
        events.append("stop")
        return True

    started = Mock()
    monkeypatch.setattr(cutover_module, "_do_stop", stop)
    monkeypatch.setattr(cutover_module, "start", started)
    monkeypatch.setattr(cutover_module, "_stop_admission_refusal", lambda *_args, **_kwargs: None)
    result = _invoke_cli(tmp_path, monkeypatch, cutover_module.run_cutover)

    assert result.exit_code == 1
    assert "The daemon is stopped" in result.output, result.exception
    assert "After resolving this failure, run `gobby start`" in result.output
    assert "utf-8" in result.output
    assert events == ["stop", "promote"]
    started.assert_not_called()
    assert "Cutover complete" not in result.output


@pytest.mark.parametrize("stage", ["promotion", "directory", "verification"])
def test_post_stop_failure_reports_stopped_daemon_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    events: list[str] = []
    _stub_cutover_pipeline(monkeypatch, tmp_path, events)

    def fail(*_args: object, **_kwargs: object) -> None:
        events.append(stage)
        if stage == "promotion":
            raise bin_set_coherence.BinarySetCoherenceError("partial promotion failed")
        if stage == "directory":
            raise OSError("cannot create binary directory")
        raise cutover_module.CutoverError("installed identity mismatch")

    def stop() -> bool:
        events.append("stop")
        return True

    target = "_verify_restart_target" if stage == "verification" else "promote_workspace_binary_set"
    monkeypatch.setattr(cutover_module, target, fail)
    started = Mock()

    with pytest.raises(cutover_module.CutoverError) as error:
        cutover_module.run_cutover(
            tmp_path,
            tmp_path / "bin",
            stop_daemon=stop,
            start_daemon=started,
            start_refusal=lambda _candidate: None,
        )

    assert "The daemon is stopped" in str(error.value)
    assert "After resolving this failure, run `gobby start`" in str(error.value)
    expected_error = {
        "promotion": "partial promotion failed",
        "directory": "cannot create binary directory",
        "verification": "installed identity mismatch",
    }[stage]
    assert expected_error in str(error.value)
    assert error.value.__cause__ is not None
    assert events == (
        ["stop", "promote", "verification"] if stage == "verification" else ["stop", stage]
    )
    started.assert_not_called()
