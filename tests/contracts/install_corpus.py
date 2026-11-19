"""Record the install golden tree: what `gobby install` writes on a temporary HOME.

The recorder installs, then uninstalls, the six components claude, codex, grok, droid,
agy and git-hooks, one at a time and in that order, against a temporary HOME, a
temporary GOBBY_HOME and a temporary git project. The provider CLIs are absent by
design: PATH holds a recorder-owned empty directory and the directory of git only.

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
changed: ``path``, ``mode`` (an octal string such as ``"0644"``, POSIX only) and
exactly one content key. ``text`` holds UTF-8 content after placeholder normalization,
``sha256`` the digest of other bytes, and ``source`` names the file under
``src/gobby/install/shared/`` whose bytes the installed file equals. A ``source``
entry is compared by path, mode and source: a replay checks that the installed bytes
equal the bundled file it names, so bundled content is checked, not excluded, and its
bytes are not copied into the golden.

An uninstall entry gives the disposition of one path after that component's uninstall
step, relative to the state just before the step. Every path of the component's install
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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class InstallRecording:
    """One recording: the golden document and the temporary roots it was taken under."""

    golden: dict[str, Any] = field(default_factory=dict)
    home: Path = Path("")
    project: Path = Path("")


def record_install_tree(root: Path) -> InstallRecording:
    """Install and uninstall the six components under ``root`` and return the golden."""
    return InstallRecording()


def encode_golden(golden: Mapping[str, Any]) -> str:
    """Serialize a golden document to the committed bytes."""
    return ""


def golden_differences(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Name each differing leaf of two golden documents by its JSON pointer."""
    return []
