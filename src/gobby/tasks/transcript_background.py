"""Correlate Claude's automatic background command receipts without running jobs."""

from __future__ import annotations

import re
import shlex
from dataclasses import replace
from datetime import datetime

from defusedxml import ElementTree

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.tasks.transcript_evidence_models import OutputDelivery, TranscriptValidationRun
from gobby.tasks.transcript_evidence_snapshots import PendingTool
from gobby.tasks.transcript_outcomes import extract_outcome, extract_output

_BACKGROUND = re.compile(
    r"\ACommand (?:did not complete within its [0-9]+s timeout and was moved to the "
    r"background \(ID: |running in background with ID: )(?P<job>[A-Za-z0-9_-]+)"
    r"\)?\. Output is being written to: "
    r"(?P<path>/[^\r\n]+?\.output)\. "
)
_MAX_RECOVERED_CHARS = 64_000
# The Read tool renders each line behind its number and a tab.
_READ_LINE_NUMBER = re.compile(r"(?m)^ *\d+\t")
_GREP_LINE_NUMBER = re.compile(r"(?m)^\d+[:-]")
_TAIL_COUNT = re.compile(r"\+?\d+")
_TAIL_OPTION = re.compile(r"-(?:\d+|[nc]\+?\d+|q)")
# shlex's punctuation_chars set: a token made only of these is a shell operator.
_SHELL_OPERATOR_CHARS = frozenset("();<>|&")
# Unquoted, Bash brace-expands, globs, tilde-expands or history-expands these.
_UNQUOTED_EXPANSION_CHARS = frozenset("{}*?[~!")


def output_as_of(run: TranscriptValidationRun, before: datetime) -> TranscriptValidationRun:
    """The run as its transcript had delivered it before ``before``.

    A later recovery never displaces what was delivered earlier.
    """
    if not run.output_deliveries:
        return run
    output: str | None = None
    truncated = False
    source: str | None = None
    recovered_at: datetime | None = None
    for delivery in run.output_deliveries:
        if delivery.delivered_at < before:
            output, truncated = _applied(output, truncated, delivery)
            source = delivery.source
            recovered_at = delivery.delivered_at if source is not None else None
    return replace(
        run,
        output=output,
        output_truncated=truncated,
        output_recovered_from=source,
        output_recovered_at=recovered_at,
    )


def deliver_output(
    run: TranscriptValidationRun, delivery: OutputDelivery
) -> TranscriptValidationRun:
    """Apply one recovery to the run and record it for gate 12's replay."""
    launch = OutputDelivery(run.started_at, None, run.output, run.output_truncated, appends=False)
    output, truncated = _applied(run.output, run.output_truncated, delivery)
    return replace(
        run,
        output=output,
        output_truncated=truncated,
        output_recovered_from=delivery.source,
        output_recovered_at=delivery.delivered_at,
        output_deliveries=(*(run.output_deliveries or (launch,)), delivery),
    )


def _applied(
    output: str | None, truncated: bool, delivery: OutputDelivery
) -> tuple[str | None, bool]:
    if not delivery.appends:
        return delivery.text, delivery.truncated
    joined = (output or "") + "\n" + (delivery.text or "")
    return (
        joined[:_MAX_RECOVERED_CHARS],
        truncated or delivery.truncated or len(joined) > _MAX_RECOVERED_CHARS,
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
        shape = _read_shape(pending)
        if shape is None or extract_outcome(message.tool_result)[0] != "success":
            return
        path, numbered, context = shape
        matches = [i for i, run in enumerate(runs) if run.background_output_path == path]
        if len(matches) != 1:
            return
        index = matches[0]
        output, truncated = extract_output(message.tool_result, max_chars=_MAX_RECOVERED_CHARS)
        if output and pending.name.lower() == "read":
            output = _READ_LINE_NUMBER.sub("", output)
        if output and numbered:
            output = _GREP_LINE_NUMBER.sub("", output)
        if not output or message.timestamp < runs[index].started_at:
            return
        reader = _reader(pending, path)
        # Each grep -A group is its own delivery, so separate groups never become adjacent.
        for group in _context_groups(output) if context else (output,):
            runs[index] = deliver_output(
                runs[index],
                OutputDelivery(
                    message.timestamp, path, group, truncated, appends=False, reader=reader
                ),
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
        # Non-POSIX lexing keeps quotes on their words, so only the unquoted pipe
        # lexes as ``|`` and any other separator or redirection breaks the shape.
        argv = _shell_words(pending.arguments.get("command"), posix=False)
        if (
            argv is None
            or len(argv) != 10
            or argv[:4] != ["tail", "-n", "+1", "-f"]
            or argv[5:9] != ["|", "grep", "-E", "--line-buffered"]
            or set(argv[9]) <= _SHELL_OPERATOR_CHARS
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
        runs[index] = deliver_output(
            run,
            OutputDelivery(
                message.timestamp,
                run.background_output_path,
                event,
                False,
                appends=True,
                reader=f"Monitor {run.background_monitor_id}",
            ),
        )
    return True


def _shell_words(command: object, *, posix: bool) -> list[str] | None:
    """Words of one plain command line, with every unquoted operator split out.

    A command Bash could expand, substitute or end early is refused, never guessed.
    """
    if not isinstance(command, str):
        return None
    command = command.rstrip("\n")
    # shlex reads a newline as a blank; Bash ends the command there.
    if "\n" in command or _expands(command):
        return None
    lexer = shlex.shlex(command, posix=posix, punctuation_chars=True)
    lexer.whitespace_split = True
    # Bash splits words only on blanks and keeps a word-internal ``#`` literal.
    lexer.whitespace = " \t"
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _expands(command: str) -> bool:
    """Whether Bash could substitute or expand any part of ``command``.

    ``$`` and backticks are refused even inside quotes; the other expansion
    characters only outside quotes or a backslash escape.
    """
    if "$" in command or "`" in command:
        return True
    quote: str | None = None
    chars = iter(command)
    for char in chars:
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "\\":
            next(chars, None)
        elif quote == '"':
            if char == '"':
                quote = None
        elif char in "'\"":
            quote = char
        elif char in _UNQUOTED_EXPANSION_CHARS:
            return True
    return False


def _read_path(pending: PendingTool) -> str | None:
    shape = _read_shape(pending)
    return shape[0] if shape is not None else None


def _read_shape(pending: PendingTool) -> tuple[str, bool, bool] | None:
    """The one file a read names, and whether its grep printed line numbers and -A groups."""
    if pending.name.lower() == "read":
        path = pending.arguments.get("file_path")
        return (path, False, False) if isinstance(path, str) else None
    if pending.name.lower() != "bash":
        return None
    argv = _shell_words(pending.arguments.get("command"), posix=True)
    if argv is None:
        return None
    if len(argv) == 2 and argv[0] == "cat":
        return argv[1], False, False
    if argv[:1] == ["tail"]:
        path = _tail_path(argv[1:])
        return (path, False, False) if path is not None else None
    if argv[:1] == ["grep"]:
        return _grep_shape(argv[1:])
    return None


def _grep_shape(args: list[str]) -> tuple[str, bool, bool] | None:
    """``grep`` with only -n, -E, -F and -A N (or -AN), then one pattern and one file."""
    numbered = context = False
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option == "-n":
            numbered = True
        elif option == "-A" and index + 1 < len(args) and args[index + 1].isdigit():
            context = True
            index += 1
        elif re.fullmatch(r"-A\d+", option):
            context = True
        elif option not in ("-E", "-F"):
            return None
        index += 1
    if len(args) - index != 2 or args[index].startswith("-"):
        return None
    return args[index + 1], numbered, context


def _reader(pending: PendingTool, path: str) -> str:
    """The call that delivered output: ``Read <path>``, or the Bash command."""
    if pending.name.lower() == "read":
        return f"Read {path}"
    return str(pending.arguments.get("command"))


def _context_groups(output: str) -> list[str]:
    """Each contiguous group grep -A printed, split at its ``--`` separators."""
    groups: list[list[str]] = [[]]
    for line in output.splitlines():
        if line == "--":
            groups.append([])
        else:
            groups[-1].append(line)
    return ["\n".join(group) for group in groups if any(line.strip() for line in group)]


def recovered_deliveries(
    run: TranscriptValidationRun, before: datetime | None
) -> list[OutputDelivery]:
    """Each distinct recovered delivery before ``before``, in delivery order.

    Gate 12 proves each one alone: deliveries are never joined, since a newline between
    two reads would make unrelated lines adjacent to a failure section.
    """
    seen: set[str] = set()
    found: list[OutputDelivery] = []
    for delivery in run.output_deliveries:
        if delivery.source is None or not delivery.text:
            continue
        if before is not None and delivery.delivered_at >= before:
            continue
        if delivery.text not in seen:
            seen.add(delivery.text)
            found.append(delivery)
    return found


def unread_reason(run: TranscriptValidationRun, before: datetime | None, name: str) -> str:
    """Why a background run's pre-cutoff reads did not prove RED for ``name``."""
    readers = [
        delivery.reader or str(delivery.source) for delivery in recovered_deliveries(run, before)
    ]
    if not readers:
        return "its output was never read in a recognized form before the implementation edit"
    return (
        f"its reads before the implementation edit ({', '.join(readers)}) "
        f"name no runner row for {name}"
    )


def _tail_path(args: list[str]) -> str | None:
    """The one file a plain ``tail [-N | -n N | -nN | -c N | -cN | -q]... P`` prints.

    Following (``-f``/``-F``), several files, unknown options and shell operators
    are refused.
    """
    index = 0
    while index < len(args) - 1:
        option = args[index]
        if option in ("-n", "-c") and _TAIL_COUNT.fullmatch(args[index + 1]):
            index += 2
        elif _TAIL_OPTION.fullmatch(option):
            index += 1
        else:
            return None
    if index != len(args) - 1:
        return None
    path = args[index]
    if path.startswith("-") or set(path) <= _SHELL_OPERATOR_CHARS:
        return None
    return path
