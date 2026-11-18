"""Promotion holds and burned versions refuse replacement at every promotion entry point.

A refused promotion leaves the installed bytes and the generation directories as they were.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.install import bin_freshness_promotion as promotion
from gobby.install.bin_generations import (
    GENERATIONS_DIR,
    burn_version,
    list_generations,
    read_state,
    set_hold,
)
from gobby.install.bin_release_identity import (
    BinaryReleaseIdentity,
    BinaryReleaseVersionError,
    check_binary_release,
    current_hold_token,
    promotion_hold_token,
)
from gobby.install.bin_set_coherence import promote_workspace_binary_set

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
HOLD_TASK = "hold-owner-task"
HOLD_SESSION = "hold-owner-session"
MEMBER = "gdaemon"
SET_MEMBERS = ("gcode", "gdaemon", "ghook")
CRATE_DIRS = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterminal", "gcore")
IDENTITY = {
    "runner_protocol": 1,
    "baseline_version": 420,
    "baseline_checksum": "baseline",
    "latest_version": 468,
    "latest_checksum": "latest",
    "assets_root_hash": "root",
}

Promote = Callable[[Path, Path, Path], None]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for name in CRATE_DIRS:
        crate = root / "crates" / name
        (crate / "src").mkdir(parents=True)
        dependency = '\n[dependencies]\ngobby-core = { path = "../gcore" }\n'
        if name == "gclient":
            dependency += 'gobby-terminal = { path = "../gterminal" }\n'
        (crate / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
            + (dependency if name != "gcore" else "")
        )
        (crate / "src" / "lib.rs").write_text("pub fn initial() {}\n")
    monkeypatch.setattr(promotion, "_workspace_crates_root", lambda: root / "crates")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def _binary(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(IDENTITY, separators=(",", ":"), sort_keys=True)
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' '{path.name} {version}'\nelse\n"
        f"printf '%s\\n' '{payload}'\nfi\n"
    )
    path.chmod(0o755)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _commit_all(root: Path, message: str) -> str:
    git = ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run([*git, "add", "--all"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True, capture_output=True)
    return subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _hold(bin_dir: Path) -> str:
    """An hour-long hold set now. The check takes no clock, so it judges expiry at wall time."""
    now = datetime.now(UTC)
    bin_dir.mkdir(parents=True, exist_ok=True)
    return set_hold(
        bin_dir,
        task_id=HOLD_TASK,
        session_id=HOLD_SESSION,
        expires_at=now + timedelta(hours=1),
        now=now,
    )


def _installed(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _generation_dirs(bin_dir: Path) -> list[str]:
    store = bin_dir / GENERATIONS_DIR
    if not store.is_dir():
        return []
    return sorted(path.name for path in store.iterdir() if path.is_dir())


def _archive(candidate: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(candidate, arcname=f"dist/{MEMBER}")
    return buffer.getvalue()


def _cargo_run(candidate: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        root = Path(command[command.index("--root") + 1])
        (root / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, root / "bin" / MEMBER)
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def _via_preflight(workspace: Path, candidate: Path, bin_dir: Path) -> None:
    promotion.preflight_binary_promotions(
        {MEMBER: candidate}, bin_dir=bin_dir, workspace_root=workspace
    )


def _via_binary_file(workspace: Path, candidate: Path, bin_dir: Path) -> None:
    promotion.stage_and_promote_binary_file(
        candidate, destination=bin_dir / MEMBER, workspace_root=workspace
    )


def _via_release_archive(workspace: Path, candidate: Path, bin_dir: Path) -> None:
    promotion.stage_and_promote_release_binary(
        _archive(candidate),
        archive_ext="tar.gz",
        binary_name=MEMBER,
        bin_dir=bin_dir,
        asset_name=f"{MEMBER}.tar.gz",
    )


def _via_cargo_install(workspace: Path, candidate: Path, bin_dir: Path) -> None:
    promotion.install_staged_cargo_binary(
        ["cargo", "install", "--root", "unused", MEMBER],
        binary_name=MEMBER,
        bin_dir=bin_dir,
        run=_cargo_run(candidate),
        timeout=60,
    )


ENTRY_POINTS = [
    pytest.param(_via_preflight, id="preflight"),
    pytest.param(_via_binary_file, id="binary-file"),
    pytest.param(_via_release_archive, id="release-archive"),
    pytest.param(_via_cargo_install, id="cargo-install"),
]
# Preflight only validates; the promoting entry points replace the installed binary.
PROMOTING_ENTRY_POINTS = ENTRY_POINTS[1:]


def _install_first(workspace: Path, bin_dir: Path, root: Path) -> bytes:
    """Install version 1.0.0 with no hold in place and return the installed bytes."""
    _via_binary_file(workspace, _binary(root / "1.0.0" / MEMBER, "1.0.0"), bin_dir)
    return (bin_dir / MEMBER).read_bytes()


def _check(source: Path, bin_dir: Path, name: str = MEMBER) -> BinaryReleaseIdentity:
    return check_binary_release(
        name, source, bin_dir=bin_dir, workspace_root=None, source_identity="source-a"
    )


def _candidate_identity(source: Path, version: str) -> BinaryReleaseIdentity:
    return BinaryReleaseIdentity(
        version=version, source_identity="source-a", artifact_sha256=_sha(source)
    )


# Hold token context and visibility.


def test_hold_token_is_none_outside_any_body() -> None:
    assert current_hold_token() is None


def test_hold_token_restores_the_enclosing_value_on_exit() -> None:
    with promotion_hold_token("outer"):
        with promotion_hold_token("inner"):
            assert current_hold_token() == "inner"
        assert current_hold_token() == "outer"
    assert current_hold_token() is None


def test_hold_token_is_restored_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError, match="body failed"):
        with promotion_hold_token("token-a"):
            raise RuntimeError("body failed")
    assert current_hold_token() is None


def test_hold_token_is_visible_to_a_thread_started_in_the_body() -> None:
    seen: list[str | None] = []

    def record() -> None:
        seen.append(current_hold_token())

    with promotion_hold_token("token-a"):
        worker = threading.Thread(target=record)
        worker.start()
        worker.join()

    assert seen == ["token-a"]


# The check itself, before any other outcome.


def test_burned_version_refuses_into_an_empty_bin_dir_holding_only_store_state(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    burn_version(bin_dir, MEMBER, "1.0.0", now=NOW)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    with pytest.raises(BinaryReleaseVersionError, match="burned"):
        _check(source, bin_dir)

    assert not (bin_dir / MEMBER).exists()


def test_burned_version_refuses_when_the_installed_version_differs(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _binary(bin_dir / MEMBER, "1.0.0")
    burn_version(bin_dir, MEMBER, "1.0.1", now=NOW)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.1")

    with pytest.raises(BinaryReleaseVersionError, match="burned"):
        _check(source, bin_dir)


def test_burned_version_refuses_even_inside_the_hold_token(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _hold(bin_dir)
    burn_version(bin_dir, MEMBER, "1.0.0", now=NOW)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    with promotion_hold_token(token), pytest.raises(BinaryReleaseVersionError, match="burned"):
        _check(source, bin_dir)


def test_unexpired_hold_without_a_token_names_the_hold_task(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _hold(bin_dir)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    with pytest.raises(BinaryReleaseVersionError, match=HOLD_TASK):
        _check(source, bin_dir)


def test_unexpired_hold_with_a_wrong_token_names_the_hold_task(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _hold(bin_dir)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    with (
        promotion_hold_token("not-the-hold-token"),
        pytest.raises(BinaryReleaseVersionError, match=HOLD_TASK),
    ):
        _check(source, bin_dir)


def test_unexpired_hold_with_its_token_returns_the_candidate_identity(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _hold(bin_dir)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    with promotion_hold_token(token):
        identity = _check(source, bin_dir)

    assert identity == _candidate_identity(source, "1.0.0")


def test_check_without_any_store_returns_the_candidate_identity(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    assert _check(source, bin_dir) == _candidate_identity(source, "1.0.0")


def test_expired_hold_does_not_refuse(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Set two hours ago with a one-hour window, so it expired an hour before the check runs.
    started = datetime.now(UTC) - timedelta(hours=2)
    set_hold(
        bin_dir,
        task_id=HOLD_TASK,
        session_id=HOLD_SESSION,
        expires_at=started + timedelta(hours=1),
        now=started,
    )
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.0")

    assert _check(source, bin_dir) == _candidate_identity(source, "1.0.0")


def test_burn_of_another_version_does_not_refuse(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    burn_version(bin_dir, MEMBER, "1.0.0", now=NOW)
    source = _binary(tmp_path / "candidate" / MEMBER, "1.0.1")

    assert _check(source, bin_dir) == _candidate_identity(source, "1.0.1")


def test_hold_does_not_refuse_a_name_outside_the_managed_set(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _hold(bin_dir)
    source = _binary(tmp_path / "candidate" / "unmanaged-tool", "1.0.0")

    identity = _check(source, bin_dir, name="unmanaged-tool")

    assert identity == _candidate_identity(source, "1.0.0")


# Every promotion entry point refuses before it replaces the installed binary.


@pytest.mark.parametrize("promote", ENTRY_POINTS)
def test_hold_without_a_token_refuses_replacement_and_keeps_the_installed_bytes(
    workspace: Path, tmp_path: Path, promote: Promote
) -> None:
    bin_dir = tmp_path / "bin"
    original = _install_first(workspace, bin_dir, tmp_path)
    _hold(bin_dir)
    generations = _generation_dirs(bin_dir)
    candidate = _binary(tmp_path / "1.0.1" / MEMBER, "1.0.1")

    with pytest.raises(BinaryReleaseVersionError, match=HOLD_TASK):
        promote(workspace, candidate, bin_dir)

    assert (bin_dir / MEMBER).read_bytes() == original
    assert _generation_dirs(bin_dir) == generations


@pytest.mark.parametrize("promote", ENTRY_POINTS)
def test_burned_version_refuses_replacement_even_under_the_hold_token(
    workspace: Path, tmp_path: Path, promote: Promote
) -> None:
    bin_dir = tmp_path / "bin"
    original = _install_first(workspace, bin_dir, tmp_path)
    token = _hold(bin_dir)
    burn_version(bin_dir, MEMBER, "1.0.1", now=NOW)
    generations = _generation_dirs(bin_dir)
    candidate = _binary(tmp_path / "1.0.1" / MEMBER, "1.0.1")

    with promotion_hold_token(token), pytest.raises(BinaryReleaseVersionError, match="burned"):
        promote(workspace, candidate, bin_dir)

    assert (bin_dir / MEMBER).read_bytes() == original
    assert _generation_dirs(bin_dir) == generations


@pytest.mark.parametrize("promote", ENTRY_POINTS)
def test_hold_over_an_empty_bin_dir_refuses_the_first_install(
    workspace: Path, tmp_path: Path, promote: Promote
) -> None:
    bin_dir = tmp_path / "bin"
    _hold(bin_dir)
    generations = _generation_dirs(bin_dir)
    candidate = _binary(tmp_path / "1.0.0" / MEMBER, "1.0.0")

    with pytest.raises(BinaryReleaseVersionError, match=HOLD_TASK):
        promote(workspace, candidate, bin_dir)

    assert not (bin_dir / MEMBER).exists()
    assert _generation_dirs(bin_dir) == generations


@pytest.mark.parametrize("promote", PROMOTING_ENTRY_POINTS)
def test_hold_token_promotes_and_replaces_the_installed_bytes(
    workspace: Path, tmp_path: Path, promote: Promote
) -> None:
    bin_dir = tmp_path / "bin"
    _install_first(workspace, bin_dir, tmp_path)
    token = _hold(bin_dir)
    candidate = _binary(tmp_path / "1.0.1" / MEMBER, "1.0.1")

    with promotion_hold_token(token):
        promote(workspace, candidate, bin_dir)

    assert (bin_dir / MEMBER).read_bytes() == candidate.read_bytes()


# The replacement step checks again, so a precomputed identity cannot skip the hold or a burn.


def test_precomputed_identity_is_refused_under_a_hold_without_its_token(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    original = _install_first(workspace, bin_dir, tmp_path)
    _hold(bin_dir)
    candidate = _binary(tmp_path / "1.0.1" / MEMBER, "1.0.1")
    identity = BinaryReleaseIdentity(
        version="1.0.1", source_identity="precomputed-source", artifact_sha256=_sha(candidate)
    )

    with pytest.raises(BinaryReleaseVersionError, match=HOLD_TASK):
        promotion.stage_and_promote_binary_file(
            candidate, destination=bin_dir / MEMBER, identity=identity
        )

    assert (bin_dir / MEMBER).read_bytes() == original


def test_precomputed_identity_with_a_burned_version_is_refused(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    original = _install_first(workspace, bin_dir, tmp_path)
    bin_dir.mkdir(exist_ok=True)
    burn_version(bin_dir, MEMBER, "1.0.1", now=NOW)
    candidate = _binary(tmp_path / "1.0.1" / MEMBER, "1.0.1")
    identity = BinaryReleaseIdentity(
        version="1.0.1", source_identity="precomputed-source", artifact_sha256=_sha(candidate)
    )

    with pytest.raises(BinaryReleaseVersionError, match="burned"):
        promotion.stage_and_promote_binary_file(
            candidate, destination=bin_dir / MEMBER, identity=identity
        )

    assert (bin_dir / MEMBER).read_bytes() == original


# Workspace set promotion. A refusal from the first preflight surfaces unwrapped, so these
# assert the hold or burn text and the unchanged state rather than a wrapper type.


def _candidates(root: Path, version: str) -> dict[str, Path]:
    return {name: _binary(root / version / name, version) for name in SET_MEMBERS}


def _bump_set(workspace: Path) -> None:
    for name in SET_MEMBERS:
        manifest = workspace / "crates" / name / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace("1.0.0", "1.0.1"))


def _install_set(workspace: Path, bin_dir: Path, root: Path) -> dict[str, bytes | None]:
    promote_workspace_binary_set(
        _candidates(root, "1.0.0"), bin_dir=bin_dir, workspace_root=workspace
    )
    return _installed_set(bin_dir)


def _installed_set(bin_dir: Path) -> dict[str, bytes | None]:
    return {name: _installed(bin_dir / name) for name in SET_MEMBERS}


def test_set_hold_without_a_token_refuses_before_replacing_any_member(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    original = _install_set(workspace, bin_dir, tmp_path)
    _hold(bin_dir)
    generations = _generation_dirs(bin_dir)
    _bump_set(workspace)

    with pytest.raises(RuntimeError, match=HOLD_TASK):
        promote_workspace_binary_set(
            _candidates(tmp_path, "1.0.1"), bin_dir=bin_dir, workspace_root=workspace
        )

    assert _installed_set(bin_dir) == original
    assert _generation_dirs(bin_dir) == generations


def test_set_burned_member_refuses_the_whole_set_even_under_the_hold_token(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    original = _install_set(workspace, bin_dir, tmp_path)
    token = _hold(bin_dir)
    burn_version(bin_dir, "gcode", "1.0.1", now=NOW)
    generations = _generation_dirs(bin_dir)
    _bump_set(workspace)

    with promotion_hold_token(token), pytest.raises(RuntimeError, match="burned"):
        promote_workspace_binary_set(
            _candidates(tmp_path, "1.0.1"), bin_dir=bin_dir, workspace_root=workspace
        )

    assert _installed_set(bin_dir) == original
    assert _generation_dirs(bin_dir) == generations


def test_set_hold_over_an_empty_bin_dir_refuses_the_first_set_install(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    _hold(bin_dir)
    generations = _generation_dirs(bin_dir)

    with pytest.raises(RuntimeError, match=HOLD_TASK):
        promote_workspace_binary_set(
            _candidates(tmp_path, "1.0.0"), bin_dir=bin_dir, workspace_root=workspace
        )

    assert _installed_set(bin_dir) == dict.fromkeys(SET_MEMBERS)
    assert _generation_dirs(bin_dir) == generations


def test_set_under_the_hold_token_replaces_every_member(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install_set(workspace, bin_dir, tmp_path)
    token = _hold(bin_dir)
    _bump_set(workspace)
    candidates = _candidates(tmp_path, "1.0.1")

    with promotion_hold_token(token):
        promote_workspace_binary_set(candidates, bin_dir=bin_dir, workspace_root=workspace)

    assert _installed_set(bin_dir) == {name: candidates[name].read_bytes() for name in SET_MEMBERS}


def test_set_under_the_hold_token_retains_one_generation_and_records_the_source_commit(
    workspace: Path, tmp_path: Path
) -> None:
    head = _commit_all(workspace, "base")
    bin_dir = tmp_path / "bin"
    _install_set(workspace, bin_dir, tmp_path)
    token = _hold(bin_dir)
    _bump_set(workspace)

    with promotion_hold_token(token):
        promote_workspace_binary_set(
            _candidates(tmp_path, "1.0.1"), bin_dir=bin_dir, workspace_root=workspace
        )

    # The first install retains nothing, so the outgoing set of the second promotion is seq 1.
    assert [generation.seq for generation in list_generations(bin_dir)] == [1]
    installed = read_state(bin_dir)["installed"]
    assert {name: installed[name]["source_commit"] for name in SET_MEMBERS} == dict.fromkeys(
        SET_MEMBERS, head
    )
