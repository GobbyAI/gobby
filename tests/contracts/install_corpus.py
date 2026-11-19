"""Record the install golden tree: what `gobby install` writes on a temporary HOME.

The recorder takes each of the six components claude, codex, grok, droid, agy and
git-hooks on its own fresh root (``<root>/<component>``): a temporary HOME, a temporary
GOBBY_HOME and a temporary git project, where it installs that one component and then
uninstalls it. A component's install entries are therefore every path it writes, never
a delta over another component's install. The provider CLIs are absent by design: PATH
holds a recorder-owned empty directory and the directory of git only.

Golden document (``tests/contracts/install/golden_tree.json``)::

    {
      "components": ["claude", "codex", "grok", "droid", "agy", "git-hooks"],
      "phases": {
        "install": {"<component>": [<install entry>, ...]},
        "uninstall": {"<component>": [<uninstall entry>, ...]}
      }
    }

Paths are relative to a placeholder root: ``@HOME@``, ``@PROJECT@``, ``@INSTALL_DIR@``
or ``@PYTHON@``, so no host path is written. Entries of each component are sorted by
path.

An install entry is the state of one path that the component's install step created or
changed: ``path``, ``mode`` (an octal string such as ``"0644"``, recorded on POSIX
and omitted on Windows) and
exactly one content key. ``text`` holds UTF-8 content after placeholder normalization,
``sha256`` the digest of other bytes, and ``source`` names the file under
``src/gobby/install/shared/`` whose bytes the installed file equals. A ``source``
entry is compared by path, mode and source: a replay checks that the installed bytes
equal the bundled file it names, so bundled content is checked, not excluded, and its
bytes are not copied into the golden.

An uninstall entry gives the disposition of one path after that component's uninstall
step, relative to the state its install step left. Every path of the component's install
entries has one: ``removed`` (absent after the step), ``kept`` (unchanged) or
``modified`` (content or mode changed). A path the uninstall step creates that its
install entries do not name is ``created``. ``kept``, ``modified`` and ``created``
entries carry ``mode`` and one content key, as install entries do; ``removed``
entries carry neither.

``golden_differences`` returns one line per differing leaf of the keyed view, in which
each component's entries are keyed by path. Each line starts with the RFC 6901 JSON
pointer of that leaf (for example
``/phases/install/claude/@HOME@~1.claude~1settings.json/mode``) followed by ``": "``.
A missing or extra entry is named by the pointer of the entry itself.

Recording conditions, beyond HOME and PATH: the environment is cleared of every
variable that redirects an installer (the Gobby, provider CLI, git and XDG prefixes),
git reads no system or global config, the umask is 022, and ``time.time`` returns a
fixed instant during the install step and one second later during the uninstall step,
because the installers name their backups by the clock and real steps do not share a
second. The existing install that component runs need is a ghook version stamp under
each temporary GOBBY_HOME, holding the workspace ghook crate version, since no ghook
binary is on PATH. The recording root is resolved first, so it has one spelling. Every
step must report success; a failed step stops the recording.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOT = REPO_ROOT / "src" / "gobby" / "install" / "shared"
INSTALL_DIR = Path(__file__).resolve().parent / "install"
GOLDEN_PATH = INSTALL_DIR / "golden_tree.json"
README_PATH = INSTALL_DIR / "README.md"
INSTALL_COMPONENTS: tuple[str, ...] = ("claude", "codex", "grok", "droid", "agy", "git-hooks")
INSTALL_DISPOSITIONS: tuple[str, ...] = ("removed", "kept", "modified")
CREATED_DISPOSITION = "created"
CONTENT_KEYS: tuple[str, ...] = ("text", "sha256", "source")
PLACEHOLDERS: tuple[str, ...] = ("@HOME@", "@PROJECT@", "@INSTALL_DIR@", "@PYTHON@")

# The installers name their backups by int(time.time()); 2026-01-01T00:00:00Z.
RECORDING_CLOCK = 1_767_225_600.0
RECORDING_UMASK = 0o022
# Environment variables with these prefixes redirect an installer, git or a CLI's
# config home, so none of them reaches a recording.
CLEARED_ENV_PREFIXES: tuple[str, ...] = (
    "GOBBY_",
    "CLAUDE",
    "CODEX_",
    "GROK_",
    "FACTORY_",
    "DROID_",
    "AGY_",
    "GEMINI_",
    "GIT_",
    "XDG_",
)
# Commands that must not resolve on the recording PATH: the provider CLIs, the
# pre-commit framework the git-hooks component probes, and the managed binaries.
ABSENT_COMMANDS: tuple[str, ...] = (
    "claude",
    "codex",
    "grok",
    "droid",
    "agy",
    "pre-commit",
    "ghook",
    "gcode",
    "gdaemon",
)
GHOOK_MANIFEST = REPO_ROOT / "crates" / "ghook" / "Cargo.toml"

type Snapshot = dict[str, tuple[int, bytes]]
type BundledIndex = dict[str, list[str]]


@dataclass(frozen=True)
class InstallRecording:
    """One recording: the golden document and each component's own temporary root."""

    golden: dict[str, Any] = field(default_factory=dict)
    roots: dict[str, Path] = field(default_factory=dict)


class _StepClock:
    """The recording clock: fixed within a step, one second later for the next step."""

    def __init__(self) -> None:
        self._now = RECORDING_CLOCK

    def time(self) -> float:
        return self._now

    def advance(self) -> None:
        self._now += 1.0


class InstallRecordingError(RuntimeError):
    """A recording precondition or step failed; the golden is not written."""


def _ghook_version() -> str:
    with GHOOK_MANIFEST.open("rb") as source:
        version = tomllib.load(source)["package"]["version"]
    if not isinstance(version, str) or not version:
        raise InstallRecordingError(f"{GHOOK_MANIFEST} names no package version")
    return version


@contextmanager
def _isolated_process(
    home: Path, path_dir: Path, git_dir: Path, clock: _StepClock
) -> Iterator[None]:
    """Point HOME, GOBBY_HOME, PATH, the clock and the umask at the recording."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(CLEARED_ENV_PREFIXES)
    }
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "GOBBY_HOME": str(home / ".gobby"),
            "PATH": os.pathsep.join((str(path_dir), str(git_dir))),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    previous_umask = os.umask(RECORDING_UMASK)
    try:
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(Path, "home", return_value=home),
            mock.patch("time.time", new=clock.time),
        ):
            yield
    finally:
        os.umask(previous_umask)


def _assert_contained(root: Path, home: Path) -> None:
    """Fail before any installer runs if a home the installers read leaves root."""
    from gobby.claude_config import claude_config_path
    from gobby.paths import get_gobby_home
    from gobby.utils.native_bin import native_bin_dir

    resolved_root = root.resolve()
    homes = {
        "Path.home()": Path.home(),
        "expanduser('~')": Path(os.path.expanduser("~")),
        "the Gobby home": get_gobby_home(),
        "the native bin directory": native_bin_dir(),
        "the Claude config file": claude_config_path(),
        "the Gobby hooks directory": get_gobby_home() / "hooks",
    }
    for label, value in homes.items():
        if not value.resolve().is_relative_to(resolved_root):
            raise InstallRecordingError(f"{label} resolves to {value}, outside {root}")
    if Path.home() != home:
        raise InstallRecordingError(f"Path.home() is {Path.home()}, not {home}")
    found = {name: shutil.which(name) for name in ABSENT_COMMANDS}
    present = {name: where for name, where in found.items() if where is not None}
    if present:
        raise InstallRecordingError(f"the recording PATH resolves commands it must not: {present}")


def _snapshot(roots: Mapping[str, Path]) -> Snapshot:
    """Every file and symlink under each root, keyed by placeholder path."""
    snapshot: Snapshot = {}
    for placeholder, root in roots.items():
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            key = f"{placeholder}/{path.relative_to(root).as_posix()}"
            if stat.S_ISLNK(info.st_mode):
                raise InstallRecordingError(f"an installer created the symlink {key}")
            snapshot[key] = (stat.S_IMODE(info.st_mode), path.read_bytes())
    return snapshot


def _bundled_index() -> BundledIndex:
    """Every bundled file's path under BUNDLED_ROOT, keyed by its content digest."""
    bundled: BundledIndex = {}
    for path in sorted(BUNDLED_ROOT.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            bundled.setdefault(digest, []).append(path.relative_to(BUNDLED_ROOT).as_posix())
    return bundled


class _Encoder:
    """Turn one installed file into its golden fields."""

    def __init__(self, bundled: BundledIndex, replacements: list[tuple[str, str]]) -> None:
        self._replacements = replacements
        self._bundled = bundled

    def normalize(self, text: str) -> str:
        for value, placeholder in self._replacements:
            text = text.replace(value, placeholder)
        return text

    def _source(self, key: str, data: bytes) -> str | None:
        candidates = self._bundled.get(hashlib.sha256(data).hexdigest())
        if not candidates:
            return None
        installed = key.split("/")

        def shared_tail(candidate: str) -> int:
            parts = candidate.split("/")
            count = 0
            while count < min(len(parts), len(installed)) and (
                parts[-1 - count] == installed[-1 - count]
            ):
                count += 1
            return count

        return sorted(candidates, key=lambda candidate: (-shared_tail(candidate), candidate))[0]

    def fields(self, key: str, mode: int, data: bytes) -> dict[str, str]:
        # Windows has no POSIX mode bits, so the mode is recorded on POSIX only.
        entry: dict[str, str] = {} if sys.platform == "win32" else {"mode": f"{mode:04o}"}
        source = self._source(key, data)
        if source is not None:
            entry["source"] = source
            return entry
        try:
            entry["text"] = self.normalize(data.decode("utf-8"))
        except UnicodeDecodeError:
            entry["sha256"] = hashlib.sha256(data).hexdigest()
        return entry


def _install_entries(before: Snapshot, after: Snapshot, encoder: _Encoder) -> list[dict[str, str]]:
    gone = sorted(set(before) - set(after))
    if gone:
        raise InstallRecordingError(f"an install step removed {gone}")
    entries = []
    for key in sorted(after):
        if before.get(key) != after[key]:
            mode, data = after[key]
            entries.append({"path": key, **encoder.fields(key, mode, data)})
    return entries


def _uninstall_entries(
    installed: list[str], before: Snapshot, after: Snapshot, encoder: _Encoder
) -> list[dict[str, str]]:
    entries = []
    for key in sorted(set(installed) | set(after) | set(before)):
        if key in installed:
            if key not in after:
                entries.append({"path": key, "disposition": "removed"})
                continue
            disposition = "kept" if before.get(key) == after[key] else "modified"
        elif key not in before:
            disposition = CREATED_DISPOSITION
        elif before[key] != after.get(key):
            raise InstallRecordingError(
                f"an uninstall step changed {key}, which its component never installed"
            )
        else:
            continue
        mode, data = after[key]
        entries.append({"path": key, "disposition": disposition, **encoder.fields(key, mode, data)})
    return entries


def _replacements(home: Path, project: Path) -> list[tuple[str, str]]:
    from gobby.paths import get_install_dir

    pairs = {
        str(home): "@HOME@",
        str(project): "@PROJECT@",
        str(get_install_dir()): "@INSTALL_DIR@",
        sys.executable: "@PYTHON@",
    }
    for value, placeholder in list(pairs.items()):
        pairs.setdefault(os.path.realpath(value), placeholder)
    return sorted(pairs.items(), key=lambda pair: (-len(pair[0]), pair[0]))


def _require_success(phase: str, component: str, results: Mapping[str, Any]) -> None:
    result = results.get(component)
    if not isinstance(result, Mapping) or result.get("success") is not True:
        detail = result.get("error") if isinstance(result, Mapping) else result
        raise InstallRecordingError(f"{phase} of {component} failed: {detail}")


def _record_component(
    component: str, root: Path, git: str, bundled: BundledIndex
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Install, then uninstall, one component on the fresh root ``root``."""
    from gobby.cli.install_components import run_install_components, run_uninstall_components
    from gobby.cli.runtime import CliRuntime
    from gobby.config.app import DaemonConfig

    home = root / "home"
    project = root / "project"
    path_dir = root / "path"
    for directory in (home, project, path_dir):
        directory.mkdir(parents=True)
    clock = _StepClock()
    with _isolated_process(home, path_dir, Path(git).parent, clock):
        _assert_contained(root, home)
        stamp = home / ".gobby" / "bin" / ".ghook-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text(_ghook_version() + "\n", encoding="utf-8")
        subprocess.run(  # nosec B603 # git resolved by the caller, fixed argv
            [git, "init", "-q", str(project)], check=True, capture_output=True
        )
        roots = {"@HOME@": home, "@PROJECT@": project}
        encoder = _Encoder(bundled, _replacements(home, project))
        runtime = CliRuntime(None, config=DaemonConfig())
        before = _snapshot(roots)
        results = run_install_components(
            [component],
            project_path=project,
            no_interactive=True,
            embedding=None,
            runtime=runtime,
        )
        _require_success("install", component, results)
        installed_snapshot = _snapshot(roots)
        install = _install_entries(before, installed_snapshot, encoder)
        clock.advance()
        results = run_uninstall_components([component], project_path=project, runtime=runtime)
        _require_success("uninstall", component, results)
        installed = [entry["path"] for entry in install]
        uninstall = _uninstall_entries(installed, installed_snapshot, _snapshot(roots), encoder)
        if any(path_dir.iterdir()):
            raise InstallRecordingError(f"a step wrote into the recording PATH {path_dir}")
    return install, uninstall


def record_install_tree(root: Path) -> InstallRecording:
    """Install and uninstall each of the six components on its own root under ``root``."""
    git = shutil.which("git")
    if git is None:
        raise InstallRecordingError(
            "git is not on PATH; the install recorder needs it for the git-hooks component "
            "and does not fall back to a fixed directory"
        )
    root.mkdir(parents=True, exist_ok=True)
    # Trust seeding writes both the configured and the resolved Gobby home, so a root
    # reached through a symlink (macOS /var is /private/var) would record two entries
    # that normalize to one placeholder. The resolved root gives one spelling.
    root = root.resolve()
    if any(root.iterdir()):
        raise InstallRecordingError(f"the recording root {root} is not empty")
    bundled = _bundled_index()
    install: dict[str, list[dict[str, str]]] = {}
    uninstall: dict[str, list[dict[str, str]]] = {}
    component_roots: dict[str, Path] = {}
    for component in INSTALL_COMPONENTS:
        component_roots[component] = root / component
        install[component], uninstall[component] = _record_component(
            component, component_roots[component], git, bundled
        )
    golden = {
        "components": list(INSTALL_COMPONENTS),
        "phases": {"install": install, "uninstall": uninstall},
    }
    return InstallRecording(golden=golden, roots=component_roots)


def host_path_occurrences(text: str, values: Iterable[str]) -> list[str]:
    """The host values that ``text`` carries as a path, sorted.

    A value counts where it starts a path (no path-name character before it) and ends
    at a separator, whitespace, a JSON string quote or escape, a shell single quote, a
    PATH-list colon, a comma, a closing parenthesis, or the end of the text. So a recorded
    ``/tmp/x/home``, a bare ``"/tmp"``, ``cd '/tmp'`` and ``PATH=/tmp:/usr/bin`` count,
    while ``/var/tmp/x`` and the shell default ``${TMPDIR:-/tmp}`` in a bundled hook
    template do not.
    """
    found = set()
    for value in values:
        if value and re.search(rf"(?<![\w.~-]){re.escape(value)}(?=[/\\\"'\s:,)]|$)", text):
            found.add(value)
    return sorted(found)


def encode_golden(golden: Mapping[str, Any]) -> str:
    """Serialize a golden document to the committed bytes."""
    return json.dumps(golden, indent=2, ensure_ascii=False) + "\n"


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _keyed(golden: Mapping[str, Any]) -> dict[str, Any]:
    """The view golden_differences compares: entries keyed by path within each component."""
    view: dict[str, Any] = {key: value for key, value in golden.items() if key != "phases"}
    phases = golden.get("phases")
    if not isinstance(phases, Mapping):
        view["phases"] = phases
        return view
    keyed_phases: dict[str, Any] = {}
    for phase, components in phases.items():
        if not isinstance(components, Mapping):
            keyed_phases[phase] = components
            continue
        keyed_components: dict[str, Any] = {}
        for component, entries in components.items():
            if not isinstance(entries, list):
                keyed_components[component] = entries
                continue
            keyed: dict[str, Any] = {}
            for index, entry in enumerate(entries):
                if isinstance(entry, Mapping) and isinstance(entry.get("path"), str):
                    keyed[entry["path"]] = {k: v for k, v in entry.items() if k != "path"}
                else:
                    keyed[f"#{index}"] = entry
            keyed_components[component] = keyed
        keyed_phases[phase] = keyed_components
    view["phases"] = keyed_phases
    return view


def _walk(pointer: str, expected: Any, actual: Any, out: list[str]) -> None:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{pointer}/{_escape(str(key))}"
            if key not in actual:
                out.append(f"{child}: missing (expected {json.dumps(expected[key])})")
            elif key not in expected:
                out.append(f"{child}: unexpected {json.dumps(actual[key])}")
            else:
                _walk(child, expected[key], actual[key], out)
        return
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            _walk(f"{pointer}/{index}", left, right, out)
        return
    if expected != actual:
        out.append(f"{pointer}: expected {json.dumps(expected)}, got {json.dumps(actual)}")


def golden_differences(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Name each differing leaf of two golden documents by its JSON pointer."""
    differences: list[str] = []
    _walk("", _keyed(expected), _keyed(actual), differences)
    return differences
