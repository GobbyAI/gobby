"""write-compose renders only digest-pinned images and pulls only the references that are missing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from provision_environment import ensure_images, write_compose
from runtime_boundary import EvidenceError

POSTGRES = (
    "gobby-postgres-local@sha256:443364cb8e901fcbf902ee0435e9fc2333cb000d7351e22bd78485365e4d3d02"
)
QDRANT = "qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc"
FALKORDB = (
    "falkordb/falkordb@sha256:e93fcd753fe612fb0a222166a0620a1ae31b826a12f223c3b6d06038d9d7a364"
)
LATEST_FALKORDB = "falkordb/falkordb:latest@" + FALKORDB.split("@", 1)[1]
IMAGES = {"postgres": POSTGRES, "qdrant": QDRANT, "falkordb": FALKORDB}


class FakeDocker:
    def __init__(self, present: set[str], failures: dict[str, str] | None = None) -> None:
        self.present = set(present)
        self.failures = failures or {}
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.argv.append(argv)
        reference = argv[-1]
        if argv[1] == "image":
            code, stderr = (0, b"") if reference in self.present else (1, b"No such image")
            return subprocess.CompletedProcess(argv, code, b"", stderr)
        if reference in self.failures:
            return subprocess.CompletedProcess(argv, 1, b"", self.failures[reference].encode())
        self.present.add(reference)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def _runtime_dirs(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "receipts").mkdir()


def test_write_compose_refuses_a_latest_tag_even_when_it_is_also_pinned(tmp_path: Path) -> None:
    _runtime_dirs(tmp_path)

    with pytest.raises(ValueError, match="latest"):
        write_compose(tmp_path, POSTGRES, QDRANT, LATEST_FALKORDB)

    assert not (tmp_path / "config/compose.yaml").exists()


def test_write_compose_renders_only_the_pinned_digests(tmp_path: Path) -> None:
    _runtime_dirs(tmp_path)

    write_compose(tmp_path, POSTGRES, QDRANT, FALKORDB)

    compose = (tmp_path / "config/compose.yaml").read_text(encoding="utf-8")
    assert ":latest" not in compose
    assert f"image: {FALKORDB}" in compose


def test_ensure_images_pulls_only_the_references_that_are_missing() -> None:
    docker = FakeDocker(present={POSTGRES, QDRANT})

    ensure_images(IMAGES, docker)

    pulls = [argv for argv in docker.argv if argv[1] == "pull"]
    assert pulls == [["docker", "pull", FALKORDB]]
    assert FALKORDB in docker.present


def test_ensure_images_stops_with_the_exact_pull_error() -> None:
    docker = FakeDocker(present={FALKORDB, QDRANT}, failures={POSTGRES: "manifest unknown"})

    with pytest.raises(EvidenceError) as raised:
        ensure_images(IMAGES, docker)

    assert (
        str(raised.value) == f"docker pull failed for postgres image {POSTGRES}: manifest unknown"
    )
