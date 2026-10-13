"""Cargo launched from a reviewer seat must build the requested checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from gobby.agents.cargo_target import cargo_release_dir
from gobby.agents.sandbox_cargo import sandbox_cargo_environment


@dataclass
class CargoLauncher:
    workspace: Path
    target: Path
    assets: Path
    node: Path
    env: dict[str, str]

    def run(self, args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        cargo = shutil.which("cargo", path=self.env["PATH"])
        assert cargo is not None
        return subprocess.run(
            [cargo, *args],
            cwd=cwd or self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def launcher(tmp_path: Path) -> CargoLauncher:
    node = shutil.which("node")
    assert node is not None
    workspace = tmp_path / "seat"
    assets = tmp_path / "assets"
    target = tmp_path / "target"
    bin_dir = tmp_path / "bin"
    for directory in (workspace, assets, target, bin_dir):
        directory.mkdir()
    fake_cargo = bin_dir / "cargo"
    fake_cargo.write_text(
        f"#!{node}\n"
        + """
const args = process.argv.slice(2);
if (args.includes('locate-project')) {
  const index = args.indexOf('--manifest-path');
  const directory = args.indexOf('-C');
  const cwd = directory < 0 ? process.cwd() : args[directory + 1];
  console.log(index < 0 ? `${cwd}/Cargo.toml` : args[index + 1]);
} else {
  const target = process.env.CARGO_TARGET_DIR || process.env.CARGO_BUILD_TARGET_DIR;
  console.log(JSON.stringify({args, target, target_directory: target}));
  if (args.includes('--fail')) process.exit(23);
}
""",
        encoding="utf-8",
    )
    fake_cargo.chmod(0o700)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    env["CARGO_TARGET_DIR"] = str(target)
    env.pop("CARGO_BUILD_TARGET_DIR", None)
    env.update(
        sandbox_cargo_environment(
            assets=assets,
            node=Path(node),
            workspace=workspace,
            target=target,
            env=env,
        )
    )
    return CargoLauncher(workspace, target, assets, Path(node), env)


@pytest.mark.unit
@pytest.mark.parametrize("equals", [False, True])
def test_manifest_selects_isolated_warm_target(
    launcher: CargoLauncher,
    tmp_path: Path,
    equals: bool,
) -> None:
    reviewed = tmp_path / "reviewed checkout"
    other = tmp_path / "other checkout"
    reviewed.mkdir()
    other.mkdir()
    manifest = str(reviewed / "Cargo.toml")
    args = [
        "+stable",
        "test",
        *([f"--manifest-path={manifest}"] if equals else ["--manifest-path", manifest]),
        "--",
        "--nocapture",
    ]
    first = launcher.run(args)
    second = launcher.run(args)
    different = launcher.run(["test", "--manifest-path", str(other / "Cargo.toml")])
    assert first.returncode == second.returncode == different.returncode == 0
    result = json.loads(first.stdout)
    chosen = Path(result["target"])
    assert chosen.is_relative_to(launcher.target)
    assert chosen != launcher.target
    assert cargo_release_dir(reviewed, env=launcher.env) == chosen / "release"
    assert result["args"] == args
    assert json.loads(second.stdout)["target"] == str(chosen)
    assert json.loads(different.stdout)["target"] != str(chosen)


@pytest.mark.unit
def test_cwd_selects_checkout_and_program_arguments_are_untouched(
    launcher: CargoLauncher,
    tmp_path: Path,
) -> None:
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    local = launcher.run(["test"])
    foreign = launcher.run(["run", "--", "--manifest-path", "unrelated"], cwd=reviewed)
    failed = launcher.run(["test", "--fail"])
    assert json.loads(local.stdout)["target"] == str(launcher.target)
    assert Path(json.loads(foreign.stdout)["target"]).is_relative_to(launcher.target)
    assert json.loads(foreign.stdout)["target"] != str(launcher.target)
    assert json.loads(foreign.stdout)["args"] == ["run", "--", "--manifest-path", "unrelated"]
    assert failed.returncode == 23


@pytest.mark.unit
def test_nested_workspace_uses_checkouts_existing_warm_target(launcher: CargoLauncher) -> None:
    (launcher.workspace / ".git").write_text("gitdir: elsewhere\n")
    crates = launcher.workspace / "crates"
    crates.mkdir()
    result = launcher.run(["test", "--manifest-path", str(crates / "Cargo.toml")])
    assert result.returncode == 0
    assert json.loads(result.stdout)["target"] == str(launcher.target)
    assert cargo_release_dir(crates, env=launcher.env) == launcher.target / "release"


@pytest.mark.unit
def test_cargo_directory_option_selects_reviewed_checkout(
    launcher: CargoLauncher,
    tmp_path: Path,
) -> None:
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    args = ["+nightly", "-Zunstable-options", "-C", str(reviewed), "test"]
    result = launcher.run(args)
    assert result.returncode == 0
    chosen = Path(json.loads(result.stdout)["target"])
    assert chosen != launcher.target
    assert cargo_release_dir(reviewed, env=launcher.env) == chosen / "release"


@pytest.mark.unit
def test_explicit_target_selection_remains_cargos_argument(launcher: CargoLauncher) -> None:
    args = ["test", "--target-dir", str(launcher.target / "explicit")]
    result = launcher.run(args)
    assert result.returncode == 0
    assert json.loads(result.stdout)["args"] == args


@pytest.mark.unit
def test_no_cargo_leaves_environment_unchanged(tmp_path: Path) -> None:
    assert (
        sandbox_cargo_environment(
            assets=tmp_path,
            node=tmp_path / "node",
            workspace=tmp_path,
            target=tmp_path / "target",
            env={"PATH": ""},
        )
        == {}
    )


@pytest.mark.unit
def test_resume_replaces_previous_run_launcher(launcher: CargoLauncher, tmp_path: Path) -> None:
    new_assets = tmp_path / "new-run" / "assets"
    new_assets.mkdir(parents=True)
    env = sandbox_cargo_environment(
        assets=new_assets,
        node=launcher.node,
        workspace=launcher.workspace,
        target=launcher.target,
        env=launcher.env,
    )
    assert str(launcher.assets / "cargo-bin") not in env["PATH"].split(os.pathsep)
    launcher.env.update(env)
    assert launcher.run(["test"]).returncode == 0
    # A repeated preparation of the same run must replace its read-only assets.
    launcher.env.update(
        sandbox_cargo_environment(
            assets=new_assets,
            node=launcher.node,
            workspace=launcher.workspace,
            target=launcher.target,
            env=launcher.env,
        )
    )
    assert launcher.run(["test"]).returncode == 0


@pytest.mark.unit
@pytest.mark.parametrize("variable", ["CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"])
def test_caller_target_override_is_preserved(launcher: CargoLauncher, variable: str) -> None:
    explicit = launcher.workspace / "caller-target"
    launcher.env.pop("CARGO_TARGET_DIR", None)
    launcher.env[variable] = str(explicit)
    result = launcher.run(["metadata", "--no-deps", "--format-version", "1"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["target_directory"] == str(explicit)
    assert cargo_release_dir(launcher.workspace, env=launcher.env) == explicit / "release"


@pytest.mark.unit
def test_build_target_override_preserves_cargo_target_precedence(launcher: CargoLauncher) -> None:
    reviewed = launcher.workspace / "reviewed"
    reviewed.mkdir()
    launcher.env["CARGO_BUILD_TARGET_DIR"] = str(launcher.workspace / "caller-target")
    result = launcher.run(["metadata"], cwd=reviewed)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["target_directory"] == str(launcher.target)
    assert cargo_release_dir(reviewed, env=launcher.env) == launcher.target / "release"


def _checkout(root: Path, *, extra_test: bool) -> Path:
    member = root / "member"
    (member / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[workspace]\nmembers=["member"]\nresolver="2"\n')
    (member / "Cargo.toml").write_text(
        '[package]\nname="review_target_probe"\nversion="0.1.0"\nedition="2021"\n'
    )
    source = "#[test]\nfn first() {}\n"
    if extra_test:
        source += "#[test]\nfn candidate_only() {}\n"
    (member / "src" / "lib.rs").write_text(source)
    for path in root.rglob("*"):
        os.utime(path, (1_700_000_000, 1_700_000_000))
    return root / "Cargo.toml"


@pytest.mark.integration
def test_real_cargo_stale_checkout_and_incremental_rebuild(tmp_path: Path) -> None:
    """Prove the old shared target is wrong, then prove isolation and warm reuse."""
    cargo = shutil.which("cargo")
    node = shutil.which("node")
    assert cargo is not None and node is not None
    first_manifest = _checkout(tmp_path / "first", extra_test=False)
    second_manifest = _checkout(tmp_path / "second", extra_test=True)
    seat = tmp_path / "seat"
    assets = tmp_path / "assets"
    target = tmp_path / "target"
    home = tmp_path / "cargo-home"
    temp = tmp_path / "temp"
    for directory in (seat, assets, target, home, temp):
        directory.mkdir()
    env = {
        **os.environ,
        "CARGO_HOME": str(home),
        "CARGO_TARGET_DIR": str(target),
        "CARGO_BUILD_JOBS": "3",
        "TMPDIR": str(temp),
    }
    env.pop("CARGO_BUILD_TARGET_DIR", None)
    args = ["test", "--offline", "--manifest-path"]
    old_first = subprocess.run(
        [cargo, *args, str(first_manifest)],
        env=env,
        cwd=seat,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    old_second = subprocess.run(
        [cargo, *args, str(second_manifest)],
        env=env,
        cwd=seat,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert old_first.returncode == old_second.returncode == 0
    assert "running 1 test" in old_second.stdout, old_second.stdout
    assert "candidate_only" not in old_second.stdout
    env.update(
        sandbox_cargo_environment(
            assets=assets,
            node=Path(node),
            workspace=seat,
            target=target,
            env=env,
        )
    )
    launcher = CargoLauncher(seat, target, assets, Path(node), env)
    fixed_first = launcher.run([*args, str(first_manifest)])
    fixed_second = launcher.run([*args, str(second_manifest)])
    warm = launcher.run([*args, str(second_manifest)])
    assert fixed_first.returncode == fixed_second.returncode == warm.returncode == 0
    assert "running 1 test" in fixed_first.stdout
    assert "running 2 tests" in fixed_second.stdout
    assert "candidate_only" in fixed_second.stdout
    assert "Compiling review_target_probe" in fixed_second.stderr
    assert "Compiling" not in warm.stderr, warm.stderr
    print(f"unchanged reviewed checkout: {warm.stderr.strip()}")
    locks = list((target / "reviewed-checkouts").glob("*/debug/.cargo-build-lock"))
    assert len(locks) == 2
    for variable in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"):
        explicit = tmp_path / variable.lower()
        launcher.env.pop("CARGO_TARGET_DIR", None)
        launcher.env.pop("CARGO_BUILD_TARGET_DIR", None)
        launcher.env[variable] = str(explicit)
        metadata = launcher.run(
            [
                "metadata",
                "--no-deps",
                "--format-version",
                "1",
                "--manifest-path",
                str(second_manifest),
            ]
        )
        assert metadata.returncode == 0, metadata.stderr
        assert json.loads(metadata.stdout)["target_directory"] == str(explicit)
        assert cargo_release_dir(second_manifest.parent, env=launcher.env) == explicit / "release"


@pytest.mark.integration
def test_reviewed_target_build_lock_is_writable(launcher: CargoLauncher, tmp_path: Path) -> None:
    """Use an inherited SRT grant, or a target-only native grant on an unwrapped host."""
    ambient_target = Path(os.environ.get("CARGO_TARGET_DIR", ""))
    inherited_srt = (
        ambient_target.parent.name == "cargo-target"
        and ambient_target.parent.parent.name == "sandbox"
    )
    # A seat cannot apply a second macOS sandbox. Probe within an isolated,
    # automatically removed child of its actual target grant instead.
    with TemporaryDirectory(
        prefix="gobby-lock-proof-", dir=ambient_target if inherited_srt else tmp_path
    ) as namespace:
        target = Path(namespace)
        assets = tmp_path / "lock-assets"
        assets.mkdir()
        env = {
            **launcher.env,
            "CARGO_TARGET_DIR": str(target),
            **sandbox_cargo_environment(
                assets=assets,
                node=launcher.node,
                workspace=launcher.workspace,
                target=target,
                env=launcher.env,
            ),
        }
        reviewed = tmp_path / "reviewed"
        reviewed.mkdir()
        probe = CargoLauncher(launcher.workspace, target, assets, launcher.node, env)
        result = probe.run(["test", "--manifest-path", str(reviewed / "Cargo.toml")])
        assert result.returncode == 0, result.stderr
        chosen = Path(json.loads(result.stdout)["target"])
        assert chosen.is_relative_to(target) and chosen != target
        assert cargo_release_dir(reviewed, env=env) == chosen / "release"
        lock = chosen / "debug" / ".cargo-build-lock"
        lock.parent.mkdir(parents=True)
        command = [
            str(launcher.node),
            "-e",
            "require('fs').writeFileSync(process.argv[1], '')",
            str(lock),
        ]
        if sys.platform == "darwin" and not inherited_srt:
            # Exercise the target-only write grant when a new sandbox is allowed.
            profile = "(version 1)(allow default)(deny file-write*)(allow file-write* " + (
                f"(subpath {json.dumps(str(target))}))"
            )
            command = ["/usr/bin/sandbox-exec", "-p", profile, *command]
        write = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert write.returncode == 0, write.stderr
        assert lock.is_file()
        print(f"reviewed Cargo lock writable: {lock}; inherited_srt={inherited_srt}")
