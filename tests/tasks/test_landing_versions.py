"""Version generation uses Git objects without modifying any checkout."""

from __future__ import annotations

import asyncio
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from gobby.install import bin_release_identity as release
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.close_receipts import list_close_receipts, record_close_receipt
from gobby.tasks.closure_snapshot import prepare_closure
from gobby.tasks.landing_versions import generate_version_bump, verify_version_bump
from tests.fixtures.landing_case import LandingCase, make_landing_case

pytestmark = pytest.mark.unit


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, files: Mapping[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "0.5.0")
    git(repo, "config", "user.email", "fixture@example.test")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "core.hooksPath", str(tmp_path / "empty-hooks"))
    monkeypatch.setattr(release, "RELEASE_CRATES", {"alpha": "alpha", "beta": "beta"})
    base = commit(
        repo,
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
            "Cargo.lock": 'version = 4\n\n[[package]]\nname = "alpha"\nversion = "1.2.3"\n\n'
            '[[package]]\nname = "beta"\nversion = "2.3.4"\n',
            "crates/alpha/Cargo.toml": '[package]\nname = "alpha"\nversion = "1.2.3"\n',
            "crates/alpha/src/main.rs": "fn main() {}\n",
            "crates/beta/Cargo.toml": '[package]\nname = "beta"\nversion = "2.3.4"\n',
            "crates/beta/src/main.rs": "fn main() {}\n",
            "src/gobby/install/version_pins.py": "MANAGED_BIN_VERSION_PINS = {\n"
            '    "alpha": "1.2.3",\n    "beta": "2.3.4",\n}\n',
        },
    )
    git(repo, "switch", "-c", "candidate")
    candidate = commit(repo, {"crates/alpha/src/main.rs": 'fn main() { println!("new"); }\n'})
    return repo, base, candidate


def test_generation_is_version_only_and_preserves_checkout(
    workspace: tuple[Path, str, str],
) -> None:
    repo, base, candidate = workspace
    before_index = git(repo, "write-tree")
    bump = generate_version_bump(
        repo, base, candidate, {"crates/alpha/src/main.rs"}, task_ref="#42"
    )
    assert bump is not None, "a binary source change must generate a version commit"
    assert git(repo, "rev-parse", f"{bump}^") == candidate
    assert git(repo, "diff", "--name-only", candidate, bump).splitlines() == [
        "Cargo.lock",
        "crates/alpha/Cargo.toml",
        "src/gobby/install/version_pins.py",
    ]
    assert 'version = "1.2.4"' in git(repo, "show", f"{bump}:crates/alpha/Cargo.toml")
    assert 'version = "1.2.4"' in git(repo, "show", f"{bump}:Cargo.lock")
    assert '"alpha": "1.2.4"' in git(repo, "show", f"{bump}:src/gobby/install/version_pins.py")
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert git(repo, "write-tree") == before_index
    assert git(repo, "status", "--porcelain") == ""
    assert verify_version_bump(repo, base, candidate, bump)


def test_verification_rejects_source_edits_and_descendants(
    workspace: tuple[Path, str, str],
) -> None:
    repo, base, candidate = workspace
    bump = generate_version_bump(
        repo, base, candidate, {"crates/alpha/src/main.rs"}, task_ref="#42"
    )
    assert bump is not None
    git(repo, "switch", "--detach", bump)
    altered = commit(repo, {"crates/alpha/src/main.rs": "fn main() { panic!(); }\n"})
    assert not verify_version_bump(repo, base, candidate, altered)
    assert not verify_version_bump(repo, candidate, candidate, bump)
    assert not verify_version_bump(repo, base, base, bump)


def test_nonbinary_change_needs_no_bump(workspace: tuple[Path, str, str]) -> None:
    repo, base, _ = workspace
    git(repo, "switch", "--detach", base)
    candidate = commit(repo, {"README.md": "documentation\n"})
    assert generate_version_bump(repo, base, candidate, {"README.md"}, task_ref="#42") is None


@pytest.mark.parametrize(
    "path,content",
    [
        ("crates/alpha/Cargo.toml", '[package]\nname = "alpha"\nversion = "9.9.9"\n'),
        ("unrelated.txt", "injected\n"),
        ("crates/alpha/src/main.rs", "fn main() { panic!(); }\n"),
    ],
)
def test_matching_provenance_and_parent_cannot_hide_tampering(
    workspace: tuple[Path, str, str],
    path: str,
    content: str,
) -> None:
    repo, base, candidate = workspace
    bump = generate_version_bump(
        repo, base, candidate, {"crates/alpha/src/main.rs"}, task_ref="#42"
    )
    assert bump is not None
    message = git(repo, "show", "-s", "--format=%B", bump)
    git(repo, "switch", "--detach", bump)
    altered = commit(repo, {path: content})
    forged = git(repo, "commit-tree", f"{altered}^{{tree}}", "-p", candidate, "-m", message)
    assert not verify_version_bump(repo, base, candidate, forged)


def test_dependency_closure_bumps_every_affected_binary(
    workspace: tuple[Path, str, str],
) -> None:
    repo, base, _ = workspace
    git(repo, "switch", "--detach", base)
    base = commit(
        repo,
        {
            "crates/alpha/Cargo.toml": '[package]\nname = "alpha"\nversion = "1.2.3"\n\n'
            '[dependencies]\nbeta = { path = "../beta" }\n',
        },
    )
    candidate = commit(repo, {"crates/beta/src/main.rs": "fn main() { todo!(); }\n"})
    bump = generate_version_bump(repo, base, candidate, {"crates/beta/src/main.rs"}, task_ref="#42")
    assert bump is not None
    assert 'version = "1.2.4"' in git(repo, "show", f"{bump}:crates/alpha/Cargo.toml")
    assert 'version = "2.3.5"' in git(repo, "show", f"{bump}:crates/beta/Cargo.toml")
    assert tomllib.loads(git(repo, "show", f"{bump}:crates/alpha/Cargo.toml"))["dependencies"] == {
        "beta": {"path": "../beta"}
    }
    assert verify_version_bump(repo, base, candidate, bump)


@pytest.mark.parametrize("version", ["1.2.4", "1.3.0", "1.2.2", "1.2.3-rc.1"])
def test_source_candidate_cannot_choose_binary_versions(
    workspace: tuple[Path, str, str],
    version: str,
) -> None:
    repo, base, _ = workspace
    candidate = commit(
        repo, {"crates/alpha/Cargo.toml": f'[package]\nname = "alpha"\nversion = "{version}"\n'}
    )
    with pytest.raises(release.BinaryReleaseVersionError, match="owned by landing"):
        generate_version_bump(
            repo,
            base,
            candidate,
            {"crates/alpha/src/main.rs", "crates/alpha/Cargo.toml"},
            task_ref="#42",
        )
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("version", ["1.2.3-rc.1", "1.2.3+build.1", "01.2.3", "1.2.03"])
def test_generation_fails_closed_on_invalid_base_versions(
    workspace: tuple[Path, str, str],
    version: str,
) -> None:
    repo, base, _ = workspace
    git(repo, "switch", "--detach", base)
    base = commit(
        repo, {"crates/alpha/Cargo.toml": f'[package]\nname = "alpha"\nversion = "{version}"\n'}
    )
    candidate = commit(repo, {"crates/alpha/src/main.rs": "fn main() { todo!(); }\n"})
    with pytest.raises(release.BinaryReleaseVersionError, match="invalid binary version"):
        generate_version_bump(repo, base, candidate, {"crates/alpha/src/main.rs"}, task_ref="#42")
    assert git(repo, "rev-parse", "HEAD") == candidate


@pytest.fixture
def integration_workspace(
    workspace: tuple[Path, str, str],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> tuple[LandingCase, str]:
    repo, base, _ = workspace
    case = make_landing_case(temp_db, sample_project, tmp_path)
    files = {
        path: git(repo, "show", f"{base}:{path}") + "\n"
        for path in git(repo, "ls-tree", "-r", "--name-only", base).splitlines()
    }
    files["crates/alpha/src/lib.rs"] = "pub fn value() -> usize { 1 }\n"
    integration_base = case.direct(files)
    return case, integration_base


async def test_sequential_landings_bump_same_binary_without_branch_version_edits(
    integration_workspace: tuple[LandingCase, str],
    temp_db: HubDatabase,
) -> None:
    case, integration_base = integration_workspace
    first = case.candidate(
        "first", {"crates/alpha/src/main.rs": "fn main() { todo!(); }\n"}, base=integration_base
    )
    second = case.candidate(
        "second",
        {"crates/alpha/src/lib.rs": "pub fn value() -> usize { 2 }\n"},
        base=integration_base,
    )
    first_task = case.reviewed(first)
    second_task = case.reviewed(second)
    case.git("branch", "-m", "trunk", "0.5.0")

    landed_first = await case.land(first_task, first)
    assert landed_first["landed"] is True, landed_first
    assert 'version = "1.2.4"' in case.git("show", "HEAD:crates/alpha/Cargo.toml")
    assert 'version = "1.2.3"' in case.git("show", "first:crates/alpha/Cargo.toml")
    first_closure = await asyncio.to_thread(
        prepare_closure,
        temp_db,
        task_id=first_task.id,
        session_id=case.claimant,
        facts={"candidate_commit_sha": landed_first["landed_tip"]},
    )
    first_receipts = first_closure.snapshot.facts["landing_receipts"]
    assert isinstance(first_receipts, list)
    assert isinstance(first_receipts[0], dict)
    first_facts = first_receipts[0]["facts"]
    assert isinstance(first_facts, dict)
    assert first_facts.get("verified_version_transform") is True
    landed_second = await case.land(second_task, second)
    assert landed_second["landed"] is True, landed_second
    assert 'version = "1.2.5"' in case.git("show", "HEAD:crates/alpha/Cargo.toml")
    assert 'version = "1.2.3"' in case.git("show", "second:crates/alpha/Cargo.toml")
    before_retry = case.git("rev-parse", "HEAD")
    replayed = await case.land(second_task, second)
    assert replayed["landed"] is True
    assert case.git("rev-parse", "HEAD") == before_retry
    receipt = list_close_receipts(temp_db, second_task.id)[-1]
    assert receipt.facts["reviewed_sha"] == second
    assert receipt.facts["version_bump_commit"] == before_retry
    assert len(list_close_receipts(temp_db, second_task.id)) == 1
    prepared = await asyncio.to_thread(
        prepare_closure,
        temp_db,
        task_id=second_task.id,
        session_id=case.claimant,
        facts={"candidate_commit_sha": before_retry},
    )
    admitted = prepared.snapshot.facts["landing_receipts"]
    assert isinstance(admitted, list)
    assert isinstance(admitted[0], dict)
    admitted_facts = admitted[0]["facts"]
    assert isinstance(admitted_facts, dict)
    # Proven merge provenance permits recovery, but its added source needs fresh evidence.
    assert "verified_version_transform" not in admitted_facts

    forged_task = case.task("Forged transformation receipt")
    record_close_receipt(
        temp_db,
        task=forged_task,
        author_session_id=case.claimant,
        commit_sha=second,
        facts={**receipt.facts, "integration_base": first},
    )
    forged = await asyncio.to_thread(
        prepare_closure,
        temp_db,
        task_id=forged_task.id,
        session_id=case.claimant,
        facts={"candidate_commit_sha": before_retry},
    )
    assert forged.snapshot.facts["landing_receipts"] == []


async def test_missing_receipt_recovers_verified_bump_without_bumping_again(
    integration_workspace: tuple[LandingCase, str],
    temp_db: HubDatabase,
) -> None:
    case, base = integration_workspace
    candidate = case.candidate(
        "candidate", {"crates/alpha/src/lib.rs": "pub fn changed() {}\n"}, base=base
    )
    task = case.reviewed(candidate)
    case.git("branch", "-m", "trunk", "0.5.0")
    with patch(
        "gobby.tasks.land_commit.record_close_receipt",
        side_effect=psycopg.OperationalError("lost receipt"),
    ):
        landed = await case.land(task, candidate)
    assert landed["landed"] is True
    assert landed["receipt_pending"] is True
    bump = case.git("rev-parse", "HEAD")
    recovered = await case.land(task, candidate)
    assert recovered["landed"] is True
    assert case.git("rev-parse", "HEAD") == bump
    assert 'version = "1.2.4"' in case.git("show", "HEAD:crates/alpha/Cargo.toml")
    receipt = list_close_receipts(temp_db, task.id)[0]
    assert receipt.facts["version_bump_commit"] == bump
    assert receipt.facts["reviewed_sha"] == candidate
