"""Domain state seeded before a rule-pass case's rules.

A case's ``seed`` mapping names the rows the engine reads besides the rules: the session
row, synthetic agent definitions and a step instance. This module owns the identity
constants every recorder module shares, so it never imports ``rules_recorder``.

The seeders are signature stubs until the engine-path cases land; ``validate_seed``
accepts every seed for now.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gobby.storage.hub.postgres import PostgresHubDatabase

SESSION_ID = "11111111-1111-4111-8111-111111111111"
SYNTHETIC_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
SYNTHETIC_USER_ID = "20000000-0000-4000-8000-0000000000a1"
SYNTHETIC_MACHINE_ID = "20000000-0000-4000-8000-0000000000aa"
SEED_ORDER: tuple[str, ...] = ("session", "agent_definitions", "step_instance")


def seed_identity(db: PostgresHubDatabase) -> None:
    """Insert the synthetic user and machine rows (ON CONFLICT DO NOTHING)."""
    ...


def validate_seed(case_id: str, seed: Mapping[str, Any] | None) -> None:
    """Raise ``ValueError`` naming the case and the key of a malformed seed."""
    ...


def seed_session(db: PostgresHubDatabase, spec: bool) -> None:
    """Insert the identity rows, the synthetic project and the SESSION_ID session row."""
    ...


def seed_agent_definitions(db: PostgresHubDatabase, specs: Sequence[Mapping[str, Any]]) -> None:
    """Install each synthetic agent definition globally, with its step workflow."""
    ...


def seed_step_instance(
    db: PostgresHubDatabase,
    spec: Mapping[str, Any],
    *,
    session_id: str,
    definitions: Sequence[Mapping[str, Any]],
) -> None:
    """Save the step instance of a seeded definition for ``session_id``."""
    ...


def _seed_session_entry(db: PostgresHubDatabase, seed: Mapping[str, Any]) -> None:
    seed_session(db, bool(seed["session"]))


def _seed_agent_definitions_entry(db: PostgresHubDatabase, seed: Mapping[str, Any]) -> None:
    seed_agent_definitions(db, seed["agent_definitions"])


def _seed_step_instance_entry(db: PostgresHubDatabase, seed: Mapping[str, Any]) -> None:
    seed_step_instance(
        db,
        seed["step_instance"],
        session_id=SESSION_ID,
        definitions=seed["agent_definitions"],
    )


# Each seeder takes the whole seed mapping, so one key's seeder can read another key.
SEEDERS: dict[str, Callable[[PostgresHubDatabase, Mapping[str, Any]], None]] = {
    "session": _seed_session_entry,
    "agent_definitions": _seed_agent_definitions_entry,
    "step_instance": _seed_step_instance_entry,
}


def apply_case_seed(db: PostgresHubDatabase, seed: Mapping[str, Any] | None) -> None:
    """Run the seeder of each key the seed carries, in SEED_ORDER."""
    ...
