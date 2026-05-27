"""DB-backed build profile registry."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from gobby.config.build import DeliveryMode, Isolation
from gobby.paths import get_install_dir
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.sql import sql_placeholders

BuildProfileSource = Literal["installed", "project"]
BuildProfileState = Literal["bundled", "edited", "custom", "deleted"]
_SOURCES = {"installed", "project"}
_RESERVED_PROFILE_NAMES = {"none", "null"}
logger = logging.getLogger(__name__)


class BuildProfileError(ValueError):
    """Raised when a build profile cannot be resolved or mutated."""


@dataclass(frozen=True, slots=True)
class BuildProfile:
    id: str
    name: str
    display_label: str
    description: str
    skip_stages: list[str]
    isolation: Isolation
    unattended: bool
    delivery_mode: DeliveryMode
    delivery_target_repo: str | None
    enabled: bool
    source: BuildProfileSource
    project_id: str | None = None
    tags: list[str] | None = None
    bundled_hash: str | None = None
    deleted_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    state: BuildProfileState = "custom"


@dataclass(frozen=True, slots=True)
class BuildProfileSyncResult:
    upserted: int
    skipped: int
    soft_deleted: int
    bundled_hash: str


class BuildProfileLoader:
    """Load and sync bundled build profiles."""

    BUNDLED_PATH = Path("shared/registry/build_profiles.yaml")

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def bundled_path(self) -> Path:
        return self.path if self.path is not None else get_install_dir() / self.BUNDLED_PATH

    def load(self) -> list[BuildProfile]:
        payload, _digest = self._read_payload()
        return self._parse_profiles(payload)

    def sync(self, db: HubDatabase) -> BuildProfileSyncResult:
        payload, digest = self._read_payload()
        profiles = self._parse_profiles(payload)
        names = {profile.name for profile in profiles}
        upserted = 0
        skipped = 0
        soft_deleted = 0
        manager = BuildProfileManager(db)
        with db.transaction():
            for profile in profiles:
                row = db.fetchone(
                    """
                    SELECT *
                      FROM build_profiles
                     WHERE name = %s
                       AND source = 'installed'
                       AND project_id IS NULL
                    """,
                    (profile.name,),
                )
                if row is not None:
                    if row["deleted_at"] is not None:
                        skipped += 1
                        continue
                    stored_hash = row["bundled_hash"]
                    current_hash = manager.row_hash(row)
                    legacy_hash = manager.legacy_row_hash(row)
                    if stored_hash and current_hash != stored_hash and legacy_hash != stored_hash:
                        skipped += 1
                        continue
                    if stored_hash == profile.bundled_hash and current_hash == profile.bundled_hash:
                        skipped += 1
                        continue
                manager.upsert_installed(profile)
                upserted += 1
            if names:
                placeholders = sql_placeholders(len(names))
                orphaned = db.fetchall(
                    f"""
                    SELECT name
                      FROM build_profiles
                     WHERE source = 'installed'
                       AND project_id IS NULL
                       AND bundled_hash IS NOT NULL
                       AND deleted_at IS NULL
                       AND name NOT IN ({placeholders})
                    """,  # nosec B608 # placeholders are generated, values are bound.
                    tuple(sorted(names)),
                )
                if orphaned:
                    db.executemany(
                        """
                        UPDATE build_profiles
                           SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                         WHERE source = 'installed'
                           AND project_id IS NULL
                           AND name = %s
                        """,
                        [(row["name"],) for row in orphaned],
                    )
                    soft_deleted = len(orphaned)
        return BuildProfileSyncResult(
            upserted=upserted,
            skipped=skipped,
            soft_deleted=soft_deleted,
            bundled_hash=digest,
        )

    def _read_payload(self) -> tuple[dict[str, Any], str]:
        path = self.bundled_path()
        if not path.exists():
            raise BuildProfileError(f"Bundled build profiles not found: {path}")
        raw = path.read_bytes()
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise BuildProfileError(f"Malformed build profiles YAML: {exc}") from exc
        if not isinstance(payload, dict):
            raise BuildProfileError("Build profiles YAML must be a mapping")
        return payload, hashlib.sha256(raw).hexdigest()

    def _parse_profiles(self, payload: dict[str, Any]) -> list[BuildProfile]:
        if payload.get("version") != 1:
            raise BuildProfileError("Build profile registry version must be 1")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise BuildProfileError("Build profile registry must contain profiles")
        profiles: list[BuildProfile] = []
        seen: set[str] = set()
        for index, raw_profile in enumerate(raw_profiles):
            if not isinstance(raw_profile, dict):
                raise BuildProfileError(f"Build profile {index} must be a mapping")
            profile = self._parse_profile(index, raw_profile)
            if profile.name in seen:
                raise BuildProfileError(f"Duplicate build profile name: {profile.name}")
            seen.add(profile.name)
            profiles.append(
                replace(profile, bundled_hash=BuildProfileManager._profile_hash(profile))
            )
        return profiles

    @staticmethod
    def _parse_profile(index: int, raw: dict[str, Any]) -> BuildProfile:
        name = _required_string(raw, "name", index)
        _validate_profile_name(name)
        isolation = raw.get("isolation", "worktree")
        if isolation not in {"none", "worktree", "clone"}:
            raise BuildProfileError(f"Build profile {name} isolation is invalid")
        skip_stages = raw.get("skip_stages", [])
        if not isinstance(skip_stages, list) or not all(
            isinstance(stage, str) for stage in skip_stages
        ):
            raise BuildProfileError(f"Build profile {name} skip_stages must be a string list")
        unattended = raw.get("unattended", False)
        if not isinstance(unattended, bool):
            raise BuildProfileError(f"Build profile {name} unattended must be boolean")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BuildProfileError(f"Build profile {name} enabled must be boolean")
        delivery_mode = raw.get("delivery_mode", "auto")
        if delivery_mode not in {"auto", "pull_request"}:
            raise BuildProfileError(f"Build profile {name} delivery_mode is invalid")
        delivery_target_repo = raw.get("delivery_target_repo")
        if delivery_target_repo is not None and not isinstance(delivery_target_repo, str):
            raise BuildProfileError(
                f"Build profile {name} delivery_target_repo must be a string or null"
            )
        _validate_delivery_target_repo(delivery_target_repo)
        tags = raw.get("tags", ["gobby"])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise BuildProfileError(f"Build profile {name} tags must be a string list")
        return BuildProfile(
            id=str(uuid.uuid4()),
            name=name,
            display_label=_required_string(raw, "display_label", index),
            description=_required_string(raw, "description", index),
            skip_stages=list(skip_stages),
            isolation=isolation,
            unattended=unattended,
            delivery_mode=delivery_mode,
            delivery_target_repo=delivery_target_repo,
            enabled=enabled,
            source="installed",
            project_id=None,
            tags=list(tags),
        )


class BuildProfileManager:
    """CRUD and resolution for build profiles."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def list_profiles(
        self,
        *,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[BuildProfile]:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        rows = self.db.fetchall(
            f"""
            SELECT *
              FROM build_profiles
             WHERE (source = 'installed' OR project_id IS NULL OR project_id = %s)
               {deleted_filter}
             ORDER BY source, COALESCE(project_id, ''), name
            """,  # nosec B608 # deleted_filter is controlled by a boolean.
            (project_id,),
        )
        return [self._profile_from_row(row) for row in rows]

    def get(
        self,
        name: str,
        *,
        source: BuildProfileSource | None = None,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> BuildProfile | None:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        source_filter = "" if source is None else "AND source = %s"
        params: list[Any] = [name]
        if source is not None:
            params.append(source)
        if project_id is None:
            scope_filter = "AND project_id IS NULL"
        else:
            scope_filter = "AND project_id = %s"
            params.append(project_id)
        row = self.db.fetchone(
            f"""
            SELECT *
              FROM build_profiles
             WHERE name = %s
               {source_filter}
               {scope_filter}
               {deleted_filter}
             ORDER BY source DESC
             LIMIT 1
            """,  # nosec B608 # filters are static snippets chosen by arguments.
            tuple(params),
        )
        return self._profile_from_row(row) if row is not None else None

    def resolve(self, name: str, *, project_id: str | None) -> BuildProfile:
        matches = [
            self.get(name, source="project", project_id=project_id, include_deleted=False)
            if project_id
            else None,
            self.get(name, source="project", project_id=None, include_deleted=False),
            self.get(name, source="installed", project_id=None, include_deleted=False),
        ]
        for profile in matches:
            if profile is None:
                continue
            if not profile.enabled:
                raise BuildProfileError(f"Build profile '{name}' is disabled")
            return profile
        raise BuildProfileError(f"Unknown build profile '{name}'")

    def create(
        self,
        *,
        name: str,
        display_label: str,
        description: str,
        skip_stages: Iterable[str],
        isolation: Isolation,
        unattended: bool,
        enabled: bool = True,
        delivery_mode: DeliveryMode = "auto",
        delivery_target_repo: str | None = None,
        source: BuildProfileSource = "project",
        project_id: str | None = None,
        tags: Iterable[str] | None = None,
    ) -> BuildProfile:
        _validate_profile_name(name)
        if source not in _SOURCES:
            raise BuildProfileError("source must be installed or project")
        if source == "installed" and project_id is not None:
            raise BuildProfileError("installed build profiles must be global")
        profile = BuildProfile(
            id=str(uuid.uuid4()),
            name=name,
            display_label=display_label,
            description=description,
            skip_stages=list(skip_stages),
            isolation=isolation,
            unattended=unattended,
            delivery_mode=delivery_mode,
            delivery_target_repo=delivery_target_repo,
            enabled=enabled,
            source=source,
            project_id=project_id,
            tags=list(tags or []),
            bundled_hash=None,
        )
        self._validate_profile(profile)
        self._ensure_no_active_duplicate(profile)
        self._insert_profile(profile)
        created = self.get(
            name,
            source=source,
            project_id=project_id,
            include_deleted=True,
        )
        if created is None:
            raise BuildProfileError(f"Build profile '{name}' could not be created")
        return created

    def update(
        self,
        name: str,
        *,
        source: BuildProfileSource,
        project_id: str | None,
        updates: dict[str, Any],
    ) -> BuildProfile:
        current = self.get(name, source=source, project_id=project_id)
        if current is None:
            raise BuildProfileError(f"Unknown build profile '{name}'")
        if "name" in updates and updates["name"] != name:
            raise BuildProfileError("build profile names are immutable")
        payload = self._profile_dataclass_payload(current)
        payload.update({key: value for key, value in updates.items() if key != "name"})
        if payload.get("delivery_target_repo") == "":
            payload["delivery_target_repo"] = None
        profile = BuildProfile(**payload)
        self._validate_profile(profile)
        self._update_profile(profile)
        updated = self.get(name, source=source, project_id=project_id)
        if updated is None:
            raise BuildProfileError(f"Build profile '{name}' could not be updated")
        return updated

    def set_enabled(
        self,
        name: str,
        *,
        source: BuildProfileSource,
        project_id: str | None,
        enabled: bool,
    ) -> BuildProfile:
        return self.update(
            name,
            source=source,
            project_id=project_id,
            updates={"enabled": enabled},
        )

    def restore(
        self,
        name: str,
        *,
        source: BuildProfileSource,
        project_id: str | None,
    ) -> BuildProfile:
        current = self.get(name, source=source, project_id=project_id, include_deleted=True)
        if current is None:
            raise BuildProfileError(f"Unknown build profile '{name}'")
        bundled = {profile.name: profile for profile in BuildProfileLoader().load()}
        bundled_profile = bundled.get(name)
        if bundled_profile is None:
            raise BuildProfileError(f"Build profile '{name}' is custom and cannot be restored")
        restored = replace(
            bundled_profile,
            id=current.id,
            source=current.source,
            project_id=current.project_id,
            deleted_at=None,
            created_at=current.created_at,
        )
        self._update_profile(restored, restore_deleted=True)
        restored_row = self.get(name, source=source, project_id=project_id)
        if restored_row is None:
            raise BuildProfileError(f"Build profile '{name}' could not be restored")
        return restored_row

    def delete(
        self,
        name: str,
        *,
        source: BuildProfileSource,
        project_id: str | None,
        purge: bool = False,
    ) -> BuildProfile | None:
        current = self.get(name, source=source, project_id=project_id)
        if current is None:
            raise BuildProfileError(f"Unknown build profile '{name}'")
        if purge:
            if source != "project":
                raise BuildProfileError("only project build profiles can be purged")
            self.db.execute("DELETE FROM build_profiles WHERE id = %s", (current.id,))
            return None
        self.db.execute(
            """
            UPDATE build_profiles
               SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            (current.id,),
        )
        return self.get(name, source=source, project_id=project_id, include_deleted=True)

    def upsert_installed(self, profile: BuildProfile) -> None:
        installed = replace(
            profile,
            source="installed",
            project_id=None,
            tags=profile.tags or ["gobby"],
        )
        self._validate_profile(installed)
        current = self.get(
            installed.name,
            source="installed",
            project_id=None,
            include_deleted=True,
        )
        if current is None:
            self._insert_profile(installed)
            return
        self._update_profile(replace(installed, id=current.id), restore_deleted=True)

    @classmethod
    def row_hash(cls, row: Mapping[str, Any]) -> str:
        return cls._hash_payload(cls._row_payload(row))

    @classmethod
    def legacy_row_hash(cls, row: Mapping[str, Any]) -> str:
        return cls._hash_payload(cls._legacy_row_payload(row))

    @classmethod
    def _profile_hash(cls, profile: BuildProfile) -> str:
        return cls._hash_payload(cls._profile_payload(profile))

    @staticmethod
    def _hash_payload(payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def _insert_profile(self, profile: BuildProfile) -> None:
        self.db.execute(
            """
            INSERT INTO build_profiles (
                id, name, display_label, description, skip_stages_json, isolation,
                unattended, delivery_mode, delivery_target_repo, enabled, source, project_id,
                tags_json, bundled_hash, deleted_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            self._insert_params(profile),
        )

    def _ensure_no_active_duplicate(self, profile: BuildProfile) -> None:
        row = self.db.fetchone(
            """
            SELECT id
              FROM build_profiles
             WHERE name = %s
               AND source = %s
               AND ((project_id IS NULL AND %s::text IS NULL) OR project_id = %s)
               AND deleted_at IS NULL
             LIMIT 1
            """,
            (profile.name, profile.source, profile.project_id, profile.project_id),
        )
        if row is not None:
            scope = "global" if profile.project_id is None else f"project {profile.project_id}"
            raise BuildProfileError(
                f"Active build profile '{profile.name}' already exists for {profile.source} {scope}"
            )

    def _update_profile(self, profile: BuildProfile, *, restore_deleted: bool = False) -> None:
        deleted_assignment = "deleted_at = NULL," if restore_deleted else ""
        self.db.execute(
            f"""
            UPDATE build_profiles
               SET display_label = %s,
                   description = %s,
                   skip_stages_json = %s,
                   isolation = %s,
                   unattended = %s,
                   delivery_mode = %s,
                   delivery_target_repo = %s,
                   enabled = %s,
                   tags_json = %s,
                   bundled_hash = %s,
                   {deleted_assignment}
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,  # nosec B608 # deleted_assignment is controlled by a boolean.
            (
                profile.display_label,
                profile.description,
                json.dumps(profile.skip_stages),
                profile.isolation,
                bool(profile.unattended),
                profile.delivery_mode,
                profile.delivery_target_repo,
                bool(profile.enabled),
                json.dumps(profile.tags or []),
                profile.bundled_hash,
                profile.id,
            ),
        )

    @staticmethod
    def _insert_params(profile: BuildProfile) -> tuple[Any, ...]:
        return (
            profile.id,
            profile.name,
            profile.display_label,
            profile.description,
            json.dumps(profile.skip_stages),
            profile.isolation,
            bool(profile.unattended),
            profile.delivery_mode,
            profile.delivery_target_repo,
            bool(profile.enabled),
            profile.source,
            profile.project_id,
            json.dumps(profile.tags or []),
            profile.bundled_hash,
        )

    def _profile_from_row(self, row: Mapping[str, Any]) -> BuildProfile:
        profile = BuildProfile(
            id=row["id"],
            name=row["name"],
            display_label=row["display_label"],
            description=row["description"],
            skip_stages=_json_list(row["skip_stages_json"], "skip_stages_json"),
            isolation=row["isolation"],
            unattended=bool(row["unattended"]),
            delivery_mode=row["delivery_mode"],
            delivery_target_repo=row["delivery_target_repo"],
            enabled=bool(row["enabled"]),
            source=row["source"],
            project_id=row["project_id"],
            tags=_json_list(row["tags_json"], "tags_json"),
            bundled_hash=row["bundled_hash"],
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return replace(profile, state=self._state(profile))

    def _state(self, profile: BuildProfile) -> BuildProfileState:
        if profile.deleted_at is not None:
            return "deleted"
        if profile.bundled_hash is None:
            return "custom"
        return "edited" if self._profile_hash(profile) != profile.bundled_hash else "bundled"

    @staticmethod
    def _profile_payload(profile: BuildProfile) -> dict[str, Any]:
        return {
            "name": profile.name,
            "display_label": profile.display_label,
            "description": profile.description,
            "skip_stages": list(profile.skip_stages),
            "isolation": profile.isolation,
            "unattended": profile.unattended,
            "delivery_mode": profile.delivery_mode,
            "delivery_target_repo": profile.delivery_target_repo,
            "enabled": profile.enabled,
            "tags": list(profile.tags or []),
        }

    @staticmethod
    def _profile_dataclass_payload(profile: BuildProfile) -> dict[str, Any]:
        return {
            "id": profile.id,
            "name": profile.name,
            "display_label": profile.display_label,
            "description": profile.description,
            "skip_stages": list(profile.skip_stages),
            "isolation": profile.isolation,
            "unattended": profile.unattended,
            "delivery_mode": profile.delivery_mode,
            "delivery_target_repo": profile.delivery_target_repo,
            "enabled": profile.enabled,
            "source": profile.source,
            "project_id": profile.project_id,
            "tags": list(profile.tags or []),
            "bundled_hash": profile.bundled_hash,
            "deleted_at": profile.deleted_at,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
            "state": profile.state,
        }

    @staticmethod
    def _row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "display_label": row["display_label"],
            "description": row["description"],
            "skip_stages": _json_list(row["skip_stages_json"], "skip_stages_json"),
            "isolation": row["isolation"],
            "unattended": bool(row["unattended"]),
            "delivery_mode": row["delivery_mode"],
            "delivery_target_repo": row["delivery_target_repo"],
            "enabled": bool(row["enabled"]),
            "tags": _json_list(row["tags_json"], "tags_json"),
        }

    @staticmethod
    def _legacy_row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "display_label": row["display_label"],
            "description": row["description"],
            "skip_stages": _json_list(row["skip_stages_json"], "skip_stages_json"),
            "isolation": row["isolation"],
            "unattended": bool(row["unattended"]),
            "enabled": bool(row["enabled"]),
            "tags": _json_list(row["tags_json"], "tags_json"),
        }

    @staticmethod
    def _validate_profile(profile: BuildProfile) -> None:
        _validate_profile_name(profile.name)
        if profile.isolation not in {"none", "worktree", "clone"}:
            raise BuildProfileError("isolation must be one of: none, worktree, clone")
        if profile.delivery_mode not in {"auto", "pull_request"}:
            raise BuildProfileError("delivery_mode must be one of: auto, pull_request")
        _validate_delivery_target_repo(profile.delivery_target_repo)
        if profile.source not in _SOURCES:
            raise BuildProfileError("source must be installed or project")
        if profile.source == "installed" and profile.project_id is not None:
            raise BuildProfileError("installed build profiles must be global")
        if not profile.display_label:
            raise BuildProfileError("display_label is required")


def _required_string(raw: dict[str, Any], key: str, index: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise BuildProfileError(f"Build profile {index} {key} must be a non-empty string")
    return value


def _validate_profile_name(name: str) -> None:
    if not name or not name.strip():
        raise BuildProfileError("build profile name is required")
    if name.lower() in _RESERVED_PROFILE_NAMES:
        raise BuildProfileError(f"build profile name '{name}' is reserved")


def _validate_delivery_target_repo(repo: str | None) -> None:
    if repo is None:
        return
    owner, separator, name = repo.partition("/")
    if (
        not separator
        or not owner
        or not name
        or "/" in name
        or owner.strip() != owner
        or name.strip() != name
    ):
        raise BuildProfileError(f"delivery_target_repo {repo!r} is invalid; expected 'owner/repo'")


def _json_list(raw: str | None, field_name: str) -> list[str]:
    if raw is None:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Malformed build profile JSON list in %s: %s", field_name, exc)
        return []
    if not isinstance(payload, list):
        logger.warning(
            "Build profile JSON field %s must contain a list, got %s",
            field_name,
            type(payload).__name__,
        )
        return []
    return [str(item) for item in payload]


def sync_bundled_build_profiles(db: HubDatabase) -> dict[str, int]:
    result = BuildProfileLoader().sync(db)
    return {
        "synced": result.upserted,
        "skipped": result.skipped,
        "soft_deleted": result.soft_deleted,
    }


__all__ = [
    "BuildProfile",
    "BuildProfileError",
    "BuildProfileLoader",
    "BuildProfileManager",
    "BuildProfileSource",
    "sync_bundled_build_profiles",
]
