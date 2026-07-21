"""Paged, runtime-grounded tool loop for task validation."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
from gobby.tasks.validation_coverage import (
    MIN_BOUNDED_CONTENT_CALLS,
    analyze_evidence_coverage,
    compute_bounded_disclosure,
    plan_tool_calls,
)
from gobby.tasks.validation_prompts import _build_prompt
from gobby.tasks.validation_verdict import (
    contradiction_rejection_message,
    demote_contradictory_valid,
    filter_failure_evidence,
    is_contradictory_valid,
)

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
    diff_total_bytes: int
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
    evidence_error: dict[str, object] | None = None
    verdict_override: dict[str, object] | None = None
    inspection_summary: dict[str, object] | None = None


@dataclass
class ValidationVerdictSink:
    """Single-assignment sink for the model's schema-validated terminal verdict."""

    payload: dict[str, object] | None = None
    issued_evidence_refs: set[str] = field(default_factory=set)
    contradiction_rejections: int = 0
    last_contradiction: dict[str, object] | None = None
    bounded_mode: bool = False

    def record_evidence_ref(self, evidence_ref: str) -> None:
        """Record a successful runtime-issued evidence reference."""
        self.issued_evidence_refs.add(evidence_ref)

    def submit(self, arguments: Mapping[str, object]) -> BuiltinToolResult:
        if self.payload is not None:
            return BuiltinToolResult(
                error_code="verdict_already_submitted",
                error="validation verdict was already submitted",
            )
        raw_refs = arguments.get("evidence_refs")
        invalid_refs = (
            tuple(
                dict.fromkeys(
                    ref
                    for ref in raw_refs
                    if isinstance(ref, str) and ref not in self.issued_evidence_refs
                )
            )
            if isinstance(raw_refs, list)
            else ()
        )
        if invalid_refs:
            return BuiltinToolResult(
                error_code="invalid_evidence_reference",
                error="validation verdict cited non-runtime-issued evidence refs",
                details={
                    "evidence_refs": list(invalid_refs),
                    "issued_evidence_refs": sorted(self.issued_evidence_refs),
                },
            )
        if self.bounded_mode and arguments.get("evidence_complete") is not False:
            return BuiltinToolResult(
                error_code="evidence_complete_invalid",
                error="bounded validation verdicts must submit evidence_complete=false",
                details={"expected": False},
            )
        submitted = dict(arguments)
        evidence = filter_failure_evidence(submitted.get("current_failure_evidence"))
        if is_contradictory_valid(submitted.get("status"), evidence):
            self.last_contradiction = submitted
            self.contradiction_rejections += 1
            if self.contradiction_rejections == 1:
                return BuiltinToolResult(
                    error_code="verdict_contradiction",
                    error=contradiction_rejection_message(submitted),
                    details={"current_failure_evidence": evidence},
                )
        self.payload = submitted
        return BuiltinToolResult(
            payload={"accepted": True},
            selector={"kind": "validation_verdict"},
            complete=True,
        )


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
        diff_total_bytes=page["total_bytes"],
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


_VERIFICATION_EVIDENCE_FIELDS = (
    "evidence_type",
    "success",
    "timestamp",
    "command",
    "exit_code",
    "summary",
    "outcome_provenance",
    "task_id",
)


def _sanitize_verification_item(item: Mapping[str, object]) -> dict[str, object]:
    return {
        name: item[name]
        for name in _VERIFICATION_EVIDENCE_FIELDS
        if name in item and (item[name] is None or isinstance(item[name], str | int | bool))
    }


def build_validation_builtins(
    *,
    task_id: str,
    repo_path: str,
    canonical_commits: Sequence[str],
    preview_bytes: int,
    verdict_sink: ValidationVerdictSink | None = None,
    bounded_mode: bool = False,
    verification_items: Sequence[Mapping[str, object]] = (),
) -> tuple[BuiltinToolSpec, ...]:
    """Create bounded diff builtins closed over one canonical linked-commit set."""
    commits = tuple(canonical_commits)
    canonical_set = frozenset(commits)
    manager = _LinkedCommitManager(task_id, commits)
    recorded_verification = tuple(_sanitize_verification_item(item) for item in verification_items)
    aggregate_diff_complete = False

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
        result = BuiltinToolResult(
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
        if verdict_sink is not None:
            verdict_sink.record_evidence_ref(context.evidence_ref)
        return result

    async def read_task_diff_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        nonlocal aggregate_diff_complete
        snapshot_hash, view_hash = _page_tokens(arguments)
        commit = _linked_commit(_optional_str(arguments, "commit"), canonical_set)
        path_selector = _optional_str(arguments, "path_selector")
        if bounded_mode and path_selector is None:
            return BuiltinToolResult(
                error_code="bounded_view_forbidden",
                error="bounded validation permits only per-file task diff views",
            )
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
        result = _diff_result(page, selector)
        if result.ok and verdict_sink is not None:
            verdict_sink.record_evidence_ref(context.evidence_ref)
        if commit is None and path_selector is None and result.complete:
            aggregate_diff_complete = True
        return result

    async def list_verification_evidence_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        offset = _int_argument(arguments, "offset", 0)
        limit = _int_argument(arguments, "limit", 50)
        if offset > len(recorded_verification):
            return BuiltinToolResult(
                error_code="cursor_out_of_range",
                error="verification evidence offset exceeds the available item count",
            )
        end = min(len(recorded_verification), offset + limit)
        result = BuiltinToolResult(
            payload={
                "items": list(recorded_verification[offset:end]),
                "total": len(recorded_verification),
            },
            selector={"kind": "verification_evidence", "task_id": task_id},
            range={"cursor_offset": offset, "cursor_end": end, "total": len(recorded_verification)},
            complete=end >= len(recorded_verification),
        )
        if verdict_sink is not None:
            verdict_sink.record_evidence_ref(context.evidence_ref)
        return result

    async def read_file_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        if aggregate_diff_complete:
            return BuiltinToolResult(
                error_code="aggregate_diff_complete",
                error="complete aggregate task diff already covers the committed changes",
            )
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
        result = _diff_result(
            page,
            {
                "kind": "file_at_commit",
                "task_id": task_id,
                "commit": commit,
                "path_selector": path_selector,
            },
        )
        if result.ok and verdict_sink is not None:
            verdict_sink.record_evidence_ref(context.evidence_ref)
        return result

    async def submit_verdict_handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        del context
        if verdict_sink is None:
            return BuiltinToolResult(
                error_code="verdict_sink_unavailable",
                error="validation verdict sink is unavailable",
            )
        return verdict_sink.submit(arguments)

    manifest_properties = {
        "offset": _integer_property(default=0, minimum=0, maximum=MAX_CURSOR_OFFSET),
        "limit": _integer_property(
            default=MAX_MANIFEST_LIMIT, minimum=1, maximum=MAX_MANIFEST_LIMIT
        ),
        **_token_properties(),
    }
    verification_properties = {
        "offset": _integer_property(default=0, minimum=0, maximum=MAX_CURSOR_OFFSET),
        "limit": _integer_property(default=50, minimum=1, maximum=50),
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
    builtins = (
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
            name="list_verification_evidence",
            description="Return one cursor-bounded page of runtime-recorded command evidence.",
            input_schema={
                "type": "object",
                "properties": verification_properties,
                "additionalProperties": False,
            },
            handler=list_verification_evidence_handler,
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
    if verdict_sink is None:
        return builtins
    verdict_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["valid", "invalid", "pending"]},
            "feedback": {"type": "string"},
            "blocking_reasons": {"type": "array", "items": {"type": "string"}},
            "current_failure_evidence": {
                "type": "array",
                "items": {"type": "string"},
            },
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "evidence_complete": {"type": "boolean"},
        },
        "required": [
            "status",
            "feedback",
            "blocking_reasons",
            "current_failure_evidence",
            "evidence_refs",
            "evidence_complete",
        ],
        "additionalProperties": False,
    }
    return (
        *builtins,
        BuiltinToolSpec(
            name="submit_validation_verdict",
            description="Submit the one terminal typed validation verdict after evidence completion.",
            input_schema=verdict_schema,
            handler=submit_verdict_handler,
        ),
    )


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    inspection_summary: Mapping[str, object] | None = None,
) -> str:
    provenance_payload: dict[str, object] = {
        "mode": "tool_loop",
        "evidence_refs": list(evidence_refs),
        "evidence_complete": evidence_complete,
        "trace": list(trace_summary),
    }
    if inspection_summary is not None:
        provenance_payload["bounded_inspection"] = dict(inspection_summary)
    provenance = _compact_json(provenance_payload)
    prefix = feedback.strip() if feedback else "Tool-loop validation produced no feedback."
    return f"{prefix}\n\nTool-loop provenance:\n{provenance}"


def _pending_verdict(
    message: str,
    *,
    evidence_refs: Sequence[str] = (),
    evidence_complete: bool = False,
    trace_summary: Sequence[Mapping[str, object]] = (),
    evidence_error: dict[str, object] | None = None,
    inspection_summary: Mapping[str, object] | None = None,
) -> ToolLoopVerdict:
    return ToolLoopVerdict(
        status="pending",
        feedback=_feedback_with_provenance(
            message,
            evidence_refs=evidence_refs,
            evidence_complete=evidence_complete,
            trace_summary=trace_summary,
            inspection_summary=inspection_summary,
        ),
        blocking_reasons=(),
        evidence_refs=tuple(evidence_refs),
        evidence_complete=evidence_complete,
        trace_summary=tuple(dict(item) for item in trace_summary),
        evidence_error=evidence_error,
        inspection_summary=dict(inspection_summary) if inspection_summary is not None else None,
    )


def normalize_tool_loop_result(
    result: ToolChatResult,
    *,
    submitted_verdict: Mapping[str, object] | None = None,
    rejected_contradiction: Mapping[str, object] | None = None,
    require_submission: bool = False,
    bounded_mode: bool = False,
    inspection_summary: Mapping[str, object] | None = None,
) -> ToolLoopVerdict:
    """Accept only mode-explicit, runtime-issued evidence from successful invocations."""
    trace_summary = _trace_summary(result.trace)
    normalized_inspection = dict(inspection_summary) if inspection_summary is not None else None
    if not result.trace_available:
        error: dict[str, object] = {"code": "trace_unavailable", "artifacts": []}
        return _pending_verdict(
            "Tool-loop validation trace is unavailable; evidence cannot be verified.",
            trace_summary=trace_summary,
            evidence_error=error,
            inspection_summary=normalized_inspection,
        )

    coverage = analyze_evidence_coverage(result.trace, require_manifest=require_submission)
    coverage_error = coverage.error_payload()
    if coverage_error is not None:
        return _pending_verdict(
            f"Evidence acquisition failed: {_compact_json(coverage_error)}",
            evidence_refs=coverage.evidence_refs,
            trace_summary=trace_summary,
            evidence_error=coverage_error,
            inspection_summary=normalized_inspection,
        )

    def protocol_error(message: str) -> ToolLoopVerdict:
        error: dict[str, object] = {"code": "verdict_protocol_error", "message": message}
        return _pending_verdict(
            f"Validator protocol failed: {_compact_json(error)}",
            evidence_refs=coverage.evidence_refs,
            evidence_complete=not bounded_mode,
            trace_summary=trace_summary,
            evidence_error=error,
            inspection_summary=normalized_inspection,
        )

    if submitted_verdict is not None:
        payload: object = dict(submitted_verdict)
    elif rejected_contradiction is not None:
        payload = dict(rejected_contradiction)
    elif require_submission:
        return protocol_error("submit_validation_verdict was not called")
    else:
        try:
            payload = json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return protocol_error("validator must return exactly one typed verdict object")
    if not isinstance(payload, dict):
        return protocol_error("validator must return exactly one typed verdict object")
    if "current_failure_evidence" not in payload:
        return protocol_error("current_failure_evidence is required")
    if "evidence_refs" not in payload or "evidence_complete" not in payload:
        return protocol_error("evidence_refs and evidence_complete are required")
    required_fields = ("status", "feedback", "blocking_reasons")
    missing_fields = [name for name in required_fields if name not in payload]
    if missing_fields:
        return protocol_error(f"{', '.join(missing_fields)} are required")

    raw_status = payload["status"]
    feedback = payload["feedback"]
    raw_reasons = payload["blocking_reasons"]
    raw_failure_evidence = payload["current_failure_evidence"]
    raw_refs = payload["evidence_refs"]
    if not isinstance(raw_status, str):
        return protocol_error("status must be a string")
    if not isinstance(feedback, str):
        return protocol_error("feedback must be a string")
    if not isinstance(raw_reasons, list) or any(
        not isinstance(reason, str) for reason in raw_reasons
    ):
        return protocol_error("blocking_reasons must be an array of strings")
    if not isinstance(raw_failure_evidence, list) or any(
        not isinstance(item, str) for item in raw_failure_evidence
    ):
        return protocol_error("current_failure_evidence must be an array of strings")
    if not isinstance(raw_refs, list) or any(not isinstance(ref, str) for ref in raw_refs):
        return protocol_error("evidence_refs must be an array of strings")
    if not isinstance(payload["evidence_complete"], bool):
        return protocol_error("evidence_complete has an invalid type")
    if bounded_mode and payload["evidence_complete"] is not False:
        return protocol_error("bounded verdicts must submit evidence_complete=false")

    issued_refs = set(coverage.evidence_refs)
    verified_refs = tuple(dict.fromkeys(ref for ref in raw_refs if ref in issued_refs))
    fabricated_refs = tuple(dict.fromkeys(ref for ref in raw_refs if ref not in issued_refs))
    if fabricated_refs:
        invalid_ref_error: dict[str, object] = {
            "code": "invalid_evidence_reference",
            "evidence_refs": list(fabricated_refs),
        }
        return _pending_verdict(
            "Tool-loop verdict cited non-runtime-issued evidence refs: "
            f"{', '.join(fabricated_refs)}.",
            evidence_refs=verified_refs,
            evidence_complete=not bounded_mode,
            trace_summary=trace_summary,
            evidence_error=invalid_ref_error,
            inspection_summary=normalized_inspection,
        )
    if raw_status not in _VERDICT_STATUSES:
        return protocol_error("status must be valid, invalid, or pending")
    status = cast(Literal["valid", "invalid", "pending"], raw_status)
    if status != "pending" and not any(
        ref in coverage.content_evidence_refs for ref in verified_refs
    ):
        return _pending_verdict(
            "Tool-loop validation evidence is incomplete for a terminal verdict.",
            evidence_refs=verified_refs,
            evidence_complete=not bounded_mode,
            trace_summary=trace_summary,
            inspection_summary=normalized_inspection,
        )
    normalized_payload = dict(payload)
    normalized_payload["current_failure_evidence"] = filter_failure_evidence(raw_failure_evidence)
    normalized_payload = demote_contradictory_valid(normalized_payload)
    normalized_status = normalized_payload.get("status")
    assert isinstance(normalized_status, str)
    status = cast(Literal["valid", "invalid", "pending"], normalized_status)
    normalized_reasons = normalized_payload.get("blocking_reasons")
    assert isinstance(normalized_reasons, list)
    blocking_reasons = tuple(reason.strip() for reason in normalized_reasons if reason.strip())
    if status == "valid":
        blocking_reasons = ()
    elif status == "invalid" and not blocking_reasons:
        return _pending_verdict(
            "Tool-loop invalid verdict did not name unmet criteria or failing gates.",
            evidence_refs=verified_refs,
            evidence_complete=not bounded_mode,
            trace_summary=trace_summary,
            inspection_summary=normalized_inspection,
        )
    return ToolLoopVerdict(
        status=status,
        feedback=_feedback_with_provenance(
            feedback,
            evidence_refs=verified_refs,
            evidence_complete=not bounded_mode,
            trace_summary=trace_summary,
            inspection_summary=normalized_inspection,
        ),
        blocking_reasons=blocking_reasons,
        evidence_refs=verified_refs,
        evidence_complete=not bounded_mode,
        trace_summary=trace_summary,
        verdict_override=cast(
            dict[str, object] | None,
            normalized_payload.get("verdict_override"),
        ),
        inspection_summary=normalized_inspection,
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
    repo_path: str,
    canonical_commits: Sequence[str],
    first_commits_page: Mapping[str, object],
    manifest_items: Sequence[ManifestItem],
    manifest_count: int,
    diff_total_bytes: int,
    verification_items: Sequence[Mapping[str, object]] = (),
) -> ToolLoopVerdict:
    """Run one bounded validation investigation and verify its cited evidence."""
    commits = tuple(canonical_commits)
    call_plan = plan_tool_calls(
        diff_total_bytes=diff_total_bytes,
        manifest_count=manifest_count,
        preview_bytes=config.tool_loop_preview_bytes,
        manifest_page_limit=MAX_MANIFEST_LIMIT,
        configured_max_calls=config.tool_loop_max_calls,
        verification_pages=1 if verification_items else 0,
    )
    if call_plan.mode == "infeasible":
        error = {
            "code": "evidence_budget_exceeded",
            "required_tool_calls": call_plan.required_tool_calls,
            "configured_max_calls": config.tool_loop_max_calls,
            "content_call_budget": call_plan.content_call_budget,
            "min_content_calls": MIN_BOUNDED_CONTENT_CALLS,
            "manifest_pages": call_plan.manifest_pages,
            "artifacts": [
                {
                    "selector": {"kind": "task_diff", "task_id": task_id},
                    "total_bytes": diff_total_bytes,
                    "unconsumed_ranges": [[0, diff_total_bytes]],
                }
            ],
        }
        return _pending_verdict(
            f"Evidence acquisition failed: {_compact_json(error)}", evidence_error=error
        )
    bounded_mode = call_plan.mode == "bounded"
    verdict_sink = ValidationVerdictSink(bounded_mode=bounded_mode)
    request = ToolChatRequest(
        prompt=_build_prompt(
            title=title,
            description=description,
            validation_criteria=validation_criteria,
            category=category,
            commit_count=len(commits),
            first_commits_page=first_commits_page,
            manifest_count=manifest_count,
            diff_total_bytes=diff_total_bytes,
            mode="bounded" if bounded_mode else "exhaustive",
            content_call_budget=call_plan.content_call_budget,
            verification_item_count=len(verification_items),
        ),
        system_prompt=config.system_prompt,
        tool_policy=ToolPolicy(cli="gcode", tools=()),
        project_path=repo_path,
        profile=config.profile.value,
        candidates=tuple(config.candidates),
        candidate_timeout_seconds=config.cli_candidate_timeout_seconds,
        cli_candidate_timeout_seconds=config.cli_candidate_timeout_seconds,
        # Claude adapter consumes request.max_turns; OpenAI-compatible/local
        # adapters consume limits.max_turns. Both must carry the turn budget.
        max_turns=call_plan.max_turns,
        limits=ToolLoopLimits(
            max_turns=call_plan.max_turns,
            max_tool_calls=call_plan.max_tool_calls,
            tool_timeout_seconds=TOOL_LOOP_TOOL_TIMEOUT_SECONDS,
        ),
        builtins=build_validation_builtins(
            task_id=task_id,
            repo_path=repo_path,
            canonical_commits=commits,
            preview_bytes=config.tool_loop_preview_bytes,
            verdict_sink=verdict_sink,
            bounded_mode=bounded_mode,
            verification_items=verification_items,
        ),
        allowed_adapter_styles=_RUNTIME_ADAPTER_STYLES,
        caller="tasks.validation.tool_loop",
    )
    result = await tool_chat_service.chat_result(request)
    inspection_summary: dict[str, object] | None = None
    if bounded_mode and result.trace_available:
        inspection_summary = compute_bounded_disclosure(
            result.trace,
            manifest_items=manifest_items,
            content_call_budget=call_plan.content_call_budget,
        ).as_dict()
    return normalize_tool_loop_result(
        result,
        submitted_verdict=verdict_sink.payload,
        rejected_contradiction=verdict_sink.last_contradiction,
        require_submission=True,
        bounded_mode=bounded_mode,
        inspection_summary=inspection_summary,
    )
