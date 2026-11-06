"""Terminal notices for Claude background commands, read from the owner's transcript.

Claude writes a ``<task-notification>`` user row to the owning session's transcript when a
background command's process ends. That row is the only evidence this module accepts: the
command's output file, and anything printed into it, can say whatever a running process
writes, so it never proves the process ended.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from defusedxml import ElementTree

NOTICE_TAG = "<task-notification>"
NOTICE_FIELDS = ("task-id", "tool-use-id", "output-file", "status", "summary")
_NOTICE_ORIGIN = "task-notification"
_MAX_NOTICE_CHARS = 16_000
_RECEIPT_START = re.compile(
    r"\s*(?:Command running in background with ID: "
    r"|Command did not complete within its [0-9]+s timeout and was moved to the background \(ID: )"
)
_RECEIPT_JOB = re.compile(r"(?:running in background with ID: |\(ID: )([A-Za-z0-9_-]+)")
_RECEIPT_OUTPUT = re.compile(r"Output is being written to: ([^\s\"\\]+?)\.(?=[\s\"\\]|$)")
_SUMMARY = re.compile(
    r'Background command ".*" (?:(?:failed|completed) with exit code (-?\d+)'
    r"|completed \(exit code (-?\d+)\))",
    re.DOTALL,
)
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class BackgroundReceipt:
    """A backgrounded command's job id and output file, each when the response names it.

    A receipt missing either is still a background call: it stays in flight, because no notice
    can be matched to it.
    """

    job_id: str | None
    output_path: str | None


@dataclass(frozen=True)
class BackgroundNotice:
    """A notice row that proves one background call ended, referenced by its row uuid."""

    row_uuid: str
    tool_use_id: str
    job_id: str
    output_path: str
    status: str
    exit_code: int


def _stdout_of(response: Any) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        stdout = response.get("stdout")
        if isinstance(stdout, str):
            return stdout
    return None


def _structured_job_id(response: Any) -> str | None:
    if isinstance(response, Mapping):
        job_id = response.get("backgroundTaskId")
        if isinstance(job_id, str) and job_id:
            return job_id
    return None


def _all_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, default=str)


def background_receipt(data: Mapping[str, Any]) -> BackgroundReceipt | None:
    """Read the background receipt from a hook payload's tool response, when it has one.

    A structured response that names a background task is a background call, whatever its stdout
    printed before the receipt: its receipt is searched in the whole response. When the output
    file is not named, the call still counts, with no output path, so it stays in flight.
    Other output is a background call only when the receipt is its first sentence. A foreground
    command that prints the sentence after other text, such as a grep of a file that holds it,
    started no job and has no receipt.
    """
    response = data.get("tool_output", data.get("tool_response", data.get("tool_result")))
    structured_job = _structured_job_id(response)
    if structured_job is not None:
        output = _RECEIPT_OUTPUT.search(_all_text(response))
        return BackgroundReceipt(
            job_id=structured_job, output_path=output.group(1) if output else None
        )
    text = _stdout_of(response)
    if text is None or _RECEIPT_START.match(text) is None:
        return None
    output = _RECEIPT_OUTPUT.search(text)
    job = _RECEIPT_JOB.search(text)
    return BackgroundReceipt(
        job_id=job.group(1) if job else None,
        output_path=output.group(1) if output else None,
    )


def notice_fields(content: str, fields: tuple[str, ...] = NOTICE_FIELDS) -> dict[str, str] | None:
    """Parse the named fields from a notice's text; None unless it is one well-formed notice."""
    text = content.strip()
    if not text.startswith(NOTICE_TAG) or len(text) > _MAX_NOTICE_CHARS or "<!" in text:
        return None
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    if root.tag != "task-notification":
        return None
    if any(len(root.findall(field)) != 1 for field in fields):
        return None
    return {field: root.findtext(field, "").strip() for field in fields}


def resolve_background_notice(
    transcript_path: str | None, *, tool_use_id: str, receipt: BackgroundReceipt
) -> BackgroundNotice | None:
    """Return the one notice row that ends this background call, or None.

    The scan reads newest-first and stops at the call's own tool-use row, since a notice for
    this call cannot precede it. Zero matches, several matches, an unreadable transcript, or
    a row that is not Claude's notice all return None, so the call stays in flight.
    """
    if (
        not transcript_path
        or not tool_use_id
        or receipt.job_id is None
        or receipt.output_path is None
    ):
        return None
    needle = tool_use_id.encode()
    matches: list[BackgroundNotice] = []
    try:
        for row in _rows_newest_first(Path(transcript_path), needle):
            if _is_call_start(row, tool_use_id):
                break
            notice = _notice_of(row, tool_use_id, receipt)
            if notice is not None:
                matches.append(notice)
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _rows_newest_first(path: Path, needle: bytes) -> Iterator[dict[str, Any]]:
    for raw in _reverse_lines(path):
        if needle not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _reverse_lines(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        carry = b""
        while position > 0:
            size = min(_CHUNK_BYTES, position)
            position -= size
            handle.seek(position)
            # The first segment may continue into the earlier chunk; the rest are whole lines.
            head, *lines = (handle.read(size) + carry).split(b"\n")
            for line in reversed(lines):
                if line:
                    yield line
            carry = head
        if carry:
            yield carry


def _is_call_start(row: Mapping[str, Any], tool_use_id: str) -> bool:
    message = row.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if row.get("type") != "assistant" or not isinstance(content, list):
        return False
    return any(
        isinstance(block, Mapping)
        and block.get("type") == "tool_use"
        and block.get("id") == tool_use_id
        for block in content
    )


def _notice_of(
    row: Mapping[str, Any], tool_use_id: str, receipt: BackgroundReceipt
) -> BackgroundNotice | None:
    message = row.get("message")
    origin = row.get("origin")
    if row.get("type") != "user" or not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, str):
        return None
    if not isinstance(origin, Mapping) or origin.get("kind") != _NOTICE_ORIGIN:
        return None
    fields = notice_fields(content)
    if fields is None or receipt.job_id is None or receipt.output_path is None:
        return None
    if (
        fields["tool-use-id"] != tool_use_id
        or fields["task-id"] != receipt.job_id
        or fields["output-file"] != receipt.output_path
    ):
        return None
    summary = _SUMMARY.fullmatch(fields["summary"])
    if summary is None:
        return None
    exit_code = int(summary[1] or summary[2])
    status = "completed" if exit_code == 0 else "failed"
    if fields["status"] != status:
        return None
    row_uuid = row.get("uuid")
    if not isinstance(row_uuid, str) or not row_uuid:
        return None
    return BackgroundNotice(
        row_uuid=row_uuid,
        tool_use_id=tool_use_id,
        job_id=receipt.job_id,
        output_path=receipt.output_path,
        status=status,
        exit_code=exit_code,
    )
