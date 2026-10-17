"""Resolve explicit/origin machine targets without local-path fallbacks."""

from __future__ import annotations

from dataclasses import dataclass

from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.utils.machine_id import require_machine_id


@dataclass(frozen=True)
class RoutedMachine:
    machine_id: str
    local: bool
    checkout_root: str | None = None


def is_local_machine(machine_id: str | None) -> bool:
    return machine_id is None or machine_id == require_machine_id()


async def resolve_target_machine(
    db: HubDatabase,
    *,
    project_id: str,
    requested: str | None,
    origin_machine_id: str | None,
) -> RoutedMachine:
    """Remote roots are metadata only; local filesystem guards remain untouched."""
    origin_id = origin_machine_id if origin_machine_id is not None else require_machine_id()
    target_id = requested if requested is not None else origin_id
    if is_local_machine(target_id):
        return RoutedMachine(target_id, local=True)

    machines = LocalMachineManager(db)
    try:
        origin = machines.get(origin_id)
        if origin is None:
            raise NodeCommandError("machine_not_found", "Origin machine not found", "none")
        target = machines.get(target_id, owner_user_id=origin.owner_user_id)
    except ValueError as exc:
        raise NodeCommandError("machine_not_found", "Invalid machine reference", "none") from exc
    if target is None:
        raise NodeCommandError("machine_not_found", "Target machine not found", "none")
    if is_local_machine(target.id):
        return RoutedMachine(target.id, local=True)
    checkout = LocalProjectCheckoutManager(db).get(target.id, project_id)
    if checkout is None:
        raise NodeCommandError("checkout_unresolved", "Target has no project checkout", "none")
    if target.id not in await node_command_client().connected_machine_ids():
        raise NodeCommandError("node_offline", "Target has no live channel", "none")
    if target.owner_user_id != origin.owner_user_id:
        raise NodeCommandError(
            "machine_not_owned", "Target and origin have different owners", "none"
        )
    return RoutedMachine(target.id, local=False, checkout_root=checkout.root_path)
