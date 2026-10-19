"""Typed hub-node command frames and node bodies.

Wire JSON is compact, field order follows each model, unknown fields fail
validation, and strict mode refuses coercion, so a missing nullable field and
``"1"`` for an integer both fail. ``docs/contracts/node-commands.md`` is the
contract.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from gobby.utils.terminal_output import redact_terminal_output

CONTRACT_VERSION = 4

_LIST_LIMIT = 1000
_READ_BUDGET = 8_388_608
_GIT_TIMEOUT_LIMIT = 600_000
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_SRT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_OPERATION = re.compile(r"[a-z][a-z0-9*-]*")


def _sha256_hex(value: str) -> str:
    if _SHA256_HEX.fullmatch(value) is None:
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _srt_version(value: str) -> str:
    if _SRT_VERSION.fullmatch(value) is None:
        raise ValueError("must be a <digits>.<digits>.<digits> version")
    return value


def _operation_name(value: str) -> str:
    if value != "unclassified" and (
        _OPERATION.fullmatch(value) is None or redact_terminal_output(value) != value
    ):
        raise ValueError("must be unclassified or a plain operation name")
    return value


Digest = Annotated[str, AfterValidator(_sha256_hex)]
Count = Annotated[int, Field(ge=0)]
Version = Annotated[int, Field(ge=1)]


class NodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Sandbox(NodeModel):
    enabled: bool
    backend: Literal["srt", "provider-native"]
    mode: Literal["permissive", "restrictive"]
    allow_network: bool
    extra_read_paths: list[str]
    extra_write_paths: list[str]
    extra_deny_read_paths: list[str]
    extra_deny_write_paths: list[str]
    allowed_domains: list[str]
    denied_domains: list[str]
    allow_git_network: bool
    allow_package_registries: bool
    allow_unix_sockets: list[str]


class SpawnWorkspace(NodeModel):
    mode: Literal["none", "worktree", "clone", "existing"]
    path: str | None
    branch_name: str | None
    base_branch: str | None

    @model_validator(mode="after")
    def _path_names_existing(self) -> SpawnWorkspace:
        if (self.mode == "existing") != (self.path is not None):
            raise ValueError("path is required with mode existing and null otherwise")
        return self


class SpawnMonitor(NodeModel):
    auto_enter_agent_terminals: bool
    auto_enter_agent_interval_seconds: int
    auto_enter_approval_prompts: bool
    init_timeout_seconds: int


class SpawnPayload(NodeModel):
    run_id: str
    session_id: str
    parent_session_id: str | None
    project_id: str
    terminal_id: str
    agent_depth: int
    max_agent_depth: int
    workflow_name: str | None
    provider: str
    model: str | None
    prompt: str
    provider_args: list[str]
    reasoning_effort: str | None
    sandbox: Sandbox
    timeout_seconds: int
    title: str | None
    source_root: str
    workspace: SpawnWorkspace
    agent_name: str | None
    api_base: str | None
    api_token: str | None
    codex_oss_provider: str | None
    resume_session_id: str | None
    execution_mode: str | None
    code_index_preflight: str | None
    task_id: str | None
    extra_env: dict[str, str]
    codex_config_overrides: list[str]
    droid_mode: Literal["exec", "interactive"]
    prewarm_pre_commit_store: bool
    monitor: SpawnMonitor


class CancelPayload(NodeModel):
    run_id: str
    terminal_id: str
    signal: Literal["TERM", "KILL"]
    close_terminal: bool


class WritePayload(NodeModel):
    terminal_id: str
    kind: Literal["text", "key", "input", "paste"]
    data: str
    submit: bool


class MergePayload(NodeModel):
    project_id: str
    mode: Literal["worktree", "clone"]
    path: str
    branch_name: str
    target_branch: str
    source_root: str


class RemovePayload(NodeModel):
    project_id: str
    mode: Literal["worktree", "clone"]
    path: str
    branch_name: str
    source_root: str
    force: bool
    force_delete_branch: bool
    merged_into: str | None


class StatusPayload(NodeModel):
    project_id: str
    root_path: str


class SpawnCommand(NodeModel):
    type: Literal["command"]
    v: Version
    request_id: str
    command: Literal["agent.spawn"]
    payload: SpawnPayload


class CancelCommand(NodeModel):
    type: Literal["command"]
    v: Version
    request_id: str
    command: Literal["agent.cancel"]
    payload: CancelPayload


class WriteCommand(NodeModel):
    type: Literal["command"]
    v: Version
    request_id: str
    command: Literal["terminal.write"]
    payload: WritePayload


class MergeCommand(NodeModel):
    type: Literal["command"]
    v: Version
    request_id: str
    command: Literal["workspace.merge"]
    payload: MergePayload


class RemoveCommand(NodeModel):
    type: Literal["command"]
    v: Version
    request_id: str
    command: Literal["workspace.remove"]
    payload: RemovePayload


class StatusCommand(NodeModel):
    type: Literal["command"]
    v: Version
    request_id: str
    command: Literal["checkout.status"]
    payload: StatusPayload


class AcceptedResult(NodeModel):
    accepted: Literal[True]


class StoppedResult(NodeModel):
    stopped: bool


class EmptyResult(NodeModel):
    pass


class MergeResult(NodeModel):
    outcome: Literal["merged", "already_merged", "conflict", "target_dirty", "busy"]
    merge_sha: str | None
    target_head_sha: str | None
    conflicted_files: list[str]
    dirty_paths: list[str]

    @model_validator(mode="after")
    def _outcome_consistent(self) -> MergeResult:
        if self.outcome == "merged" and self.merge_sha is None:
            raise ValueError("merge_sha is required with outcome merged")
        if self.outcome != "merged" and self.merge_sha is not None:
            raise ValueError("merge_sha is set only with outcome merged")
        heads = ("merged", "already_merged")
        if self.outcome in heads and self.target_head_sha is None:
            raise ValueError("target_head_sha is required with outcome merged or already_merged")
        if self.outcome not in heads and self.target_head_sha is not None:
            raise ValueError("target_head_sha is set only with outcome merged or already_merged")
        if self.conflicted_files and self.outcome != "conflict":
            raise ValueError("conflicted_files is set only with outcome conflict")
        if self.dirty_paths and self.outcome != "target_dirty":
            raise ValueError("dirty_paths is set only with outcome target_dirty")
        return self


class RemoveResult(NodeModel):
    outcome: Literal["removed", "dirty", "unmerged", "busy"]


class StatusResult(NodeModel):
    current_branch: str | None
    branch_count: Count
    ahead: Count | None
    behind: Count | None


def _contract_version_only(value: int) -> int:
    if value != CONTRACT_VERSION:
        raise ValueError("v must equal CONTRACT_VERSION")
    return value


ReadLimit = Annotated[int, Field(ge=0, le=_READ_BUDGET)]
GitTimeout = Annotated[int, Field(ge=1, le=_GIT_TIMEOUT_LIMIT)]


class FilesQuery(NodeModel):
    kind: Literal["files"]
    paths: list[str] = Field(max_length=_LIST_LIMIT)
    read_limit: ReadLimit | None


class GitQuery(NodeModel):
    kind: Literal["git"]
    args: list[str]
    stdin: str | None
    timeout_ms: GitTimeout


CheckoutReadQuery = Annotated[FilesQuery | GitQuery, Field(discriminator="kind")]


class CheckoutReadPayload(NodeModel):
    cwd: str
    query: CheckoutReadQuery


class CheckoutReadCommand(NodeModel):
    type: Literal["command"]
    v: Annotated[int, AfterValidator(_contract_version_only)]
    request_id: str
    command: Literal["checkout.read"]
    payload: CheckoutReadPayload


class FileEntry(NodeModel):
    path: str
    resolved: str
    kind: Literal["file", "dir", "other", "missing", "outside"]
    checkout_root: str | None
    git_common_dir: str | None
    project_root: str | None
    project_id: str | None
    data: str | None
    truncated: bool


class GitResult(NodeModel):
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    truncated: bool


class CheckoutReadResult(NodeModel):
    root: str
    project_id: str
    home: str
    temp_dir: str
    files: list[FileEntry] | None
    git: GitResult | None


class ErrorBody(NodeModel):
    code: str
    message: str
    stage: Literal["none", "partial"] | None


class OkReply(NodeModel):
    type: Literal["reply"]
    v: Version
    request_id: str
    ok: Literal[True]
    result: (
        AcceptedResult
        | StoppedResult
        | EmptyResult
        | MergeResult
        | RemoveResult
        | StatusResult
        | CheckoutReadResult
    )


class ErrReply(NodeModel):
    type: Literal["reply"]
    v: Version
    request_id: str
    ok: Literal[False]
    error: ErrorBody


class CheckoutBody(NodeModel):
    v: Version
    project_id: str
    root_path: str


class CheckoutResult(NodeModel):
    root_path: str
    created: bool


class ReportWorkspace(NodeModel):
    mode: Literal["worktree", "clone"]
    path: str
    branch_name: str
    base_branch: str | None


class AttentionOption(NodeModel):
    option: int
    label: str


class AttentionPayload(NodeModel):
    kind: str
    excerpt: str
    options: list[AttentionOption]
    fingerprint: Digest


class PaneAttention(NodeModel):
    reason: str
    kind: str
    fingerprint: Digest
    payload: AttentionPayload
    dismissed: bool


class PaneComposer(NodeModel):
    state: Literal["empty", "draft", "unknown"]
    line: str | None


class PaneQuota(NodeModel):
    provider: str
    reset_time: str | None


class PaneIdle(NodeModel):
    status: Literal["unknown", "context_full", "idle", "active"]
    turn_in_flight: bool
    unsubmitted_input: bool
    queued_message_prompt: bool
    capacity_message: str | None
    quota: PaneQuota | None


class ReportPane(NodeModel):
    attention: PaneAttention | None
    injections: Count
    composer: PaneComposer
    idle: PaneIdle | None
    tail: Annotated[str, Field(max_length=2000)]


class ReportSandbox(NodeModel):
    runtime_version: Annotated[str, AfterValidator(_srt_version)]
    policy_schema_version: int
    policy_hash: Digest
    violation_count: Annotated[int, Field(ge=0, le=10_000)]
    violation_count_truncated: bool
    denial_operations: dict[Annotated[str, AfterValidator(_operation_name)], Count]
    recent_violations: Annotated[list[dict[str, JsonValue]], Field(max_length=100)] | None


class ReportAgent(NodeModel):
    run_id: str
    terminal_id: str
    pid: int
    locator: str
    locator_key: str
    workspace: ReportWorkspace | None
    pane: ReportPane | None
    sandbox: ReportSandbox | None

    @model_validator(mode="after")
    def _counts_only(self) -> ReportAgent:
        if self.sandbox is not None and self.sandbox.recent_violations is not None:
            raise ValueError("an agent entry's sandbox carries no recent_violations")
        return self


class ReportExit(NodeModel):
    run_id: str
    terminal_id: str
    launched: bool
    exit_code: int | None
    reason: Literal[
        "prepare_failed",
        "prepare_timeout",
        "launch_failed",
        "cancelled",
        "exited",
    ]
    workspace: ReportWorkspace | None
    sandbox: ReportSandbox | None

    @model_validator(mode="after")
    def _carries_events(self) -> ReportExit:
        if self.sandbox is not None and self.sandbox.recent_violations is None:
            raise ValueError("an exit's sandbox carries recent_violations")
        return self


class ReportBody(NodeModel):
    v: Version
    hub_epoch: int | None
    revision: Annotated[int, Field(ge=1)]
    host_epoch: int
    accepted: Annotated[list[str], Field(max_length=_LIST_LIMIT)]
    agents: Annotated[list[ReportAgent], Field(max_length=_LIST_LIMIT)]
    exits: Annotated[list[ReportExit], Field(max_length=_LIST_LIMIT)]


class StaleReport(NodeModel):
    code: Literal["stale_report"]
    hub_epoch: int


class PromptDeliveryFailureBody(NodeModel):
    v: Version
    run_id: str
    code: str
    detail: str
    pane: str | None


class PromptDeliveryFailureResult(NodeModel):
    failed: bool


class CodeIndexWarning(NodeModel):
    preflight: str
    cwd: str
    message: str


class CodeIndexWarningBody(NodeModel):
    v: Version
    run_id: str
    warning: CodeIndexWarning
    prompt: str


class CodeIndexWarningResult(NodeModel):
    prompt: str


class TerminateCheckpoint(NodeModel):
    ref_name: str
    commit_sha: str
    parent_sha: str
    files_changed: Count
    message: str


class TerminateBody(NodeModel):
    v: Version
    run_id: str
    cause: Literal["doom_loop", "provider_stall", "timeout", "init_timeout"]
    detail: str
    pane_tail: str
    checkpoint: TerminateCheckpoint | None


class TerminateResult(NodeModel):
    terminated: bool
    retry: bool


class DetectionManifestEntry(NodeModel):
    provider_id: str
    content: str


class DetectionManifestsResult(NodeModel):
    manifests: list[DetectionManifestEntry]


CommandFrame = (
    SpawnCommand
    | CancelCommand
    | WriteCommand
    | MergeCommand
    | RemoveCommand
    | StatusCommand
    | CheckoutReadCommand
)
ReplyFrame = OkReply | ErrReply

_PARSERS: dict[str, TypeAdapter[NodeModel]] = {
    "command": TypeAdapter(CommandFrame),
    "reply": TypeAdapter(ReplyFrame),
    "checkout": TypeAdapter(CheckoutBody),
    "checkout_result": TypeAdapter(CheckoutResult),
    "merge_result": TypeAdapter(MergeResult),
    "report": TypeAdapter(ReportBody),
    "stale_report": TypeAdapter(StaleReport),
    "prompt_delivery_failure": TypeAdapter(PromptDeliveryFailureBody),
    "prompt_delivery_failure_result": TypeAdapter(PromptDeliveryFailureResult),
    "code_index_warning": TypeAdapter(CodeIndexWarningBody),
    "code_index_warning_result": TypeAdapter(CodeIndexWarningResult),
    "terminate": TypeAdapter(TerminateBody),
    "terminate_result": TypeAdapter(TerminateResult),
    "detection_manifests": TypeAdapter(DetectionManifestsResult),
}


def parse_message(kind: str, raw: str) -> NodeModel:
    parser = _PARSERS.get(kind)
    if parser is None:
        raise ValidationError.from_exception_data(
            "unknown node-command kind",
            [],
        )
    return parser.validate_json(raw)


def round_trip_message(kind: str, raw: str) -> str:
    model = parse_message(kind, raw)
    payload = model.model_dump(mode="json")
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
