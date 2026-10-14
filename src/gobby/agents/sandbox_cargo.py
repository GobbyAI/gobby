"""Materialize a Cargo launcher within a sandbox's read-only run assets."""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path


def sandbox_cargo_environment(
    *, assets: Path, node: Path, workspace: Path, target: Path, env: Mapping[str, str]
) -> dict[str, str]:
    """Route cross-checkout builds below the existing writable target grant."""
    # Resume may carry the previous run's PATH. Never chain its read-only shim,
    # whose assets belong to another run and are outside this run's read grant.
    search_path = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if not (Path(entry).name == "cargo-bin" and Path(entry).parent.name == "assets")
    )
    cargo = shutil.which("cargo", path=search_path)
    if cargo is None:
        return {}
    runner = assets / "cargo-runner.mjs"
    runner.unlink(missing_ok=True)
    shutil.copyfile(Path(__file__).with_name("sandbox_cargo_runner.mjs"), runner)
    runner.chmod(0o400)
    directory = assets / "cargo-bin"
    directory.mkdir(mode=0o700, exist_ok=True)
    launcher = directory / "cargo"
    launcher.unlink(missing_ok=True)
    arguments = [str(node), str(runner), cargo, str(workspace.resolve()), str(target)]
    launcher.write_text(f'#!/bin/sh\nexec {shlex.join(arguments)} "$@"\n', encoding="utf-8")
    launcher.chmod(0o500)
    return {
        **_cargo_shell_environment(assets=assets, directory=directory, env=env),
        "PATH": f"{directory}{os.pathsep}{search_path}",
        "GOBBY_SANDBOX_CARGO_WORKSPACE": str(workspace.resolve()),
        "GOBBY_SANDBOX_CARGO_TARGET": str(target),
    }


def _cargo_shell_environment(
    *, assets: Path, directory: Path, env: Mapping[str, str]
) -> dict[str, str]:
    """Restore the launcher after noninteractive bash and zsh startup files."""
    shell_home = assets / "cargo-shell"
    shell_home.mkdir(mode=0o700, exist_ok=True)
    original_bash = env.get("GOBBY_CARGO_ORIGINAL_BASH_ENV", env.get("BASH_ENV", ""))
    original_zsh = env.get("GOBBY_CARGO_ORIGINAL_ZDOTDIR", env.get("ZDOTDIR", ""))
    bash_env = shell_home / "bash-env"
    restore_path = f'export PATH={shlex.quote(str(directory))}:"$PATH"\n'
    bash_content = ""
    if original_bash:
        original = shlex.quote(original_bash)
        bash_content = f"if [ -r {original} ]; then . {original}; fi\n"
    restore_bash = f"export BASH_ENV={shlex.quote(str(bash_env))}\n"
    _write_shell_startup(bash_env, bash_content + restore_path + restore_bash)
    for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zlogout"):
        # Respect a caller's ZDOTDIR and the usual HOME fallback, without copying
        # or modifying user startup files. Reset ZDOTDIR after each phase so the
        # following phase also passes through this run's protected overlay.
        original = f'"${{GOBBY_CARGO_ORIGINAL_ZDOTDIR:-$HOME}}/{name}"'
        content = (
            'if [ -n "$GOBBY_CARGO_ORIGINAL_ZDOTDIR" ]; then\n'
            '  export ZDOTDIR="$GOBBY_CARGO_ORIGINAL_ZDOTDIR"\n'
            "else\n  unset ZDOTDIR\nfi\n"
            + f"if [ -r {original} ]; then . {original}; fi\n"
            + 'export GOBBY_CARGO_ORIGINAL_ZDOTDIR="${ZDOTDIR-}"\n'
        )
        # Logout leaves the user's directory in place for the system logout file.
        if name != ".zlogout":
            content += (
                restore_path + f"export ZDOTDIR={shlex.quote(str(shell_home))}\n" + restore_bash
            )
        _write_shell_startup(shell_home / name, content)
    return {
        "BASH_ENV": str(bash_env),
        "ZDOTDIR": str(shell_home),
        "GOBBY_CARGO_ORIGINAL_BASH_ENV": original_bash,
        "GOBBY_CARGO_ORIGINAL_ZDOTDIR": original_zsh,
    }


def _write_shell_startup(path: Path, content: str) -> None:
    path.unlink(missing_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o400)
