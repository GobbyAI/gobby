"""Offline regression checks for the bakeoff's host provisioning scaffold."""

import json
import stat
from pathlib import Path

import provision_environment as provision
import pytest
import yaml

IMAGE = "example/image@sha256:" + "1" * 64


@pytest.mark.parametrize("image", ["example/image:1", "example/image:latest", "sha256:short"])
def test_compose_rejects_mutable_or_malformed_image(tmp_path: Path, image: str) -> None:
    with pytest.raises(ValueError, match="immutable reference"):
        provision.write_compose(tmp_path, image, IMAGE, IMAGE)
    assert not (tmp_path / "config" / "compose.yaml").exists()


def test_compose_mounts_declared_volumes_without_builds(tmp_path: Path) -> None:
    provision.write_compose(tmp_path, IMAGE, IMAGE, IMAGE)
    compose = yaml.safe_load((tmp_path / "config" / "compose.yaml").read_text())
    for service in compose["services"].values():
        assert "build" not in service
        for mount in service["volumes"]:
            assert mount.split(":", 1)[0] in compose["volumes"]
        assert all(binding.startswith("127.0.0.1:") for binding in service["ports"])
    assert {entry["name"] for entry in compose["volumes"].values()} == set(
        provision.VOLUMES.values()
    )


def test_init_records_explicit_owner_in_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def snapshot(_source_repo: Path) -> dict[str, object]:
        return {"source": {}}

    def stop_before_archive(_repo: Path, _commit: str, _destination: Path) -> None:
        raise RuntimeError("test archive boundary")

    monkeypatch.setattr(provision, "external_state_snapshot", snapshot)
    monkeypatch.setattr(provision, "_archive", stop_before_archive)
    root = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match="test archive boundary"):
        provision.init_runtime(root, tmp_path, tmp_path, owner_session="#12034")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert json.loads((root / "ownership.json").read_text())["owner_session"] == "#12034"


def test_init_refuses_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("preserve")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        provision.init_runtime(root, tmp_path, tmp_path, owner_session="#12034")
    assert sentinel.read_text() == "preserve"


def test_init_requires_owner_before_creating_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    with pytest.raises(ValueError, match="owner session"):
        provision.init_runtime(root, tmp_path, tmp_path, owner_session=" ")
    assert not root.exists()
