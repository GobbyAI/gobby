"""Bundled task stage registry loader."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from gobby.paths import get_install_dir
from gobby.storage.database import DatabaseProtocol

StageCategory = Literal["discovery", "design", "verification", "implementation", "delivery"]
ReviewPolicy = Literal["none", "required", "optional"]
DispatchType = Literal["agent", "pipeline"]
_CATEGORIES: set[str] = {"discovery", "design", "verification", "implementation", "delivery"}
_REVIEW_POLICIES: set[str] = {"none", "required", "optional"}
_DISPATCH_TYPES: set[str] = {"agent", "pipeline"}


class StageRegistryLoadError(ValueError):
    """Raised when the bundled stage registry file is malformed."""


@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: StageCategory
    default_agent: str | None
    reviewer_agent: str | None
    review_policy: ReviewPolicy
    dispatch_type: DispatchType | None
    dispatch_target: str | None
    dispatch_inputs_json: str | None
    position_hint: int
    requires_human: bool = False
    is_terminal: bool = False


@dataclass(frozen=True, slots=True)
class StageRegistrySyncResult:
    upserted: int
    skipped: int
    bundled_hash: str


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

    def sync(self, db: DatabaseProtocol) -> StageRegistrySyncResult:
        payload, bundled_hash = self._read_payload()
        entries = self._parse_entries(payload)
        upserted = 0
        skipped = 0

        with db.transaction():
            for entry in entries:
                row = db.fetchone(
                    "SELECT bundled_hash FROM task_stages_registry WHERE name = ?",
                    (entry.name,),
                )
                if row is not None and row["bundled_hash"] == bundled_hash:
                    skipped += 1
                    continue
                db.execute(
                    """
                    INSERT INTO task_stages_registry (
                        name, display_label, description, category, default_agent,
                        reviewer_agent, review_policy, dispatch_type, dispatch_target,
                        dispatch_inputs_json, position_hint, requires_human, is_terminal,
                        bundled_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(name) DO UPDATE SET
                        display_label = excluded.display_label,
                        description = excluded.description,
                        category = excluded.category,
                        default_agent = excluded.default_agent,
                        reviewer_agent = excluded.reviewer_agent,
                        review_policy = excluded.review_policy,
                        dispatch_type = excluded.dispatch_type,
                        dispatch_target = excluded.dispatch_target,
                        dispatch_inputs_json = excluded.dispatch_inputs_json,
                        position_hint = excluded.position_hint,
                        requires_human = excluded.requires_human,
                        is_terminal = excluded.is_terminal,
                        bundled_hash = excluded.bundled_hash,
                        updated_at = datetime('now')
                    """,
                    (
                        entry.name,
                        entry.display_label,
                        entry.description,
                        entry.category,
                        entry.default_agent,
                        entry.reviewer_agent,
                        entry.review_policy,
                        entry.dispatch_type,
                        entry.dispatch_target,
                        entry.dispatch_inputs_json,
                        entry.position_hint,
                        1 if entry.requires_human else 0,
                        1 if entry.is_terminal else 0,
                        bundled_hash,
                    ),
                )
                upserted += 1

        return StageRegistrySyncResult(
            upserted=upserted,
            skipped=skipped,
            bundled_hash=bundled_hash,
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
            entries.append(self._parse_entry(index, raw_stage, seen))
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

        review_policy = self._required_string(raw_stage, "review_policy", index)
        if review_policy not in _REVIEW_POLICIES:
            raise StageRegistryLoadError(f"Stage {name} has invalid review_policy: {review_policy}")
        if review_policy != "none" and not reviewer_agent and name != "pr":
            raise StageRegistryLoadError(
                f"Stage {name} reviewer_agent is required for review_policy={review_policy}"
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

        dispatch_inputs = raw_stage.get("dispatch_inputs")
        if dispatch_inputs is not None and not isinstance(dispatch_inputs, dict):
            raise StageRegistryLoadError(f"Stage {name} dispatch_inputs must be a mapping")

        return StageRegistryEntry(
            name=name,
            display_label=self._required_string(raw_stage, "display_label", index),
            description=self._required_string(raw_stage, "description", index),
            category=category,  # type: ignore[arg-type]
            default_agent=default_agent,
            reviewer_agent=reviewer_agent,
            review_policy=review_policy,  # type: ignore[arg-type]
            dispatch_type=dispatch_type,  # type: ignore[arg-type]
            dispatch_target=dispatch_target,
            dispatch_inputs_json=(
                json.dumps(dispatch_inputs, sort_keys=True) if dispatch_inputs is not None else None
            ),
            position_hint=position_hint,
            requires_human=self._optional_bool(raw_stage, "requires_human", name),
            is_terminal=self._optional_bool(raw_stage, "is_terminal", name),
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
