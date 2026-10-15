"""Correlate Claude's automatic background command receipts without running jobs."""

from __future__ import annotations

import re
import shlex
from dataclasses import replace

from defusedxml import ElementTree

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun
from gobby.tasks.transcript_evidence_snapshots import PendingTool
from gobby.tasks.transcript_outcomes import extract_outcome, extract_output

_BACKGROUND = re.compile(
    r"\ACommand did not complete within its [0-9]+s timeout and was moved to the "
    r"background \(ID: (?P<job>[A-Za-z0-9_-]+)\)\. Output is being written to: "
    r"(?P<path>/[^\r\n]+?\.output)\. "
)


def background_job_receipt(output: object) -> dict[str, str] | None:
    """Decode Claude's exact background handoff without treating it as an exit."""
    receipt = _BACKGROUND.match(output) if isinstance(output, str) else None
    return receipt.groupdict() if receipt is not None else None


def pending_background_run(
    run: TranscriptValidationRun, call_id: str | None, *, description: str | None = None
) -> TranscriptValidationRun:
    """The successful tool delivery announces a job, not a command exit."""
    receipt = background_job_receipt(run.output) if run.source == "claude" else None
    if receipt is None:
        return run
    return replace(
        run,
        outcome="unknown",
        exit_code=None,
        unknown_reason="provider background command has no terminal receipt",
        provider_call_id=call_id,
        background_job_id=receipt["job"],
        background_output_path=receipt["path"],
        background_description=description,
    )


def recover_background_receipt(
    runs: list[TranscriptValidationRun],
    message: ParsedMessage,
    pending: PendingTool | None,
    order: int,
) -> None:
    """Update only the original execution; separate reads never become test runs."""
    if _recover_monitor_receipt(runs, message, pending):
        return
    if message.content_type == "tool_result" and pending is not None:
        path = _read_path(pending)
        if path is None or extract_outcome(message.tool_result)[0] != "success":
            return
        matches = [i for i, run in enumerate(runs) if run.background_output_path == path]
        if len(matches) != 1:
            return
        index = matches[0]
        run = runs[index]
        output, truncated = extract_output(message.tool_result, max_chars=64_000)
        if output and message.timestamp >= run.started_at:
            runs[index] = replace(
                run,
                output=output,
                output_truncated=truncated,
                output_recovered_from=path,
                output_recovered_at=message.timestamp,
            )
        return
    notice = _notification(message)
    if notice is None:
        return
    matches = [
        i
        for i, run in enumerate(runs)
        if run.background_job_id == notice["task-id"]
        and run.provider_call_id == notice["tool-use-id"]
        and run.background_output_path == notice["output-file"]
    ]
    if len(matches) != 1:
        return
    index = matches[0]
    run = runs[index]
    names = "|".join(re.escape(name) for name in (run.command, run.background_description) if name)
    summary = re.fullmatch(
        rf'Background command "(?:{names})" '
        r"(?:(?:failed|completed) with exit code (-?\d+)|completed \(exit code (-?\d+)\))",
        notice["summary"],
    )
    if summary is None or message.timestamp < run.completed_at:
        return
    exit_code = int(summary[1] or summary[2])
    if notice["status"] != ("completed" if exit_code == 0 else "failed"):
        return
    if run.exit_code is not None:
        if run.exit_code != exit_code:
            runs[index] = replace(
                run,
                outcome="unknown",
                exit_code=None,
                unknown_reason="conflicting provider background terminal receipts",
            )
        return
    if run.unknown_reason == "conflicting provider background terminal receipts":
        return
    runs[index] = replace(
        run,
        outcome="success" if exit_code == 0 else "failure",
        exit_code=exit_code,
        unknown_reason=None,
        completed_at=message.timestamp,
        order=order,
    )


def _notification(message: ParsedMessage) -> dict[str, str] | None:
    return _notice_fields(message, ("task-id", "tool-use-id", "output-file", "status", "summary"))


def _notice_fields(message: ParsedMessage, fields: tuple[str, ...]) -> dict[str, str] | None:
    if message.role != "user" or message.content_type != "text":
        return None
    content = message.content
    if not isinstance(content, str) or not content.strip().startswith("<task-notification>"):
        return None
    if len(content) > 16_000 or "<!" in content:
        return None
    try:
        root = ElementTree.fromstring(content.strip())
    except ElementTree.ParseError:
        return None
    if root.tag != "task-notification":
        return None
    if any(len(root.findall(field)) != 1 for field in fields):
        return None
    return {field: root.findtext(field, "").strip() for field in fields}


def _recover_monitor_receipt(
    runs: list[TranscriptValidationRun],
    message: ParsedMessage,
    pending: PendingTool | None,
) -> bool:
    """Bind a provider Monitor to exactly one original job's output file."""
    if message.content_type == "tool_result" and pending and pending.name.lower() == "monitor":
        if extract_outcome(message.tool_result)[0] != "success":
            return True
        command = pending.arguments.get("command")
        try:
            argv = shlex.split(command) if isinstance(command, str) else []
        except ValueError:
            return True
        if (
            len(argv) != 10
            or argv[:4] != ["tail", "-n", "+1", "-f"]
            or argv[5:9] != ["|", "grep", "-E", "--line-buffered"]
        ):
            return True
        output, _ = extract_output(message.tool_result)
        receipt = re.match(r"Monitor started \(task ([A-Za-z0-9_-]+),", output or "")
        matches = [i for i, run in enumerate(runs) if run.background_output_path == argv[4]]
        if receipt and len(matches) == 1:
            index = matches[0]
            run = runs[index]
            if pending.timestamp >= run.completed_at and run.background_monitor_id is None:
                runs[index] = replace(run, background_monitor_id=receipt[1])
        return True
    notice = _notice_fields(message, ("task-id", "summary", "event"))
    if notice is None or not notice["summary"].startswith('Monitor event: "'):
        return False
    matches = [i for i, run in enumerate(runs) if run.background_monitor_id == notice["task-id"]]
    if len(matches) != 1:
        return True
    index = matches[0]
    run = runs[index]
    if message.timestamp < run.completed_at:
        return True
    event = notice["event"]
    # A Monitor's own exit cannot settle the command it watches. Only the
    # original job/call/output-file terminal receipt above proves its exit.
    if not event.startswith(("[Monitor expired", "[exited with code")):
        output = (run.output or "") + "\n" + event
        runs[index] = replace(
            run,
            output=output[:64_000],
            output_truncated=run.output_truncated or len(output) > 64_000,
            output_recovered_from=run.background_output_path,
            output_recovered_at=message.timestamp,
        )
    return True


def _read_path(pending: PendingTool) -> str | None:
    if pending.name.lower() == "read":
        path = pending.arguments.get("file_path")
        return path if isinstance(path, str) else None
    if pending.name.lower() != "bash":
        return None
    command = pending.arguments.get("command")
    if not isinstance(command, str):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        argv = list(lexer)
    except ValueError:
        return None
    if len(argv) == 2 and argv[0] == "cat":
        return argv[1]
    if len(argv) == 4 and argv[:2] == ["tail", "-n"] and argv[2].isdigit():
        return argv[3]
    return None
