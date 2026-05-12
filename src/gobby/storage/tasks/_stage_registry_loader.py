"""Bundled task stage registry loader."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from gobby.paths import get_install_dir
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._stage_registry import (
    StageRegistryEntry as StorageStageRegistryEntry,
)
from gobby.storage.tasks._stage_registry import (
    StageRegistryManager,
)
from gobby.storage.tasks._stage_reviewer_selector import (
    ReviewerAgentSelectorError,
    normalize_reviewer_agent_selector,
)

StageCategory = Literal["discovery", "design", "verification", "implementation", "delivery"]
ReviewPolicy = Literal["none", "required", "optional"]
DispatchType = Literal["agent", "pipeline"]
_CATEGORIES: set[str] = {"discovery", "design", "verification", "implementation", "delivery"}
_REVIEW_POLICIES: set[str] = {"none", "required", "optional"}
_DISPATCH_TYPES: set[str] = {"agent", "pipeline"}


class StageRegistryLoadError(ValueError):
    """Raised when the bundled stage registry file is malformed."""


def _stage_category(value: str) -> StageCategory:
    return cast(StageCategory, value)


def _review_policy(value: str) -> ReviewPolicy:
    return cast(ReviewPolicy, value)


def _dispatch_type(value: str | None) -> DispatchType | None:
    return cast(DispatchType, value) if value is not None else None


@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: StageCategory
    default_agent: str | None
    reviewer_agent: str | None
    reviewer_agent_selector_json: str | None
    review_policy: ReviewPolicy
    dispatch_type: DispatchType | None
    dispatch_target: str | None
    dispatch_inputs_json: str | None
    position_hint: int
    requires_human: bool = False
    is_terminal: bool = False
    default_max_work_attempts: int = 3
    default_max_review_rounds: int = 5
    bundled_hash: str = ""

    def to_registry_entry(self) -> StorageStageRegistryEntry:
        return StorageStageRegistryEntry(
            name=self.name,
            display_label=self.display_label,
            description=self.description,
            category=self.category,
            default_agent=self.default_agent,
            reviewer_agent=self.reviewer_agent,
            reviewer_agent_selector_json=self.reviewer_agent_selector_json,
            review_policy=self.review_policy,
            dispatch_type=self.dispatch_type,
            dispatch_target=self.dispatch_target,
            dispatch_inputs_json=self.dispatch_inputs_json,
            position_hint=self.position_hint,
            requires_human=self.requires_human,
            is_terminal=self.is_terminal,
            default_max_work_attempts=self.default_max_work_attempts,
            default_max_review_rounds=self.default_max_review_rounds,
            bundled_hash=self.bundled_hash,
        )


@dataclass(frozen=True, slots=True)
class StageRegistrySyncResult:
    upserted: int
    skipped: int
    bundled_hash: str
    soft_deleted: int = 0


class StageRegistryLoader:
    """Load and sync the bundled stages.yaml registry."""

    BUNDLED_PATH = Path("shared/registry/stages.yaml")

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def bundled_path(self) -> Path:
        return self.path if self.path is not None else get_install_dir() / self.BUNDLED_PATH

    def load(self) -> list[StageRegistryEntry]:
        entries, _digest = self.load_with_hash()
        return entries

    def load_with_hash(self) -> tuple[list[StageRegistryEntry], str]:
        payload, _digest = self._read_payload()
        return self._parse_entries(payload), _digest

    def load_with_hashes(self) -> list[StageRegistryEntry]:
        entries, _digest = self.load_with_hash()
        return entries

    def sync(self, db: DatabaseProtocol) -> StageRegistrySyncResult:
        payload, bundled_hash = self._read_payload()
        entries = self._parse_entries(payload)
        upserted = 0
        skipped = 0
        soft_deleted = 0
        bundled_names = {entry.name for entry in entries}

        with db.transaction():
            for entry in entries:
                row = db.fetchone(
                    "SELECT * FROM task_stages_registry WHERE name = ?",
                    (entry.name,),
                )
                if row is not None:
                    if row["deleted_at"] is not None:
                        skipped += 1
                        continue
                    stored_hash = row["bundled_hash"]
                    current_hash = StageRegistryManager.row_hash(row)
                    if stored_hash and current_hash not in {stored_hash, entry.bundled_hash}:
                        skipped += 1
                        continue
                    if current_hash == entry.bundled_hash and stored_hash == entry.bundled_hash:
                        skipped += 1
                        continue
                if row is not None and row["bundled_hash"] == entry.bundled_hash:
                    skipped += 1
                    continue
                db.execute(
                    """
                    INSERT INTO task_stages_registry (
                        name, display_label, description, category, default_agent,
                        reviewer_agent, reviewer_agent_selector_json, review_policy,
                        dispatch_type, dispatch_target, dispatch_inputs_json, position_hint,
                        requires_human, is_terminal, default_max_work_attempts,
                        default_max_review_rounds, bundled_hash, deleted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, datetime('now'))
                    ON CONFLICT(name) DO UPDATE SET
                        display_label = excluded.display_label,
                        description = excluded.description,
                        category = excluded.category,
                        default_agent = excluded.default_agent,
                        reviewer_agent = excluded.reviewer_agent,
                        reviewer_agent_selector_json = excluded.reviewer_agent_selector_json,
                        review_policy = excluded.review_policy,
                        dispatch_type = excluded.dispatch_type,
                        dispatch_target = excluded.dispatch_target,
                        dispatch_inputs_json = excluded.dispatch_inputs_json,
                        position_hint = excluded.position_hint,
                        requires_human = excluded.requires_human,
                        is_terminal = excluded.is_terminal,
                        default_max_work_attempts = excluded.default_max_work_attempts,
                        default_max_review_rounds = excluded.default_max_review_rounds,
                        bundled_hash = excluded.bundled_hash,
                        deleted_at = NULL,
                        updated_at = datetime('now')
                    """,
                    (
                        entry.name,
                        entry.display_label,
                        entry.description,
                        entry.category,
                        entry.default_agent,
                        entry.reviewer_agent,
                        entry.reviewer_agent_selector_json,
                        entry.review_policy,
                        entry.dispatch_type,
                        entry.dispatch_target,
                        entry.dispatch_inputs_json,
                        entry.position_hint,
                        1 if entry.requires_human else 0,
                        1 if entry.is_terminal else 0,
                        entry.default_max_work_attempts,
                        entry.default_max_review_rounds,
                        entry.bundled_hash,
                    ),
                )
                upserted += 1
            if bundled_names:
                placeholders = ",".join("?" for _ in bundled_names)
                orphaned = db.fetchall(
                    f"""
                    SELECT name
                      FROM task_stages_registry
                     WHERE bundled_hash IS NOT NULL
                       AND deleted_at IS NULL
                       AND name NOT IN ({placeholders})
                    """,  # nosec B608 - placeholders are generated, values are bound.
                    tuple(sorted(bundled_names)),
                )
                if orphaned:
                    db.executemany(
                        """
                        UPDATE task_stages_registry
                           SET deleted_at = datetime('now'), updated_at = datetime('now')
                         WHERE name = ?
                        """,
                        [(row["name"],) for row in orphaned],
                    )
                    soft_deleted = len(orphaned)

        return StageRegistrySyncResult(
            upserted=upserted,
            skipped=skipped,
            bundled_hash=bundled_hash,
            soft_deleted=soft_deleted,
        )

    def detect_override(self, db_row: dict[str, Any], bundled_row: dict[str, Any]) -> bool:
        return bool(db_row.get("bundled_hash") != bundled_row.get("bundled_hash"))

    def _read_payload(self) -> tuple[dict[str, Any], str]:
        path = self.bundled_path()
        if not path.exists():
            raise StageRegistryLoadError(f"Bundled stage registry not found: {path}")
        raw = path.read_bytes()
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise StageRegistryLoadError(f"Malformed stage registry YAML: {exc}") from exc
        if not isinstance(payload, dict):
            raise StageRegistryLoadError("Stage registry YAML must be a mapping")
        return payload, hashlib.sha256(raw).hexdigest()

    def _parse_entries(self, payload: dict[str, Any]) -> list[StageRegistryEntry]:
        if payload.get("version") != 1:
            raise StageRegistryLoadError("Stage registry version must be 1")
        raw_stages = payload.get("stages")
        if not isinstance(raw_stages, list) or not raw_stages:
            raise StageRegistryLoadError("Stage registry must contain a non-empty stages list")

        entries: list[StageRegistryEntry] = []
        seen: set[str] = set()
        for index, raw_stage in enumerate(raw_stages):
            if not isinstance(raw_stage, dict):
                raise StageRegistryLoadError(f"Stage entry {index} must be a mapping")
            entry = self._parse_entry(index, raw_stage, seen)
            entries.append(self._with_bundled_hash(entry))
        return entries

    def _parse_entry(
        self,
        index: int,
        raw_stage: dict[str, Any],
        seen: set[str],
    ) -> StageRegistryEntry:
        required = {
            "name",
            "display_label",
            "description",
            "category",
            "review_policy",
            "position_hint",
        }
        missing = required - set(raw_stage)
        if missing:
            raise StageRegistryLoadError(
                f"Stage entry {index} missing required fields: {sorted(missing)}"
            )

        name = self._required_string(raw_stage, "name", index)
        if name in seen:
            raise StageRegistryLoadError(f"Duplicate stage name in bundled registry: {name}")
        seen.add(name)

        category = self._required_string(raw_stage, "category", index)
        if category not in _CATEGORIES:
            raise StageRegistryLoadError(f"Stage {name} has invalid category: {category}")
        position_hint = raw_stage["position_hint"]
        if not isinstance(position_hint, int):
            raise StageRegistryLoadError(f"Stage {name} position_hint must be an integer")

        default_agent = raw_stage.get("default_agent")
        if default_agent is not None and not isinstance(default_agent, str):
            raise StageRegistryLoadError(f"Stage {name} default_agent must be a string")

        reviewer_agent = raw_stage.get("reviewer_agent")
        if reviewer_agent is not None and not isinstance(reviewer_agent, str):
            raise StageRegistryLoadError(f"Stage {name} reviewer_agent must be a string")
        try:
            reviewer_agent_selector_json = normalize_reviewer_agent_selector(
                raw_stage.get("reviewer_agent_selector"),
                stage_name=name,
            )
        except ReviewerAgentSelectorError as exc:
            raise StageRegistryLoadError(str(exc)) from exc

        review_policy = self._required_string(raw_stage, "review_policy", index)
        if review_policy not in _REVIEW_POLICIES:
            raise StageRegistryLoadError(f"Stage {name} has invalid review_policy: {review_policy}")
        if (
            review_policy != "none"
            and not reviewer_agent
            and not reviewer_agent_selector_json
            and name != "pr"
        ):
            raise StageRegistryLoadError(
                f"Stage {name} reviewer_agent or reviewer_agent_selector is required "
                f"for review_policy={review_policy}"
            )
        dispatch_type = raw_stage.get("dispatch_type")
        if dispatch_type is not None:
            if not isinstance(dispatch_type, str) or dispatch_type not in _DISPATCH_TYPES:
                raise StageRegistryLoadError(f"Stage {name} has invalid dispatch_type")

        dispatch_target = raw_stage.get("dispatch_target")
        if dispatch_target is not None and not isinstance(dispatch_target, str):
            raise StageRegistryLoadError(f"Stage {name} dispatch_target must be a string")
        if dispatch_type == "pipeline" and not dispatch_target:
            raise StageRegistryLoadError(f"Stage {name} dispatch_target is required")
        if dispatch_type == "agent" and not (dispatch_target or default_agent):
            raise StageRegistryLoadError(
                f"Stage {name} dispatch_target or default_agent is required"
            )

        dispatch_inputs = raw_stage.get("dispatch_inputs")
        if dispatch_inputs is not None and not isinstance(dispatch_inputs, dict):
            raise StageRegistryLoadError(f"Stage {name} dispatch_inputs must be a mapping")

        return StageRegistryEntry(
            name=name,
            display_label=self._required_string(raw_stage, "display_label", index),
            description=self._required_string(raw_stage, "description", index),
            category=_stage_category(category),
            default_agent=default_agent,
            reviewer_agent=reviewer_agent,
            reviewer_agent_selector_json=reviewer_agent_selector_json,
            review_policy=_review_policy(review_policy),
            dispatch_type=_dispatch_type(dispatch_type),
            dispatch_target=dispatch_target,
            dispatch_inputs_json=(
                json.dumps(dispatch_inputs, sort_keys=True) if dispatch_inputs is not None else None
            ),
            position_hint=position_hint,
            requires_human=self._optional_bool(raw_stage, "requires_human", name),
            is_terminal=self._optional_bool(raw_stage, "is_terminal", name),
            default_max_work_attempts=self._optional_positive_int(
                raw_stage, "default_max_work_attempts", name, default=3
            ),
            default_max_review_rounds=self._optional_positive_int(
                raw_stage, "default_max_review_rounds", name, default=5
            ),
        )

    @staticmethod
    def _required_string(raw_stage: dict[str, Any], key: str, index: int) -> str:
        value = raw_stage[key]
        if not isinstance(value, str) or not value:
            raise StageRegistryLoadError(f"Stage entry {index} {key} must be a non-empty string")
        return value

    @staticmethod
    def _optional_bool(raw_stage: dict[str, Any], key: str, stage_name: str) -> bool:
        value = raw_stage.get(key, False)
        if not isinstance(value, bool):
            raise StageRegistryLoadError(f"Stage {stage_name} {key} must be a boolean")
        return value

    @staticmethod
    def _optional_positive_int(
        raw_stage: dict[str, Any],
        key: str,
        stage_name: str,
        *,
        default: int,
    ) -> int:
        value = raw_stage.get(key, default)
        if not isinstance(value, int) or value < 1:
            raise StageRegistryLoadError(f"Stage {stage_name} {key} must be a positive integer")
        return value

    @staticmethod
    def _with_bundled_hash(entry: StageRegistryEntry) -> StageRegistryEntry:
        payload = {
            "name": entry.name,
            "display_label": entry.display_label,
            "description": entry.description,
            "category": entry.category,
            "default_agent": entry.default_agent,
            "reviewer_agent": entry.reviewer_agent,
            "reviewer_agent_selector_json": entry.reviewer_agent_selector_json,
            "review_policy": entry.review_policy,
            "dispatch_type": entry.dispatch_type,
            "dispatch_target": entry.dispatch_target,
            "dispatch_inputs_json": entry.dispatch_inputs_json,
            "position_hint": entry.position_hint,
            "requires_human": entry.requires_human,
            "is_terminal": entry.is_terminal,
            "default_max_work_attempts": entry.default_max_work_attempts,
            "default_max_review_rounds": entry.default_max_review_rounds,
        }
        return replace(entry, bundled_hash=StageRegistryManager.row_hash(payload))
