"""Contract for the install golden tree recorded on a temporary HOME.

The golden document's shape is fixed in the module docstring of
``tests/contracts/install_corpus.py``. Recording runs the real installers, so it never
reads or writes the real HOME, the checkout or any directory outside its own root.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.contracts.install_corpus import (
    BUNDLED_ROOT,
    CONTENT_KEYS,
    CREATED_DISPOSITION,
    GOLDEN_PATH,
    INSTALL_COMPONENTS,
    INSTALL_DISPOSITIONS,
    README_PATH,
    REPO_ROOT,
    encode_golden,
    golden_differences,
    host_path_occurrences,
    record_install_tree,
)
from tests.fixtures.external_write_audit import observe_writes

RECORD_ENV = "GOBBY_RECORD_INSTALL_CONTRACTS"
README_HEADINGS = (
    "Record and replay",
    "Normalization",
    "Bundled source reference",
    "Uninstall disposition",
    "Containment",
    "Date stamp",
    "Platforms",
)
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="the golden records POSIX modes and is recorded on macOS"
)


def _load_golden() -> dict[str, Any]:
    assert GOLDEN_PATH.is_file(), f"the install golden does not exist at {GOLDEN_PATH}"
    loaded = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{GOLDEN_PATH} is not a JSON object"
    return loaded


def _phases(golden: dict[str, Any], label: str) -> dict[str, Any]:
    phases = golden.get("phases")
    assert isinstance(phases, dict), f"{label} has no phases object"
    assert set(phases) == {"install", "uninstall"}, (
        f"{label} has phases {sorted(phases)}, not install and uninstall"
    )
    for name in ("install", "uninstall"):
        assert isinstance(phases[name], dict), f"{label} phase {name} is not an object"
    return phases


def _tree(root: Path, skip: Path | None = None) -> dict[str, tuple[int, str]]:
    """Every file and directory under root with its mode and content digest."""
    tree: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if skip is not None and path.is_relative_to(skip):
            continue
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            digest = "link:" + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = "dir"
        tree[relative] = (info.st_mode, digest)
    return tree


@POSIX_ONLY
def test_python_install_matches_golden_tree(tmp_path: Path) -> None:
    golden = _load_golden()
    _phases(golden, str(GOLDEN_PATH))
    recording = record_install_tree(tmp_path / "replay")
    _phases(recording.golden, "the replayed recording")
    assert golden_differences(golden, recording.golden) == []
    assert encode_golden(recording.golden) == GOLDEN_PATH.read_text(encoding="utf-8")


@POSIX_ONLY
def test_install_recording_is_deterministic(tmp_path: Path) -> None:
    first = record_install_tree(tmp_path / "first")
    second = record_install_tree(tmp_path / "second")
    install = _phases(first.golden, "the first recording")["install"]
    encoded = encode_golden(first.golden)
    assert encoded, "the first recording encodes to no bytes"
    assert encoded == encode_golden(second.golden)
    assert set(install) == set(INSTALL_COMPONENTS), (
        f"the recording installs {sorted(install)}, not {sorted(INSTALL_COMPONENTS)}"
    )
    for component in INSTALL_COMPONENTS:
        entries = install[component]
        assert isinstance(entries, list), f"{component} install entries are not a list"
        assert entries, f"the recording has no install path for {component}"


def test_golden_has_no_host_paths() -> None:
    golden = _load_golden()
    _phases(golden, str(GOLDEN_PATH))
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    temp = tempfile.gettempdir()
    host_values = {
        temp,
        os.path.realpath(temp),
        sys.executable,
        os.path.realpath(sys.executable),
        str(REPO_ROOT),
        str(Path.home()),
    }
    assert host_path_occurrences(text, host_values) == [], f"{GOLDEN_PATH} carries host paths"


HOST_PATH_CASES: dict[str, tuple[str, list[str]]] = {
    "recorded_path_under_temp": ('{"path": "/tmp/pytest-of-ci/home/.claude"}', ["/tmp"]),
    "bare_temp_string": ('{"cwd": "/tmp"}', ["/tmp"]),
    "temp_before_whitespace": ('{"command": "cd /tmp && ls"}', ["/tmp"]),
    "temp_at_end_of_text_line": ('{"text": "TMP=/tmp\\nrun"}', ["/tmp"]),
    "shell_default_in_hook_template": ('"$(mktemp -d \\"${TMPDIR:-/tmp}/gobby.XXXXXX\\")"', []),
    "other_directory_ending_in_temp": ('{"path": "/var/tmp/cache"}', []),
    "longer_sibling_name": ('{"path": "/tmpfiles/x"}', []),
}


@pytest.mark.parametrize("case", sorted(HOST_PATH_CASES))
def test_host_path_occurrences_finds_host_paths_not_substrings(case: str) -> None:
    text, expected = HOST_PATH_CASES[case]
    assert host_path_occurrences(text, {"/tmp"}) == expected


def test_golden_components_match_declared_scope() -> None:
    golden = _load_golden()
    phases = _phases(golden, str(GOLDEN_PATH))
    assert golden.get("components") == list(INSTALL_COMPONENTS)
    for name in ("install", "uninstall"):
        assert set(phases[name]) == set(INSTALL_COMPONENTS), (
            f"phase {name} covers {sorted(phases[name])}, not {sorted(INSTALL_COMPONENTS)}"
        )
    for component in INSTALL_COMPONENTS:
        install = phases["install"][component]
        uninstall = phases["uninstall"][component]
        assert isinstance(install, list), f"{component} install entries are not a list"
        assert isinstance(uninstall, list), f"{component} uninstall entries are not a list"
        assert install, f"{component} has no install entry"
        installed = [entry["path"] for entry in install]
        assert installed == sorted(set(installed)), f"{component} install paths are not unique"
        dispositions = {entry["path"]: entry["disposition"] for entry in uninstall}
        assert len(dispositions) == len(uninstall), f"{component} uninstall paths repeat"
        for path in installed:
            assert path in dispositions, f"{component} gives no uninstall disposition for {path}"
            assert dispositions[path] in INSTALL_DISPOSITIONS, (
                f"{component} gives {path} the disposition {dispositions[path]}"
            )
        for path, disposition in dispositions.items():
            if path not in installed:
                assert disposition == CREATED_DISPOSITION, (
                    f"{component} uninstall names {path}, which it never installed, as "
                    f"{disposition}"
                )


def test_golden_entries_name_mode_and_one_content_or_bundled_source() -> None:
    golden = _load_golden()
    phases = _phases(golden, str(GOLDEN_PATH))
    sources: list[str] = []
    for name in ("install", "uninstall"):
        for component, entries in sorted(phases[name].items()):
            assert isinstance(entries, list), f"{name} {component} entries are not a list"
            for entry in entries:
                label = f"{name} {component} {entry.get('path')}"
                if entry.get("disposition") == "removed":
                    assert set(entry) == {"path", "disposition"}, label
                    continue
                content = [key for key in CONTENT_KEYS if key in entry]
                assert len(content) == 1, f"{label} carries content keys {content}"
                mode = entry.get("mode")
                assert isinstance(mode, str), f"{label} has no mode"
                assert len(mode) == 4, f"{label} mode {mode} is not four octal digits"
                int(mode, 8)
                if "source" in entry:
                    sources.append(entry["source"])
    assert sources, f"{GOLDEN_PATH} names no bundled source for any installed file"
    for source in sources:
        bundled = (BUNDLED_ROOT / source).resolve()
        assert bundled.is_relative_to(BUNDLED_ROOT.resolve()), f"{source} leaves the bundle"
        assert bundled.is_file(), f"the bundled source {source} does not exist"


def _synthetic_golden() -> dict[str, Any]:
    install: dict[str, list[dict[str, str]]] = {}
    uninstall: dict[str, list[dict[str, str]]] = {}
    for component in INSTALL_COMPONENTS:
        path = f"@HOME@/.{component}/settings.json"
        install[component] = [{"path": path, "mode": "0644", "text": "{}\n"}]
        uninstall[component] = [{"path": path, "disposition": "removed"}]
    skill = "@HOME@/.claude/skills/router/SKILL.md"
    install["claude"].append({"path": skill, "mode": "0644", "source": "skills/router/SKILL.md"})
    uninstall["claude"].append(
        {"path": skill, "disposition": "kept", "mode": "0644", "source": "skills/router/SKILL.md"}
    )
    return {
        "components": list(INSTALL_COMPONENTS),
        "phases": {"install": install, "uninstall": uninstall},
    }


def _set(phase: str, component: str, index: int, key: str, value: str) -> Callable[..., None]:
    def mutate(golden: dict[str, Any]) -> None:
        golden["phases"][phase][component][index][key] = value

    return mutate


def _drop(phase: str, component: str, index: int) -> Callable[..., None]:
    def mutate(golden: dict[str, Any]) -> None:
        del golden["phases"][phase][component][index]

    return mutate


def _add(phase: str, component: str, entry: dict[str, str]) -> Callable[..., None]:
    def mutate(golden: dict[str, Any]) -> None:
        golden["phases"][phase][component].append(entry)

    return mutate


_CLAUDE_SETTINGS = "/@HOME@~1.claude~1settings.json"
_CLAUDE_SKILL = "/@HOME@~1.claude~1skills~1router~1SKILL.md"
MUTATIONS: dict[str, tuple[Callable[..., None], set[str]]] = {
    "mode": (
        _set("install", "claude", 0, "mode", "0755"),
        {f"/phases/install/claude{_CLAUDE_SETTINGS}/mode"},
    ),
    "text": (
        _set("install", "claude", 0, "text", '{"hooks": {}}\n'),
        {f"/phases/install/claude{_CLAUDE_SETTINGS}/text"},
    ),
    "bundled_source": (
        _set("install", "claude", 1, "source", "skills/other/SKILL.md"),
        {f"/phases/install/claude{_CLAUDE_SKILL}/source"},
    ),
    "disposition": (
        _set("uninstall", "claude", 0, "disposition", "kept"),
        {f"/phases/uninstall/claude{_CLAUDE_SETTINGS}/disposition"},
    ),
    "missing_entry": (
        _drop("install", "codex", 0),
        {"/phases/install/codex/@HOME@~1.codex~1settings.json"},
    ),
    "extra_entry": (
        _add(
            "install",
            "grok",
            {"path": "@HOME@/.grok/extra.json", "mode": "0600", "sha256": "0" * 64},
        ),
        {"/phases/install/grok/@HOME@~1.grok~1extra.json"},
    ),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_golden_differences_names_a_mutated_entry(mutation: str) -> None:
    expected = _synthetic_golden()
    assert golden_differences(expected, copy.deepcopy(expected)) == []
    mutate, pointers = MUTATIONS[mutation]
    actual = copy.deepcopy(expected)
    mutate(actual)
    differences = golden_differences(expected, actual)
    named = {line.partition(": ")[0] for line in differences}
    assert named == pointers, f"{mutation} gives {differences}"


def test_install_recorder_writes_only_under_temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "real-home"
    (sentinel / ".claude").mkdir(parents=True)
    (sentinel / ".claude" / "settings.json").write_text('{"user": true}\n', encoding="utf-8")
    (sentinel / ".codex").mkdir()
    (sentinel / ".codex" / "config.toml").write_text("# user\n", encoding="utf-8")
    (sentinel / ".gitconfig").write_text("[user]\n\tname = sentinel\n", encoding="utf-8")
    (tmp_path / "beside-project.txt").write_text("ancestor target\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(sentinel))
    monkeypatch.setenv("USERPROFILE", str(sentinel))
    monkeypatch.setenv("GOBBY_HOME", str(sentinel / ".gobby"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    root = tmp_path / "recording"
    before = _tree(tmp_path, skip=root)
    assert before, "the sentinel tree is empty before recording"

    with observe_writes(REPO_ROOT) as repo_audit, observe_writes(sentinel) as home_audit:
        recording = record_install_tree(root)

    assert set(recording.roots) == set(INSTALL_COMPONENTS), (
        f"the recording has roots for {sorted(recording.roots)}, not {sorted(INSTALL_COMPONENTS)}"
    )
    assert len(set(recording.roots.values())) == len(INSTALL_COMPONENTS), (
        f"two components share a recording root: {recording.roots}"
    )
    for component, component_root in sorted(recording.roots.items()):
        home = component_root / "home"
        assert component_root.is_relative_to(root.resolve()), (
            f"the {component} recording root {component_root} is not under {root}"
        )
        assert (component_root / "project" / ".git").is_dir(), (
            f"the {component} recording project under {component_root} is not a git checkout"
        )
        assert any(path.is_file() for path in home.rglob("*")), (
            f"the {component} recording wrote nothing under {home}"
        )
    assert repo_audit.writes == []
    assert home_audit.writes == []
    assert _tree(tmp_path, skip=root) == before


def test_install_readme_documents_scope() -> None:
    assert README_PATH.is_file(), f"the install corpus README does not exist at {README_PATH}"
    headings = {
        line.lstrip("#").strip()
        for line in README_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    }
    missing = [heading for heading in README_HEADINGS if heading not in headings]
    assert missing == [], f"{README_PATH} lacks the headings {missing}"


@pytest.mark.skipif(
    os.environ.get(RECORD_ENV) != "1", reason=f"set {RECORD_ENV}=1 to record the install golden"
)
def test_record_install_contracts(tmp_path: Path) -> None:
    first = record_install_tree(tmp_path / "first")
    second = record_install_tree(tmp_path / "second")
    encoded = encode_golden(first.golden)
    assert encoded, "the recording encodes to no bytes"
    assert encoded == encode_golden(second.golden), "two recordings differ"
    GOLDEN_PATH.write_text(encoded, encoding="utf-8")
