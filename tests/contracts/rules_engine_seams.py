"""Engine-path seams of the rule-pass corpus that the recorder module does not hold.

- ``{python}`` in a ``run_command`` command list is replaced by the running interpreter.
- The fake RTK (``"proxy": {"rtk": "echo-prefix"}``) is a shebang script the real proxy
  handler runs: it prefixes a command with ``rtk ``, and denies one holding
  ``synthetic-denied`` with exit status 2.
- ``inline_run_command`` drives one ``run_command`` effect through
  ``RuleEngine._apply_effect`` with a live event. The rule pass hands effects a frozen event
  that the payload builder cannot write into, so a rule-pass run_command never runs; this
  caller is how the corpus reaches the effect's deadline arithmetic, execution, result
  handling and audit write. A case with ``input.command_boundary`` replaces the command
  execution and the clock, so it starts no process and reads no real clock.

This module never imports ``rules_recorder``; it takes the identity constants from
``rules_seed`` and the recorder's event builder as an argument.
"""

from __future__ import annotations

import inspect
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from gobby.hooks import effect_deadline
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import ContextPart, HookEvent
from gobby.integrations.rtk import RTK_VERSION, RtkProbe
from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine import run_command_effects
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.run_command import RunCommandResult, execute_run_command
from tests.contracts import rules_corpus, rules_seed

PYTHON_PLACEHOLDER = "{python}"
RTK_FAKE = rules_corpus.RTK_FAKE
SHEBANG_LIMIT_BYTES = 127
CONTROLLED_CLOCK_START = 1000.0
_FAKE_RTK_SCRIPT = """
import sys
args = sys.argv[1:]
if len(args) != 3 or args[:2] != ["rewrite", "--"]:
    sys.exit(1)
command = args[2]
if "synthetic-denied" in command:
    sys.stdout.write("synthetic rtk denial\\n"); sys.exit(2)
if command.startswith("rtk "):
    sys.exit(1)
sys.stdout.write("rtk " + command + "\\n")
"""

EventBuilder = Callable[..., HookEvent]


def _expand_placeholders(rule: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``rule`` with each ``{python}`` element of a run_command command expanded."""
    expanded = dict(rule)
    effects = rule.get("effects")
    if not isinstance(effects, list):
        return expanded
    out: list[Any] = []
    for effect in effects:
        if isinstance(effect, Mapping) and effect.get("type") == "run_command":
            command = effect.get("command")
            if isinstance(command, list):
                effect = {
                    **effect,
                    "command": [
                        sys.executable if part == PYTHON_PLACEHOLDER else part for part in command
                    ],
                }
        out.append(effect)
    expanded["effects"] = out
    return expanded


def _fake_rtk_probe(directory: Path) -> RtkProbe:
    """Write the fake RTK executable into ``directory`` and return a compatible probe."""
    shebang = f"#!{sys.executable}"
    if len(shebang.encode()) > SHEBANG_LIMIT_BYTES:
        raise RuntimeError(f"the interpreter path is too long for a shebang: {shebang!r}")
    path = directory / "rtk"
    path.write_text(shebang + "\n" + _FAKE_RTK_SCRIPT)
    os.chmod(path, 0o755)
    return RtkProbe(path=path, version=RTK_VERSION, compatible=True)


class _ControlledClock:
    """A stand-in for the ``time`` module whose monotonic clocks never move."""

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _command_boundary(
    spec: Mapping[str, Any], seen: list[float]
) -> Callable[..., Awaitable[RunCommandResult]]:
    """Return a fake ``execute_run_command`` that records the timeout it was handed.

    The command "takes" ``spec["duration"]`` seconds: it succeeds with ``spec["context"]``
    when that fits the timeout, and returns a typed timeout otherwise.
    """
    duration = float(spec["duration"])
    context = str(spec["context"])

    async def boundary(command: list[str], **kwargs: Any) -> RunCommandResult:
        timeout = float(kwargs["timeout_seconds"])
        seen.append(timeout)
        fits = duration <= timeout
        return RunCommandResult(
            status="success" if fits else "timeout",
            context=context if fits else None,
            duration_ms=duration * 1000,
            exit_code=0 if fits else None,
            stdout_bytes=len(context) if fits else 0,
            stderr_bytes=0,
            timeout_seconds=timeout,
            overflow_stream=None,
            background=bool(kwargs.get("background", False)),
            phase="execution",
            skill=None,
            script=None,
        )

    if inspect.iscoroutinefunction(execute_run_command) != inspect.iscoroutinefunction(boundary):
        raise RuntimeError("the command boundary is not the same kind of callable it replaces")
    return boundary


def _inline_deadline(spec: Mapping[str, Any], clock: _ControlledClock | None) -> Any:
    if clock is not None:
        if "deadline" in spec:
            raise ValueError("a command_boundary case gives deadline_remaining, not deadline")
        remaining = spec.get("deadline_remaining")
        if remaining is None:
            return None
        return BlockingEffectDeadline(expires_at=clock.now + float(remaining))
    if "deadline_remaining" in spec:
        raise ValueError("deadline_remaining needs a command_boundary")
    deadline = spec.get("deadline")
    if deadline is None:
        return None
    if deadline != "expired":
        raise ValueError(f"unknown inline deadline {deadline!r}")
    return BlockingEffectDeadline(expires_at=time.monotonic() - 1.0)


async def _run_inline_command(
    db: PostgresHubDatabase, spec: Mapping[str, Any], *, event_from: EventBuilder
) -> dict[str, Any]:
    """Apply one seeded rule's run_command effect with a live event and read its audit rows."""
    session = db.fetchone("SELECT id FROM sessions WHERE id = %s", (rules_seed.SESSION_ID,))
    if session is None:
        raise ValueError("inline_run_command needs the seeded session row")
    row = RuleDefinitionManager(db).get_by_name(str(spec["rule_name"]))
    if row is None:
        raise ValueError(f"no seeded rule named {spec['rule_name']!r}")
    effect = RuleDefinitionBody.model_validate(row.definition_json).resolved_effects[0]
    event = event_from(spec["event"], session_id=rules_seed.SESSION_ID)
    boundary = spec.get("command_boundary")
    if boundary is not None and not isinstance(boundary, Mapping):
        raise ValueError(f"command_boundary must be a mapping, got {boundary!r}")
    clock = _ControlledClock(CONTROLLED_CLOCK_START) if boundary is not None else None
    deadline = _inline_deadline(spec, clock)
    seen: list[float] = []
    parts: list[ContextPart] = []
    engine = RuleEngine(db, metrics_event_store=MetricsEventStore(db))
    ctx: dict[str, Any] = {"event": event, "_blocking_deadline": deadline}
    if boundary is None:
        await engine._apply_effect(effect, row, {}, ctx, {}, parts, [], {})
    else:
        with (
            mock.patch.object(run_command_effects, "time", clock),
            mock.patch.object(effect_deadline, "time", clock),
            mock.patch.object(
                run_command_effects, "execute_run_command", _command_boundary(boundary, seen)
            ),
        ):
            await engine._apply_effect(effect, row, {}, ctx, {}, parts, [], {})
    rows = db.fetchall(
        "SELECT event_type, result FROM workflow_audit_log WHERE session_id = %s ORDER BY id",
        (rules_seed.SESSION_ID,),
    )
    returned: dict[str, Any] = {
        "context_parts": [[label, text] for label, text in parts],
        "audit_rows": [[item["event_type"], item["result"]] for item in rows],
    }
    if clock is not None:
        returned["command_timeouts"] = seen
    return returned
