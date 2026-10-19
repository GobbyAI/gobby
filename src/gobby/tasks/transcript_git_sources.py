"""Recover historical Python source from bounded, successful Git receipts.

Never execute a transcript command. Read immutable objects in the task repository,
and hydrate prior source only when a clean HEAD receipt and an exact reversal of
confirmed edits establish the committed stub and its successful checkout restore.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from gobby.tasks.transcript_evidence_models import TranscriptEdit, TranscriptValidationRun

if TYPE_CHECKING:
    from gobby.tasks.transcript_evidence_snapshots import PendingTool

CLEAN_HEAD_COMMAND = (
    "pwd && git rev-parse --abbrev-ref HEAD && git rev-parse HEAD && git status --short"
)


def _checkout(command: str) -> tuple[str, str] | None:
    """Accept one literal checkout followed only by its read-only diff checks."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            segments.append([])
        else:
            segments[-1].append(token)
    first = segments[0]
    if len(first) != 5 or first[:2] != ["git", "checkout"] or first[3] != "--":
        return None
    ref, path = first[2], first[4]
    if (
        re.fullmatch(r"[0-9a-f]{7,40}", ref) is None
        or re.fullmatch(r"[A-Za-z0-9_./-]+\.py", path) is None
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
    ):
        return None
    permitted = (
        ["git", "diff", "--cached", "--stat"],
        ["git", "diff", ref, "--", path, "|", "wc", "-l"],
        ["git", "diff", "--cached", ref, "--stat", "--", path],
    )
    if any(segment not in permitted for segment in segments[1:]):
        return None
    return ref, path


def is_git_source_command(command: str) -> bool:
    """These bounded AND chains preserve failure of the state-changing command."""
    return command.strip() == CLEAN_HEAD_COMMAND or _checkout(command) is not None


def _blob(repo: str, ref: str, path: str) -> str | None:
    try:
        # Enumerate object IDs directly: revision lookup can prefer a hex-named
        # branch or tag over the historical object prefix in the receipt.
        resolved = subprocess.run(
            ["git", "-C", repo, "rev-parse", f"--disambiguate={ref}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", resolved) is None or not resolved.startswith(ref):
            return None
        object_name = f"{resolved}:{path}"
        size = subprocess.run(
            ["git", "-C", repo, "cat-file", "-s", object_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not 0 <= int(size.stdout) <= 1_000_000:
            return None
        result = subprocess.run(
            ["git", "-C", repo, "cat-file", "blob", object_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def confirm_git_restore(
    edits: list[TranscriptEdit],
    runs: list[TranscriptValidationRun],
    pending: PendingTool,
    repo: str,
    completed_at: datetime,
) -> None:
    """Fill source only after both committed trees and the reversal are proven."""
    checkout = _checkout(str(pending.arguments.get("command", "")))
    if checkout is None:
        return
    ref, path = checkout
    receipt = next(
        (
            run
            for run in reversed(runs)
            if run.command.strip() == CLEAN_HEAD_COMMAND
            and run.outcome == "success"
            and run.completed_at < pending.timestamp
        ),
        None,
    )
    if receipt is None or receipt.output is None:
        return
    lines = receipt.output.splitlines()
    if len(lines) != 3 or re.fullmatch(r"[0-9a-f]{40}", lines[2]) is None:
        return
    root = lines[0]
    if receipt.workdir != root or (pending.workdir or pending.arguments.get("cwd")) != root:
        return
    if any(receipt.order < edit.order < pending.order for edit in edits):
        return
    history = [edit for edit in edits if edit.path == path and edit.order < receipt.order]
    if not history or any(edit.checkout_root != root for edit in history):
        return
    latest = history[-1]
    if not latest.task_attributed or not latest.source_confirmed:
        return
    before, after = _blob(repo, lines[2], path), _blob(repo, ref, path)
    if before is None or after is None or before == after:
        return
    # Replay confirmed stub receipts on the checkout tree. Commit hooks may have
    # formatted their inserted raise across lines; compare complete ASTs so that
    # formatting is allowed while indentation and all executable behavior stay bound.
    stubs: list[TranscriptEdit] = []
    for edit in reversed(history):
        old, new = edit.source_replaced, edit.source_fragment
        if (
            not edit.source_confirmed
            or edit.source_confirmed_at is None
            or edit.source_confirmed_at > receipt.started_at
            or not old
            or not new
            or "raise NotImplementedError" not in new
        ):
            break
        stubs.append(edit)
    expected = after
    for edit in reversed(stubs):
        old, new = edit.source_replaced, edit.source_fragment
        if old is None or new is None or expected.count(old) != 1:
            return
        expected = expected.replace(old, new, 1)
    try:
        same_tree = ast.dump(ast.parse(expected)) == ast.dump(ast.parse(before))
    except (SyntaxError, ValueError, RecursionError):
        return
    if not stubs or not same_tree:
        return
    snapshots: dict[int, str] = {latest.order: before}
    for edit in reversed(edits):
        if (
            edit.order >= receipt.order
            or edit.session_id != receipt.session_id
            or edit.checkout_root != root
            or not edit.task_attributed
            or not edit.path.endswith(".py")
            or not edit.source_confirmed
            or any(prior.order in snapshots and prior.path == edit.path for prior in edits)
        ):
            continue
        source = _blob(repo, lines[2], edit.path)
        if source is not None and (
            edit.source_after == source
            or (
                edit.source_after is None
                and edit.source_fragment
                and source.count(edit.source_fragment) == 1
            )
        ):
            snapshots[edit.order] = source
    edits[:] = [
        replace(edit, source_after=snapshots[edit.order], source_confirmed_at=receipt.completed_at)
        if edit.order in snapshots
        else edit
        for edit in edits
    ]
    restored_edit = replace(
        latest,
        order=pending.order,
        timestamp=pending.timestamp,
        tool_name=pending.name,
        source_after=after,
        source_fragment=None,
        source_replaced=None,
        source_confirmed=True,
        source_confirmed_at=completed_at,
        source_created=False,
        python_stub=None,
        python_added_source=None,
        source_unchanged=False,
    )
    edits[:] = [edit for edit in edits if edit.order != pending.order]
    edits.append(restored_edit)
