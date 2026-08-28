"""Handlers for lossless, progressively disclosed skill delivery."""

from __future__ import annotations

import json
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.mcp_proxy.tools.skills._paging import (
    CursorError,
    CursorState,
    ResponseView,
    build_content_page,
    content_hash,
    decode_cursor,
)
from gobby.storage.skills import Skill, SkillFile
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)

MAX_MANIFEST_FILE_ENTRIES = 100
MAX_MANIFEST_DIRECTORY_ENTRIES = 20
MAX_MANIFEST_RESPONSE_BYTES = 15_000
MAX_EMBEDDED_MANIFEST_BYTES = 6_000
MAX_BRIEF_REFERENCES_BYTES = 4_000


def _encoded_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _oversized_warning(count: int) -> str | None:
    if count == 0:
        return None
    return f"{count} legacy file path(s) exceeded 1024 UTF-8 bytes and were omitted"


def _build_manifest(snapshot: dict[str, Any], skill_id: str) -> dict[str, Any]:
    entries = list(snapshot["files"][:MAX_MANIFEST_FILE_ENTRIES])
    scripts = snapshot["scripts"]
    directories = dict(scripts["per_top_level_dir"])
    manifest: dict[str, Any] = {
        "entries": entries,
        "total_files": snapshot["total_files"],
        "remaining_file_count": snapshot["total_files"] - len(entries),
        "scripts": {
            "total_files": scripts["total_files"],
            "total_bytes": scripts["total_bytes"],
            "per_top_level_dir": directories,
            "remaining_directory_count": scripts["remaining_directory_count"],
            "remaining_file_count": scripts["remaining_file_count"],
            "note": (
                "Use materialize_skill_scripts for execution and get_skill_file "
                "for individual reads."
            ),
        },
        "omitted_oversized_path_count": snapshot["omitted_oversized_path_count"],
        "overflow_note": (
            f"Use get_skill_files(skill_id='{skill_id}') with path_prefix and after_path "
            "to discover omitted entries."
        ),
    }
    warning = _oversized_warning(snapshot["omitted_oversized_path_count"])
    if warning is not None:
        manifest["warning"] = warning

    while _encoded_size(manifest) > MAX_EMBEDDED_MANIFEST_BYTES:
        if entries:
            entries.pop()
            manifest["remaining_file_count"] += 1
            continue
        if directories:
            name = sorted(directories)[-1]
            file_count = int(directories.pop(name))
            script_manifest = manifest["scripts"]
            script_manifest["remaining_directory_count"] += 1
            script_manifest["remaining_file_count"] += file_count
            continue
        raise ValueError("Skill manifest metadata exceeds the response byte budget")
    return manifest


def _build_file_page(snapshot: dict[str, Any]) -> dict[str, Any]:
    entries = list(snapshot["files"][:MAX_MANIFEST_FILE_ENTRIES])
    response: dict[str, Any] = {
        "success": True,
        "skill_id": snapshot["skill_id"],
        "name": snapshot["name"],
        "files": entries,
        "total_files": snapshot["total_files"],
        "remaining_file_count": snapshot["total_files"] - len(entries),
        "next_after_path": None,
        "omitted_oversized_path_count": snapshot["omitted_oversized_path_count"],
    }
    warning = _oversized_warning(snapshot["omitted_oversized_path_count"])
    if warning is not None:
        response["warning"] = warning

    while True:
        response["remaining_file_count"] = snapshot["total_files"] - len(entries)
        response["next_after_path"] = (
            entries[-1]["path"] if entries and response["remaining_file_count"] > 0 else None
        )
        if _encoded_size(response) <= MAX_MANIFEST_RESPONSE_BYTES:
            return response
        if not entries:
            raise ValueError("Skill file page metadata exceeds the response byte budget")
        entries.pop()


def _serve_scan_error(
    skill_name: str,
    source_type: str | None,
    content: str,
    path: str = "SKILL.md",
) -> str | None:
    """Return a refusal message when complete external content fails its serve scan."""
    from gobby.skills.scanner import is_external_source, scan_served_content

    if not is_external_source(source_type):
        return None
    try:
        scan_result = scan_served_content(content, name=skill_name, path=path)
    except ImportError:
        return (
            f"clawcare is not installed; refusing to serve skill '{skill_name}' from external "
            f"source ({source_type}) without a security scan"
        )
    if not scan_result["is_safe"]:
        return (
            f"Skill '{skill_name}' content at {path} failed security scan "
            f"(max severity: {scan_result['max_severity']}); refusing to serve"
        )
    return None


def _error(error_code: str, message: str, *, restart: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": False,
        "error_code": error_code,
        "message": message,
    }
    if restart is not None:
        response["restart"] = restart
    return response


def _brief_references(snapshot: dict[str, Any]) -> dict[str, Any]:
    candidates = [entry for entry in snapshot["files"] if entry["file_type"] == "reference"]
    entries: list[dict[str, Any]] = []
    omitted_from_snapshot = max(0, snapshot["total_files"] - len(snapshot["files"]))
    for candidate in candidates:
        projected = {"path": candidate["path"], "size_bytes": candidate["size_bytes"]}
        if _encoded_size({"entries": [*entries, projected]}) > MAX_BRIEF_REFERENCES_BYTES:
            break
        entries.append(projected)
    remaining = len(candidates) - len(entries) + omitted_from_snapshot
    return {
        "entries": entries,
        "remaining_count": remaining,
        "next_after_path": entries[-1]["path"] if entries and remaining else None,
    }


def _brief_skill(skill: Skill, content: str) -> dict[str, Any]:
    result: dict[str, Any] = {"name": skill.name, "content": content}
    if skill.compatibility:
        result["compatibility"] = skill.compatibility
    if skill.allowed_tools:
        result["allowed_tools"] = skill.allowed_tools
    return result


def _full_skill(skill: Skill, content: str) -> dict[str, Any]:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "content": content,
        "version": skill.version,
        "license": skill.license,
        "compatibility": skill.compatibility,
        "allowed_tools": skill.allowed_tools or [],
        "metadata": skill.metadata or {},
        "enabled": skill.enabled,
        "source": {
            "scope": skill.source,
            "type": skill.source_type,
            "path": skill.source_path,
            "ref": skill.source_ref,
        },
    }


def _brief_file(skill: Skill, skill_file: SkillFile, content: str) -> dict[str, Any]:
    return {"skill_name": skill.name, "path": skill_file.path, "content": content}


def _full_file(skill: Skill, skill_file: SkillFile, content: str) -> dict[str, Any]:
    return {
        "skill_id": skill.id,
        "skill_name": skill.name,
        "path": skill_file.path,
        "file_type": skill_file.file_type,
        "size_bytes": skill_file.size_bytes,
        "content_hash": skill_file.content_hash,
        "content": content,
    }


def _resolve_level(skill: Skill, requested: str | None) -> tuple[str | None, str | None]:
    gobby_meta = (skill.metadata or {}).get("gobby") or {}
    levels = gobby_meta.get("levels") if isinstance(gobby_meta, dict) else None
    if not isinstance(levels, list) or not levels:
        if requested is not None:
            return None, f"Skill '{skill.name}' does not declare levels"
        return None, None
    if requested is not None and requested not in levels:
        return None, (
            f"Invalid level '{requested}' for skill '{skill.name}'. "
            f"Valid levels: {', '.join(levels)}"
        )
    return requested or gobby_meta.get("default_level") or levels[0], None


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register brief-by-default, cursor-paged skill delivery tools."""

    async def record_completed_load(
        skill: Skill,
        effective_level: str | None,
        session_id: str | None,
    ) -> None:
        session_reference = session_id or get_current_session_id()
        if not session_reference:
            return
        try:
            resolved_session_id = await ctx.run_db(
                ctx.session_manager.resolve_session_reference,
                session_reference,
                project_id=ctx.project_id,
            )
            await ctx.run_db(
                ctx.session_manager.record_skills_used,
                resolved_session_id,
                [skill.name],
            )
            from gobby.workflows.state_manager import SessionVariableManager

            variables = SessionVariableManager(ctx.db)
            await ctx.run_db(
                variables.append_to_set_variable,
                resolved_session_id,
                "loaded_skills",
                [skill.name],
                preserve_order=True,
            )
            if effective_level is not None:
                await ctx.run_db(
                    variables.set_variable,
                    resolved_session_id,
                    f"{skill.name.replace('-', '_')}_level",
                    effective_level,
                )
        except Exception as exc:
            logger.debug(
                "Best-effort completed skill tracking failed for session %s: %s",
                session_reference,
                exc,
            )

    @registry.tool(
        name="get_skill",
        description=(
            "Get exact skill instructions using a brief projection by default. "
            "Follow next_cursor until null before treating the skill as loaded."
        ),
    )
    async def get_skill(
        name: str | None = None,
        skill_id: str | None = None,
        session_id: str | None = None,
        level: str | None = None,
        cursor: str | None = None,
        brief: bool = True,
    ) -> dict[str, Any]:
        try:
            continuation = cursor is not None
            state: CursorState | None = None
            if continuation:
                assert cursor is not None
                if name is not None or skill_id is not None or level is not None:
                    return _error(
                        "invalid_cursor",
                        "Cursor continuation cannot include initial lookup arguments",
                        restart="Call get_skill again without cursor to restart.",
                    )
                try:
                    state = decode_cursor(cursor, expected_kind="skill")
                except CursorError as exc:
                    return _error(
                        "invalid_cursor",
                        str(exc),
                        restart="Call get_skill again without cursor to restart.",
                    )
                skill_id = state.skill_id
            elif not skill_id and not name:
                return _error("invalid_request", "Either name or skill_id is required")

            snapshot = await ctx.run_db(
                ctx.storage.get_skill_with_manifest,
                skill_id=skill_id,
                name=name,
                project_id=ctx.project_id,
                file_limit=MAX_MANIFEST_FILE_ENTRIES,
                directory_limit=MAX_MANIFEST_DIRECTORY_ENTRIES,
            )
            if snapshot is None:
                if continuation:
                    return _error(
                        "stale_cursor",
                        "Cursor skill no longer exists",
                        restart="Call get_skill again without cursor to restart.",
                    )
                return _error("not_found", f"Skill not found: {skill_id or name}")
            skill = snapshot["skill"]

            if state is None:
                effective_level, level_error = _resolve_level(skill, level)
                if level_error is not None:
                    return _error("invalid_level", level_error)
                view: ResponseView = "brief" if brief else "full"
            else:
                effective_level = state.level
                view = state.view

            complete_content = skill.content
            if effective_level is not None:
                complete_content = f"Active level: {effective_level}\n\n{complete_content}"
            hash_value = content_hash(complete_content)
            if state is not None and state.content_hash != hash_value:
                return _error(
                    "stale_cursor",
                    "Skill content changed after the cursor was issued",
                    restart="Call get_skill again without cursor to restart.",
                )
            scan_message = _serve_scan_error(skill.name, skill.source_type, complete_content)
            if scan_message is not None:
                return _error("security_scan_failed", scan_message)

            if state is None:
                state = CursorState(
                    kind="skill",
                    view=view,
                    skill_id=skill.id,
                    path=None,
                    level=effective_level,
                    content_hash=hash_value,
                    offset=0,
                )

            references = _brief_references(snapshot)
            files = _build_manifest(snapshot, skill.id)

            def response_factory(
                page_content: str,
                start_byte: int,
                end_byte: int,
                complete: bool,
                next_cursor: str | None,
            ) -> dict[str, Any]:
                if view == "brief":
                    return {
                        "success": True,
                        "view": view,
                        "skill": _brief_skill(skill, page_content),
                        "page": {"complete": complete, "next_cursor": next_cursor},
                        "references": references,
                    }
                return {
                    "success": True,
                    "view": view,
                    "skill": _full_skill(skill, page_content),
                    "page": {
                        "start_byte": start_byte,
                        "end_byte": end_byte,
                        "total_bytes": len(complete_content.encode("utf-8")),
                        "complete": complete,
                        "next_cursor": next_cursor,
                        "content_hash": hash_value,
                    },
                    "files": files,
                }

            response = build_content_page(complete_content, state, response_factory)
            if response["page"]["complete"]:
                await record_completed_load(skill, effective_level, session_id)
            return response
        except CursorError as exc:
            return _error(
                "invalid_cursor",
                str(exc),
                restart="Call get_skill again without cursor to restart.",
            )
        except Exception as exc:
            return _error("internal_error", str(exc))

    @registry.tool(
        name="get_skill_files",
        description=(
            "List one byte-bounded page of skill files. Continue with the returned "
            "next_after_path value."
        ),
    )
    async def get_skill_files_tool(
        name: str | None = None,
        skill_id: str | None = None,
        path_prefix: str | None = None,
        file_type: str | None = None,
        after_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            if not skill_id and not name:
                return _error("invalid_request", "Either name or skill_id is required")
            snapshot = await ctx.run_db(
                ctx.storage.get_skill_file_page,
                skill_id,
                name=name,
                project_id=ctx.project_id,
                path_prefix=path_prefix,
                file_type=file_type,
                after_path=after_path,
                limit=MAX_MANIFEST_FILE_ENTRIES + 1,
            )
            if snapshot is None:
                return _error("not_found", f"Skill not found: {skill_id or name}")
            return _build_file_page(snapshot)
        except Exception as exc:
            return _error("internal_error", str(exc))

    @registry.tool(
        name="get_skill_file",
        description=(
            "Get one exact skill file using a brief projection by default. "
            "Follow next_cursor until null."
        ),
    )
    def get_skill_file_tool(
        path: str | None = None,
        name: str | None = None,
        skill_id: str | None = None,
        cursor: str | None = None,
        brief: bool = True,
    ) -> dict[str, Any]:
        try:
            continuation = cursor is not None
            state: CursorState | None = None
            if continuation:
                assert cursor is not None
                if path is not None or name is not None or skill_id is not None:
                    return _error(
                        "invalid_cursor",
                        "Cursor continuation cannot include initial lookup arguments",
                        restart="Call get_skill_file again without cursor to restart.",
                    )
                try:
                    state = decode_cursor(cursor, expected_kind="file")
                except CursorError as exc:
                    return _error(
                        "invalid_cursor",
                        str(exc),
                        restart="Call get_skill_file again without cursor to restart.",
                    )
                skill_id = state.skill_id
                path = state.path
            else:
                if not path:
                    return _error("invalid_request", "path is required")
                if not skill_id and not name:
                    return _error("invalid_request", "Either name or skill_id is required")

            skill = None
            if skill_id:
                try:
                    skill = ctx.storage.get_skill(skill_id)
                except ValueError:
                    pass
            if skill is None and name:
                skill = ctx.storage.get_by_name(name, project_id=ctx.project_id)
            if skill is None:
                if continuation:
                    return _error(
                        "stale_cursor",
                        "Cursor skill no longer exists",
                        restart="Call get_skill_file again without cursor to restart.",
                    )
                return _error("not_found", f"Skill not found: {skill_id or name}")

            skill_file = ctx.storage.get_skill_file(skill.id, path or "")
            if skill_file is None:
                if continuation:
                    return _error(
                        "stale_cursor",
                        "Cursor file no longer exists",
                        restart="Call get_skill_file again without cursor to restart.",
                    )
                return _error("not_found", f"File not found: {path}")

            hash_value = content_hash(skill_file.content)
            if state is not None and state.content_hash != hash_value:
                return _error(
                    "stale_cursor",
                    "Skill file content changed after the cursor was issued",
                    restart="Call get_skill_file again without cursor to restart.",
                )
            scan_message = _serve_scan_error(
                skill.name,
                skill.source_type,
                skill_file.content,
                path=skill_file.path,
            )
            if scan_message is not None:
                return _error("security_scan_failed", scan_message)

            view: ResponseView = state.view if state is not None else ("brief" if brief else "full")
            if state is None:
                state = CursorState(
                    kind="file",
                    view=view,
                    skill_id=skill.id,
                    path=skill_file.path,
                    level=None,
                    content_hash=hash_value,
                    offset=0,
                )

            def response_factory(
                page_content: str,
                start_byte: int,
                end_byte: int,
                complete: bool,
                next_cursor: str | None,
            ) -> dict[str, Any]:
                if view == "brief":
                    return {
                        "success": True,
                        "view": view,
                        "file": _brief_file(skill, skill_file, page_content),
                        "page": {"complete": complete, "next_cursor": next_cursor},
                    }
                return {
                    "success": True,
                    "view": view,
                    "file": _full_file(skill, skill_file, page_content),
                    "page": {
                        "start_byte": start_byte,
                        "end_byte": end_byte,
                        "total_bytes": len(skill_file.content.encode("utf-8")),
                        "complete": complete,
                        "next_cursor": next_cursor,
                        "content_hash": hash_value,
                    },
                }

            return build_content_page(skill_file.content, state, response_factory)
        except CursorError as exc:
            return _error(
                "invalid_cursor",
                str(exc),
                restart="Call get_skill_file again without cursor to restart.",
            )
        except Exception as exc:
            return _error("internal_error", str(exc))
