"""Direct retirement preserves exact boundaries without recovery prerequisites."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import retire_legacy_wiki as cli
from scripts import wiki_retirement_inventory as inv
from scripts import wiki_retirement_receipts as receipts
from tests.cli import test_wiki_retirement as shared
from tests.cli.test_wiki_retirement import FakeStores

state = shared.state


class DirectStores(FakeStores):
    def inventory(self, *, direct: bool = False) -> list[inv.StoreTarget]:
        assert direct, "Direct operations must not collect recovery snapshots"
        return super().inventory()

    def capture(self, target: inv.StoreTarget) -> bytes | None:
        raise AssertionError("Direct retirement must not capture recovery payloads")

    def backup(self, target: inv.StoreTarget) -> bytes:
        raise AssertionError("Direct retirement must not back up data")

    def capture_direct(self, target: inv.StoreTarget) -> bytes | None:
        return self.values.get(target.key)

    def delete_direct(self, target: inv.StoreTarget) -> None:
        super().delete(target)


@pytest.fixture
def direct_state(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> tuple[inv.Inventory, Path, DirectStores]:
    value, _, _ = state
    stores = DirectStores()
    stores.values["postgres:wiki_schema"] = b"five exact wiki table counts"
    value = value.model_copy(update={"mode": "direct", "stores": stores.inventory(direct=True)})
    root = receipts.prepare_directory(value)
    receipts.write_private(root / "inventory.json", value.model_dump_json().encode())
    return value, root, stores


def test_direct_apply_needs_no_recovery_artifacts_and_preserves_original(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
) -> None:
    value, root, stores = direct_state
    result = cli.apply_direct(value, root, stores)
    assert result.complete and result.backup_digest is None
    assert result.operation == "apply-direct"
    assert stores.head == 426 and not stores.values
    assert not (Path(value.repository) / "wiki").exists()
    assert (Path(value.repository) / "original.txt").read_text() == "Preserve original source"
    assert inv.baseline(Path(value.repository)) == value.baseline
    assert {item.name for item in root.iterdir()} == {"inventory.json", "direct-apply.json"}
    assert cli.apply_direct(value, root, stores).complete


def test_direct_apply_defers_schema_and_resumes_after_canonical_migration(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
) -> None:
    value, root, stores = direct_state
    deferred = cli.apply_direct(value, root, stores, defer_schema=True)
    assert not deferred.complete and deferred.deferred_schema
    assert deferred.items["postgres:wiki_schema"] == "intent"
    assert stores.head == 425
    assert stores.deleted == ["qdrant:gwiki_project_example"]
    # The existing canonical maintenance owner performs exactly425 ->426.
    stores.values.pop("postgres:wiki_schema")
    stores.head = 426
    result = cli.apply_direct(value, root, stores)
    assert result.complete and not result.deferred_schema
    assert result.items["postgres:wiki_schema"] == "done"


def test_direct_failure_receipt_allows_bounded_retry(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
) -> None:
    value, root, stores = direct_state
    stores.fail = "postgres:wiki_schema"
    with pytest.raises(ConnectionError, match="unavailable"):
        cli.apply_direct(value, root, stores)
    partial = receipts.Receipt.model_validate_json((root / "direct-apply.json").read_bytes())
    assert not partial.complete
    assert partial.errors == ["ConnectionError"]
    assert partial.items["qdrant:gwiki_project_example"] == "done"
    assert partial.items["postgres:wiki_schema"] == "intent"
    stores.fail = None
    assert cli.apply_direct(value, root, stores).complete
    assert stores.deleted.count("qdrant:gwiki_project_example") == 1


def test_direct_inventory_of_already_retired_schema_is_idempotent(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
) -> None:
    value, _, stores = direct_state
    stores.values.pop("postgres:wiki_schema")
    stores.head = 426
    targets = [
        target.model_copy(update={"digest": inv.sha(b"{}"), "count": 0})
        if target.kind == "postgres"
        else target
        for target in value.stores
    ]
    value = value.model_copy(update={"stores": targets})
    root = receipts.prepare_directory(value)
    assert cli.apply_direct(value, root, stores).complete
    assert "postgres:wiki_schema" not in stores.deleted


def test_direct_apply_refuses_new_files_before_any_delete(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
) -> None:
    value, root, stores = direct_state
    unexpected = Path(value.repository) / "wiki/new-original.txt"
    unexpected.write_text("Keep this unadmitted file")
    with pytest.raises(inv.RetirementError, match="New files"):
        cli.apply_direct(value, root, stores)
    assert not stores.deleted
    assert unexpected.read_text() == "Keep this unadmitted file"


def test_direct_apply_refuses_backend_drift_and_live_writers(
    direct_state: tuple[inv.Inventory, Path, DirectStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, root, stores = direct_state
    stores.identity = "different"
    with pytest.raises(inv.RetirementError, match="Backend identity"):
        cli.apply_direct(value, root, stores)
    stores.identity = "source"
    monkeypatch.setattr(
        cli,
        "writer_processes",
        lambda: [inv.Process(pid=123, started="1", command="gwiki watch")],
    )
    with pytest.raises(inv.RetirementError, match="writer processes"):
        cli.apply_direct(value, root, stores)
    assert not stores.deleted


def test_direct_inventory_cannot_be_consumed_as_recovery(
    direct_state: tuple[inv.Inventory, Path, DirectStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, root, stores = direct_state
    with pytest.raises(inv.RetirementError, match="created with --direct"):
        cli.apply_direct(value.model_copy(update={"mode": "recovery"}), root, stores)
    monkeypatch.setattr(cli, "configured_stores", lambda: stores)
    assert cli.main(["apply", "--inventory", str(root / "inventory.json")]) == 1
    assert not stores.deleted


def test_direct_cli_applies_without_loading_backup_or_proof(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, root, stores = direct_state
    monkeypatch.setattr(cli, "configured_stores", lambda: stores)
    assert cli.main(["apply", "--direct", "--inventory", str(root / "inventory.json")]) == 0
    assert json.loads(capsys.readouterr().out)["complete"]


def test_direct_inventory_uses_lightweight_store_census(
    direct_state: tuple[inv.Inventory, Path, DirectStores],
) -> None:
    value, _, stores = direct_state
    result = cli.inventory(
        Path(value.repository),
        Path(value.gobby_home),
        Path(value.files_home),
        stores,
        [],
        direct=True,
    )
    assert result.mode == "direct" and not result.errors
    assert result.stores == stores.inventory(direct=True)


def test_direct_code_cleanup_waits_for_schema_and_passes_only_identities(
    direct_state: tuple[inv.Inventory, Path, DirectStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, _, stores = direct_state
    target = inv.CodeIndexTarget(
        project_id=shared.PROJECT,
        machine_id="35ca31fd-be58-40f6-a293-b8da6b50c462",
        root_path=value.repository,
        files=[
            inv.CodeIndexFile(
                file_path="wiki/page.md",
                versions=[],
                owner_path=str(Path(value.repository) / "wiki"),
            )
        ],
        digest=inv.sha(b"exact native identities"),
    )
    value = value.model_copy(update={"code_indexes": [target]})
    root = receipts.prepare_directory(value)
    manager = Mock()
    monkeypatch.setattr(cli, "code_index_manager", lambda _: manager)
    cli.apply_direct(value, root, stores, defer_schema=True)
    manager.delete_direct.assert_not_called()
    stores.head = 426
    stores.values.pop("postgres:wiki_schema")
    assert cli.apply_direct(value, root, stores).complete
    args = manager.delete_direct.call_args.args
    assert args[:3] == (target, value.digest, root)
    assert args[3].receipt.backup_digest is None
    assert not (Path(value.repository) / target.files[0].file_path).exists()


def test_recovery_mode_omission_preserves_archived_inventory_digest(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, _, _ = state
    serialized = value.model_dump(mode="json")
    assert "mode" not in serialized
    assert inv.Inventory.model_validate(serialized).digest == value.digest
    assert value.model_copy(update={"mode": "direct"}).digest != value.digest
