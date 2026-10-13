"""Typed hub-node command frames and node bodies.

Wire JSON is compact, field order follows each model, and unknown fields fail
validation. ``docs/contracts/node-commands.md`` is the contract.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

_LIST_LIMIT = 1000


class NodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sandbox(NodeModel):
    enabled: bool
    network: Literal["none", "trusted"]
    extra_write_paths: list[str]


class SpawnWorkspace(NodeModel):
    mode: Literal["none", "worktree", "clone"]
    branch_name: str | None
    base_branch: str | None


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


class SpawnCommand(NodeModel):
    type: Literal["command"]
    v: Annotated[int, Field(ge=1)]
    request_id: str
    command: Literal["agent.spawn"]
    payload: SpawnPayload


class CancelCommand(NodeModel):
    type: Literal["command"]
    v: Annotated[int, Field(ge=1)]
    request_id: str
    command: Literal["agent.cancel"]
    payload: CancelPayload


class WriteCommand(NodeModel):
    type: Literal["command"]
    v: Annotated[int, Field(ge=1)]
    request_id: str
    command: Literal["terminal.write"]
    payload: WritePayload


class AcceptedResult(NodeModel):
    accepted: Literal[True]


class StoppedResult(NodeModel):
    stopped: bool


class EmptyResult(NodeModel):
    pass


class ErrorBody(NodeModel):
    code: str
    message: str
    stage: Literal["none", "partial"] | None


class OkReply(NodeModel):
    type: Literal["reply"]
    v: Annotated[int, Field(ge=1)]
    request_id: str
    ok: Literal[True]
    result: AcceptedResult | StoppedResult | EmptyResult


class ErrReply(NodeModel):
    type: Literal["reply"]
    v: Annotated[int, Field(ge=1)]
    request_id: str
    ok: Literal[False]
    error: ErrorBody


class CheckoutBody(NodeModel):
    v: Annotated[int, Field(ge=1)]
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


class ReportAgent(NodeModel):
    run_id: str
    terminal_id: str
    pid: int
    locator: str
    locator_key: str
    workspace: ReportWorkspace | None


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


class ReportBody(NodeModel):
    v: Annotated[int, Field(ge=1)]
    hub_epoch: int | None
    revision: Annotated[int, Field(ge=1)]
    host_epoch: int
    accepted: Annotated[list[str], Field(max_length=_LIST_LIMIT)]
    agents: Annotated[list[ReportAgent], Field(max_length=_LIST_LIMIT)]
    exits: Annotated[list[ReportExit], Field(max_length=_LIST_LIMIT)]


class StaleReport(NodeModel):
    code: Literal["stale_report"]
    hub_epoch: int


CommandFrame = SpawnCommand | CancelCommand | WriteCommand
ReplyFrame = OkReply | ErrReply

_PARSERS: dict[str, TypeAdapter[NodeModel]] = {
    "command": TypeAdapter(CommandFrame),
    "reply": TypeAdapter(ReplyFrame),
    "checkout": TypeAdapter(CheckoutBody),
    "checkout_result": TypeAdapter(CheckoutResult),
    "report": TypeAdapter(ReportBody),
    "stale_report": TypeAdapter(StaleReport),
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
