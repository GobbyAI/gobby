"""Paged, runtime-grounded tool loop for task validation."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from gobby.ai import (
    AIAdapterStyle,
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
    InvocationRecord,
    ToolChatRequest,
    ToolChatResult,
    ToolChatService,
    ToolLoopLimits,
    ToolPolicy,
)
from gobby.config.tasks import TaskValidationConfig
from gobby.tasks.commits import DOC_EXTENSIONS
from gobby.tasks.diff_paging import (
    MAX_COMMITS_LIMIT,
    MAX_CURSOR_OFFSET,
    MAX_LIMIT_BYTES,
    MAX_MANIFEST_LIMIT,
    MIN_LIMIT_BYTES,
    CommitCursorPage,
    DiffPage,
    DiffPagingError,
    ManifestItem,
    TaskManagerProtocol,
    decode_content,
    get_task_diff_page,
    read_file_at_commit,
)

TOOL_LOOP_MAX_TURNS = 4
TOOL_LOOP_MAX_CALLS = 12
TOOL_LOOP_TOOL_TIMEOUT_SECONDS = 30.0
FIRST_COMMITS_PAGE_LIMIT = 20
_RUNTIME_ADAPTER_STYLES = (
    AIAdapterStyle.LLM_PROVIDER,
    AIAdapterStyle.LOCAL,
    AIAdapterStyle.OPENAI_COMPATIBLE,
)
_VERDICT_STATUSES = frozenset({"valid", "invalid", "pending"})


@dataclass(frozen=True)
class PreparedValidationDiff:
    """Complete manifest metadata and the canonical linked-commit set."""

    canonical_commits: tuple[str, ...]
    first_commits_page: CommitCursorPage
    manifest_items: tuple[ManifestItem, ...]
    manifest_count: int
    snapshot_hash: str
    view_hash: str


@dataclass(frozen=True)
class ToolLoopVerdict:
    """Normalized tool-loop verdict with runtime-verified provenance."""

    status: Literal["valid", "invalid", "pending"]
    feedback: str | None
    blocking_reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    evidence_complete: bool
    trace_summary: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _LinkedCommitTask:
    commits: tuple[str, ...]


class _LinkedCommitManager:
    """Frozen task view used by validation builtins for one investigation."""

    def __init__(self, task_id: str, commits: tuple[str, ...]) -> None:
        self._task_id = task_id
        self._task = _LinkedCommitTask(commits=commits)

    def get_task(self, task_id: str) -> object | None:
        return self._task if task_id == self._task_id else None


def prepare_validation_diff(
    task_id: str,
    task_manager: TaskManagerProtocol,
    *,
    repo_path: str,
    commits_page_limit: int = FIRST_COMMITS_PAGE_LIMIT,
    manifest_page_limit: int = MAX_MANIFEST_LIMIT,
) -> PreparedValidationDiff:
    """Page canonical commit and name-status metadata without collecting raw diff text."""
    page = get_task_diff_page(
        task_id,
        task_manager,
        include_uncommitted=False,
        cwd=repo_path,
        limit_bytes=MIN_LIMIT_BYTES,
        commits_limit=commits_page_limit,
        manifest_limit=manifest_page_limit,
    )
    first_commits_page = page["commits"]
    canonical_commits = list(first_commits_page["items"])
    manifest_items = list(page["manifest"]["items"])
    commits_complete = first_commits_page["complete"]
    manifest_complete = page["manifest"]["complete"]
    commits_offset = first_commits_page["cursor_end"]
    manifest_offset = page["manifest"]["cursor_end"]

    while not commits_complete or not manifest_complete:
        previous_commits_offset = commits_offset
        previous_manifest_offset = manifest_offset
        page = get_task_diff_page(
            task_id,
            task_manager,
            include_uncommitted=False,
            cwd=repo_path,
            limit_bytes=MIN_LIMIT_BYTES,
            commits_offset=commits_offset,
            commits_limit=commits_page_limit if not commits_complete else 0,
            manifest_offset=manifest_offset,
            manifest_limit=manifest_page_limit if not manifest_complete else 0,
            snapshot_hash=page["snapshot_hash"],
            view_hash=page["view_hash"],
        )
        commit_page = page["commits"]
        manifest_page = page["manifest"]
        if not commits_complete:
            canonical_commits.extend(commit_page["items"])
            commits_offset = commit_page["cursor_end"]
            commits_complete = commit_page["complete"]
            if commits_offset <= previous_commits_offset:
                raise DiffPagingError(
                    "paging_stalled", "linked-commit paging made no cursor progress"
                )
        if not manifest_complete:
            manifest_items.extend(manifest_page["items"])
            manifest_offset = manifest_page["cursor_end"]
            manifest_complete = manifest_page["complete"]
            if manifest_offset <= previous_manifest_offset:
                raise DiffPagingError(
                    "paging_stalled", "changed-file paging made no cursor progress"
                )

    commit_total = first_commits_page["total"]
    manifest_total = page["manifest"]["total"]
    if len(canonical_commits) != commit_total or len(manifest_items) != manifest_total:
        raise DiffPagingError("paging_incomplete", "validation metadata paging ended early")
    return PreparedValidationDiff(
        canonical_commits=tuple(canonical_commits),
        first_commits_page=first_commits_page,
        manifest_items=tuple(manifest_items),
        manifest_count=manifest_total,
        snapshot_hash=page["snapshot_hash"],
        view_hash=page["view_hash"],
    )


def is_doc_only_manifest(manifest_items: Sequence[ManifestItem]) -> bool:
    """Return whether every path in a complete name-status manifest is documentation."""
    if not manifest_items:
        return False
    for item in manifest_items:
        path = Path(os.fsdecode(decode_content(item["path"])))
        if path.suffix.lower() not in DOC_EXTENSIONS:
            return False
    return True


def _optional_str(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    return value if isinstance(value, str) else None


def _required_str(arguments: Mapping[str, object], name: str) -> str:
    value = _optional_str(arguments, name)
    if value is None:
        raise DiffPagingError("invalid_paging_argument", f"{name} is required")
    return value


def _int_argument(arguments: Mapping[str, object], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiffPagingError("invalid_paging_argument", f"{name} must be an integer")
    return value


def _page_tokens(arguments: Mapping[str, object]) -> tuple[str | None, str | None]:
    return _optional_str(arguments, "snapshot_hash"), _optional_str(arguments, "view_hash")


def _diff_payload(page: DiffPage) -> dict[str, object]:
    return {
        "content": page["content"],
        "byte_start": page["byte_start"],
        "byte_end": page["byte_end"],
        "total_bytes": page["total_bytes"],
        "complete": page["complete"],
        "snapshot_hash": page["snapshot_hash"],
        "view_hash": page["view_hash"],
    }


def _diff_result(page: DiffPage, selector: dict[str, object]) -> BuiltinToolResult:
    return BuiltinToolResult(
        payload=_diff_payload(page),
        selector=selector,
        range={
            "byte_start": page["byte_start"],
            "byte_end": page["byte_end"],
            "total_bytes": page["total_bytes"],
        },
        complete=page["complete"],
        content_hash=page["snapshot_hash"],
    )


def _linked_commit(commit: str | None, canonical: frozenset[str]) -> str | None:
    if commit is not None and commit not in canonical:
        raise DiffPagingError("commit_not_linked", "commit is not linked to the task")
    return commit


def _string_property(description: str) -> dict[str, object]:
    return {"type": "string", "description": description}


def _integer_property(*, default: int, minimum: int, maximum: int) -> dict[str, object]:
    return {"type": "integer", "default": default, "minimum": minimum, "maximum": maximum}


def _token_properties() -> dict[str, dict[str, object]]:
    return {
        "snapshot_hash": _string_property("Snapshot token returned by the first page."),
        "view_hash": _string_property("View token returned by the first page."),
    }


def build_validation_builtins(
    *,
    task_id: str,
    repo_path: str,
    canonical_commits: Sequence[str],
    preview_bytes: int,
) -> tuple[BuiltinToolSpec, ...]:
    """Create bounded diff builtins closed over one canonical linked-commit set."""
    commits = tuple(canonical_commits)
    canonical_set = frozenset(commits)
    manager = _LinkedCommitManager(task_id, commits)

    async def list_changed_files_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        snapshot_hash, view_hash = _page_tokens(arguments)
        page = await asyncio.to_thread(
            get_task_diff_page,
            task_id,
            manager,
            cwd=repo_path,
            limit_bytes=MIN_LIMIT_BYTES,
            commits_limit=0,
            manifest_offset=_int_argument(arguments, "offset", 0),
            manifest_limit=_int_argument(arguments, "limit", MAX_MANIFEST_LIMIT),
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
            max_payload_bytes=context.max_payload_bytes,
            subprocess_deadline=context.subprocess_deadline,
        )
        manifest = page["manifest"]
        return BuiltinToolResult(
            payload={
                "manifest": manifest,
                "snapshot_hash": page["snapshot_hash"],
                "view_hash": page["view_hash"],
            },
            selector={"kind": "changed_files", "task_id": task_id},
            range={
                "cursor_offset": manifest["cursor_offset"],
                "cursor_end": manifest["cursor_end"],
                "total": manifest["total"],
            },
            complete=manifest["complete"],
            content_hash=page["snapshot_hash"],
        )

    async def list_linked_commits_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        snapshot_hash, view_hash = _page_tokens(arguments)
        page = await asyncio.to_thread(
            get_task_diff_page,
            task_id,
            manager,
            cwd=repo_path,
            limit_bytes=MIN_LIMIT_BYTES,
            commits_offset=_int_argument(arguments, "offset", 0),
            commits_limit=_int_argument(arguments, "limit", FIRST_COMMITS_PAGE_LIMIT),
            manifest_limit=0,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
            max_payload_bytes=context.max_payload_bytes,
            subprocess_deadline=context.subprocess_deadline,
        )
        commit_page = page["commits"]
        return BuiltinToolResult(
            payload={
                "commits": commit_page,
                "snapshot_hash": page["snapshot_hash"],
                "view_hash": page["view_hash"],
            },
            selector={"kind": "linked_commits", "task_id": task_id},
            range={
                "cursor_offset": commit_page["cursor_offset"],
                "cursor_end": commit_page["cursor_end"],
                "total": commit_page["total"],
            },
            complete=commit_page["complete"],
            content_hash=page["snapshot_hash"],
        )

    async def read_task_diff_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        snapshot_hash, view_hash = _page_tokens(arguments)
        commit = _linked_commit(_optional_str(arguments, "commit"), canonical_set)
        path_selector = _optional_str(arguments, "path_selector")
        page = await asyncio.to_thread(
            get_task_diff_page,
            task_id,
            manager,
            cwd=repo_path,
            commit=commit,
            path_selector=path_selector,
            offset_bytes=_int_argument(arguments, "offset_bytes", 0),
            limit_bytes=_int_argument(arguments, "limit_bytes", preview_bytes),
            commits_limit=0,
            manifest_limit=0,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
            max_payload_bytes=context.max_payload_bytes,
            subprocess_deadline=context.subprocess_deadline,
        )
        selector: dict[str, object] = {"kind": "task_diff", "task_id": task_id}
        if commit is not None:
            selector["commit"] = commit
        if path_selector is not None:
            selector["path_selector"] = path_selector
        return _diff_result(page, selector)

    async def read_file_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        snapshot_hash, view_hash = _page_tokens(arguments)
        commit = cast(str, _linked_commit(_required_str(arguments, "commit"), canonical_set))
        path_selector = _required_str(arguments, "path_selector")
        page = await asyncio.to_thread(
            read_file_at_commit,
            task_id,
            manager,
            cwd=repo_path,
            commit=commit,
            path_selector=path_selector,
            offset_bytes=_int_argument(arguments, "offset_bytes", 0),
            limit_bytes=_int_argument(arguments, "limit_bytes", preview_bytes),
            commits_limit=0,
            manifest_limit=0,
            snapshot_hash=snapshot_hash,
            view_hash=view_hash,
            max_payload_bytes=context.max_payload_bytes,
            subprocess_deadline=context.subprocess_deadline,
        )
        return _diff_result(
            page,
            {
                "kind": "file_at_commit",
                "task_id": task_id,
                "commit": commit,
                "path_selector": path_selector,
            },
        )

    cursor_properties = {
        "offset": _integer_property(default=0, minimum=0, maximum=MAX_CURSOR_OFFSET),
        "limit": _integer_property(
            default=FIRST_COMMITS_PAGE_LIMIT, minimum=0, maximum=MAX_COMMITS_LIMIT
        ),
        **_token_properties(),
    }
    manifest_properties = {
        **cursor_properties,
        "limit": _integer_property(
            default=MAX_MANIFEST_LIMIT, minimum=0, maximum=MAX_MANIFEST_LIMIT
        ),
    }
    diff_properties = {
        "offset_bytes": _integer_property(default=0, minimum=0, maximum=MAX_CURSOR_OFFSET),
        "limit_bytes": _integer_property(
            default=preview_bytes, minimum=MIN_LIMIT_BYTES, maximum=MAX_LIMIT_BYTES
        ),
        "commit": _string_property("Canonical linked commit SHA."),
        "path_selector": _string_property("Opaque selector from list_changed_files."),
        **_token_properties(),
    }
    return (
        BuiltinToolSpec(
            name="list_changed_files",
            description="Return one cursor-bounded page of the complete name-status manifest.",
            input_schema={
                "type": "object",
                "properties": manifest_properties,
                "additionalProperties": False,
            },
            handler=list_changed_files_handler,
        ),
        BuiltinToolSpec(
            name="list_linked_commits",
            description="Return one cursor-bounded page of canonical linked commit SHAs.",
            input_schema={
                "type": "object",
                "properties": cursor_properties,
                "additionalProperties": False,
            },
            handler=list_linked_commits_handler,
        ),
        BuiltinToolSpec(
            name="read_task_diff",
            description="Read one byte-bounded task, commit, or selected-file diff page.",
            input_schema={
                "type": "object",
                "properties": diff_properties,
                "additionalProperties": False,
            },
            handler=read_task_diff_handler,
        ),
        BuiltinToolSpec(
            name="read_file_at_commit",
            description="Read one byte-bounded page of a manifest-selected file at a linked commit.",
            input_schema={
                "type": "object",
                "properties": diff_properties,
                "required": ["commit", "path_selector"],
                "additionalProperties": False,
            },
            handler=read_file_handler,
        ),
    )


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _build_prompt(
    *,
    title: str,
    description: str | None,
    validation_criteria: str | None,
    category: str | None,
    verification_evidence: str | None,
    commit_count: int,
    first_commits_page: Mapping[str, object],
    manifest_count: int,
) -> str:
    criteria_label = "Validation criteria" if validation_criteria else "Task description"
    criteria = validation_criteria or description or ""
    evidence = verification_evidence or "No verification/test evidence was supplied."
    category_line = f"Task category: {category}\n" if category else ""
    return (
        "Validate completion using only runtime-issued evidence from the paged tools.\n"
        "Investigate enough pages to judge every criterion. Use list_linked_commits for later "
        "commit pages and list_changed_files for manifest pages. Read relevant diff and file "
        "pages. Cite only evidence_ref values returned by successful tool invocations.\n"
        "Return one JSON object with status, feedback, blocking_reasons, evidence_refs, and "
        "evidence_complete. status must be valid, invalid, or pending. evidence_refs must be "
        "a JSON string array and evidence_complete must be a JSON boolean.\n\n"
        f"Task title: {title}\n"
        f"{category_line}"
        f"{criteria_label}:\n{criteria}\n\n"
        f"Linked commit count: {commit_count}\n"
        f"First linked-commits page: {_compact_json(dict(first_commits_page))}\n"
        f"Changed-file manifest count: {manifest_count}\n\n"
        f"Existing verification/test evidence:\n{evidence}"
    )


def _trace_summary(trace: Sequence[InvocationRecord]) -> tuple[dict[str, object], ...]:
    summary: list[dict[str, object]] = []
    for record in trace:
        item: dict[str, object] = {
            "tool_name": record["tool_name"],
            "ok": record["ok"],
            "error_code": record["error_code"],
            "evidence_ref": record["evidence_ref"],
        }
        if "selector" in record:
            item["selector"] = record["selector"]
        if "range" in record:
            item["range"] = record["range"]
        if "complete" in record:
            item["complete"] = record["complete"]
        if "content_hash" in record:
            item["content_hash"] = record["content_hash"]
        summary.append(item)
    return tuple(summary)


def _feedback_with_provenance(
    feedback: str | None,
    *,
    evidence_refs: Sequence[str],
    evidence_complete: bool,
    trace_summary: Sequence[Mapping[str, object]],
) -> str:
    provenance = _compact_json(
        {
            "mode": "tool_loop",
            "evidence_refs": list(evidence_refs),
            "evidence_complete": evidence_complete,
            "trace": list(trace_summary),
        }
    )
    prefix = feedback.strip() if feedback else "Tool-loop validation produced no feedback."
    return f"{prefix}\n\nTool-loop provenance:\n{provenance}"


def _pending_verdict(
    message: str,
    *,
    evidence_refs: Sequence[str] = (),
    evidence_complete: bool = False,
    trace_summary: Sequence[Mapping[str, object]] = (),
) -> ToolLoopVerdict:
    return ToolLoopVerdict(
        status="pending",
        feedback=_feedback_with_provenance(
            message,
            evidence_refs=evidence_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
        ),
        blocking_reasons=(),
        evidence_refs=tuple(evidence_refs),
        evidence_complete=evidence_complete,
        trace_summary=tuple(dict(item) for item in trace_summary),
    )


def normalize_tool_loop_result(result: ToolChatResult) -> ToolLoopVerdict:
    """Accept only mode-explicit, runtime-issued evidence from successful invocations."""
    trace_summary = _trace_summary(result.trace)
    if result.budget_exhausted:
        return _pending_verdict(
            "Tool-loop validation exhausted its tool-call budget.", trace_summary=trace_summary
        )
    if not result.trace_available:
        return _pending_verdict(
            "Tool-loop validation trace is unavailable; evidence cannot be verified.",
            trace_summary=trace_summary,
        )
    try:
        payload = json.loads(result.text)
    except (json.JSONDecodeError, TypeError):
        return _pending_verdict(
            "Malformed tool-loop validation result: expected one JSON object.",
            trace_summary=trace_summary,
        )
    if not isinstance(payload, dict):
        return _pending_verdict(
            "Malformed tool-loop validation result: expected one JSON object.",
            trace_summary=trace_summary,
        )
    if "evidence_refs" not in payload or "evidence_complete" not in payload:
        return _pending_verdict(
            "Malformed tool-loop validation result: evidence_refs and evidence_complete are required.",
            trace_summary=trace_summary,
        )
    raw_refs = payload["evidence_refs"]
    evidence_complete = payload["evidence_complete"]
    if (
        not isinstance(raw_refs, list)
        or any(not isinstance(ref, str) for ref in raw_refs)
        or not isinstance(evidence_complete, bool)
    ):
        return _pending_verdict(
            "Malformed tool-loop validation result: invalid evidence field types.",
            trace_summary=trace_summary,
        )
    issued_refs = {
        record["evidence_ref"]
        for record in result.trace
        if record["ok"] and record["error_code"] is None and record["evidence_ref"] is not None
    }
    verified_refs = tuple(dict.fromkeys(ref for ref in raw_refs if ref in issued_refs))
    fabricated_refs = tuple(dict.fromkeys(ref for ref in raw_refs if ref not in issued_refs))
    if fabricated_refs:
        return _pending_verdict(
            "Tool-loop verdict cited non-runtime-issued evidence refs: "
            f"{', '.join(fabricated_refs)}.",
            evidence_refs=verified_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
        )
    raw_status = payload.get("status")
    if not isinstance(raw_status, str) or raw_status not in _VERDICT_STATUSES:
        return _pending_verdict(
            "Malformed tool-loop validation result: invalid status.",
            evidence_refs=verified_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
        )
    status = cast(Literal["valid", "invalid", "pending"], raw_status)
    if status != "pending" and (not evidence_complete or not verified_refs):
        return _pending_verdict(
            "Tool-loop validation evidence is incomplete for a terminal verdict.",
            evidence_refs=verified_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
        )
    feedback = payload.get("feedback")
    feedback_text = feedback if isinstance(feedback, str) else None
    raw_reasons = payload.get("blocking_reasons")
    blocking_reasons = (
        tuple(reason for reason in raw_reasons if isinstance(reason, str))
        if isinstance(raw_reasons, list)
        else ()
    )
    if status == "valid":
        blocking_reasons = ()
    elif status == "invalid" and not blocking_reasons:
        return _pending_verdict(
            "Tool-loop invalid verdict did not name unmet criteria or failing gates.",
            evidence_refs=verified_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
        )
    return ToolLoopVerdict(
        status=status,
        feedback=_feedback_with_provenance(
            feedback_text,
            evidence_refs=verified_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
        ),
        blocking_reasons=blocking_reasons,
        evidence_refs=verified_refs,
        evidence_complete=evidence_complete,
        trace_summary=trace_summary,
    )


async def validate_with_tool_loop(
    tool_chat_service: ToolChatService,
    config: TaskValidationConfig,
    *,
    task_id: str,
    title: str,
    description: str | None,
    validation_criteria: str | None,
    category: str | None,
    verification_evidence: str | None,
    repo_path: str,
    canonical_commits: Sequence[str],
    first_commits_page: Mapping[str, object],
    manifest_count: int,
) -> ToolLoopVerdict:
    """Run one bounded validation investigation and verify its cited evidence."""
    commits = tuple(canonical_commits)
    request = ToolChatRequest(
        prompt=_build_prompt(
            title=title,
            description=description,
            validation_criteria=validation_criteria,
            category=category,
            verification_evidence=verification_evidence,
            commit_count=len(commits),
            first_commits_page=first_commits_page,
            manifest_count=manifest_count,
        ),
        system_prompt=config.system_prompt,
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path=repo_path,
        profile=config.profile.value,
        candidates=tuple(config.candidates),
        max_turns=TOOL_LOOP_MAX_TURNS,
        limits=ToolLoopLimits(
            max_turns=TOOL_LOOP_MAX_TURNS,
            max_tool_calls=TOOL_LOOP_MAX_CALLS,
            tool_timeout_seconds=TOOL_LOOP_TOOL_TIMEOUT_SECONDS,
        ),
        builtins=build_validation_builtins(
            task_id=task_id,
            repo_path=repo_path,
            canonical_commits=commits,
            preview_bytes=config.tool_loop_preview_bytes,
        ),
        allowed_adapter_styles=_RUNTIME_ADAPTER_STYLES,
        caller="tasks.validation.tool_loop",
    )
    return normalize_tool_loop_result(await tool_chat_service.chat_result(request))
