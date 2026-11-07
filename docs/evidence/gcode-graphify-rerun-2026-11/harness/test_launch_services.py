"""Pure checks for the owned Docker setup: owner binding, step skipping, and zero-row gating."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from launch_services import assert_no_indexed_rows, prepare, start_services, step_complete
from provision_environment import COMPOSE_PROJECT, CONTAINERS
from runtime_boundary import write_once


def test_prepare_rejects_a_foreign_owner_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_once(
        tmp_path / "ownership.json",
        json.dumps({"runtime_root": str(tmp_path), "owner_task": "#21942"}),
        0o600,
    )

    def unreachable(*args: str) -> str:
        pytest.fail("docker was reached before the owner-task check")

    monkeypatch.setattr("launch_services.docker", unreachable)
    with pytest.raises(AssertionError, match="owner task"):
        prepare(tmp_path)


def test_step_is_skipped_only_when_its_receipt_verifies(tmp_path: Path) -> None:
    receipt = tmp_path / "receipts" / "services.json"
    assert step_complete(receipt, lambda: None) is False
    write_once(receipt, "{}\n", 0o600)
    assert step_complete(receipt, lambda: None) is True

    def changed() -> None:
        raise AssertionError("artifact hash changed")

    with pytest.raises(AssertionError, match="artifact hash changed"):
        step_complete(receipt, changed)


def test_every_project_must_hold_zero_indexed_rows() -> None:
    assert_no_indexed_rows({"graphify-C0": 0, "gcode-C0": 0})
    with pytest.raises(AssertionError, match="gcode-C1"):
        assert_no_indexed_rows({"graphify-C0": 0, "gcode-C1": 2})


def test_start_records_services_without_writing_the_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for directory in ("config", "receipts", "logs/install", "isolation"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "config/compose.yaml").write_text("services: {}\n")
    (tmp_path / "receipts/compose.rendered.json").write_text("{}\n")
    image_ids = {service: f"sha256:{service}" for service in CONTAINERS}

    def fake_docker(*args: str) -> str:
        if args[0] == "inspect":
            service = next(item for item, name in CONTAINERS.items() if name == args[1])
            container = {
                "Id": f"id-{service}",
                "Image": image_ids[service],
                "State": {"Health": {"Status": "healthy"}},
            }
            return json.dumps([container])
        labels = {"com.docker.compose.project": COMPOSE_PROJECT}
        if args[0] == "network":
            return json.dumps([{"Id": "net-id", "Labels": labels}])
        if args[0] == "volume":
            return json.dumps([{"Labels": labels}])
        raise AssertionError(f"unexpected docker call: {args}")

    def compose_up(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("launch_services.docker", fake_docker)
    monkeypatch.setattr("launch_services.subprocess.run", compose_up)
    monkeypatch.setattr("launch_services.validate_container", lambda *args: None)
    monkeypatch.setattr("launch_services.external_state_snapshot", lambda source: {})
    start_services(tmp_path, ["compose"], image_ids)
    assert (tmp_path / "receipts/services.json").exists()
    assert not (tmp_path / "isolation/external-state.after.json").exists()
