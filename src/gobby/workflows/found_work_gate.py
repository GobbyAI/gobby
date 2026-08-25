"""Rule-4 stop facts for defect deferrals and terminal validation failures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.config.shell_lexing import parse_shell_command
from gobby.config.validation_detection import (
    ValidationCommandMatcher,
    ValidationDetectionConfig,
    classify_validation_command,
    resolve_validation_detection_config,
)
from gobby.hooks.events import HookEvent
from gobby.tasks.transcript_evidence import (
    TranscriptEvidenceUnavailable,
    TranscriptValidationRun,
    derive_transcript_evidence,
)

logger = logging.getLogger(__name__)

RULE_4_LADDER_MESSAGE = (
    "Rule 4 ladder: fix it now (create_task claim=true), hand it to its active owner via "
    "send_message, or — edge case — file it labeled needs-decision/clean-window with the "
    "reason. Don't ask, and don't go silent."
)

_DEFECT_RE = re.compile(
    r"\b(?:bug|broken|defect|error|fail(?:ed|ing|ure)?|incorrect|regression|warning|"
    r"type error|lint error|does(?:n't| not) work|not working)\b",
    re.IGNORECASE,
)
_PERMISSION_CLOSER_RE = re.compile(
    r"(?:should|shall|can|could|may)\s+i\s+"
    r"(?:fix|repair|address|resolve|investigate|delete|drop|erase|overwrite|reset|remove|purge)|"
    r"(?:would|do)\s+you\s+(?:like|want)\s+me\s+to\s+"
    r"(?:fix|repair|address|resolve|investigate|delete|drop|erase|overwrite|reset|remove|purge)|"
    r"want\s+me\s+to\s+"
    r"(?:fix|repair|address|resolve|investigate|delete|drop|erase|overwrite|reset|remove|purge)",
    re.IGNORECASE,
)
_DESTRUCTIVE_CONFIRMATION_RE = re.compile(
    r"(?:should|shall|can|could|may)\s+i\s+"
    r"(?:delete|drop|erase|overwrite|reset|remove|purge|destroy|force[- ]push)|"
    r"(?:would|do)\s+you\s+(?:like|want)\s+me\s+to\s+"
    r"(?:delete|drop|erase|overwrite|reset|remove|purge|destroy|force[- ]push)|"
    r"want\s+me\s+to\s+"
    r"(?:delete|drop|erase|overwrite|reset|remove|purge|destroy|force[- ]push)",
    re.IGNORECASE,
)
_USER_DEFERRAL_RE = re.compile(
    r"\b(?:do not|don't|dont|must not|without)\s+(?:fix|change|edit|modify|implement)|"
    r"\b(?:only|just)\s+(?:review|assess|audit|diagnose|explain|report|answer)|"
    r"\b(?:review|assessment|audit|diagnosis|report|q&a)\s+(?:only|deliverable)\b",
    re.IGNORECASE,
)
_SCOPE_OPTION_NAMES = {
    "-k",
    "-m",
    "-p",
    "--filter",
    "--package",
    "--run",
    "-run",
}
_REPORTED_FAILURE_PATH_RE = re.compile(
    r"^\s*(?:FAILED|ERROR)\s+([^\s:]+)|"
    r"^\s*([^\s:]+\.[A-Za-z0-9]+):\d+(?::\d+)?:\s+(?:error|warning)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PROJECT_CATEGORY = {
    "unit_tests": "test",
    "integration": "test",
    "doc_tests": "test",
    "type_check": "typecheck",
    "lint": "lint",
    "format": "format",
    "build": "build",
    "security": "security",
    "code_review": "review",
}
_SHIRK_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "block": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["block", "reason"],
    "additionalProperties": False,
}
_SHIRK_SYSTEM_PROMPT = """You classify one coding-agent stop attempt for Rule 4.
Return block=true only when the assistant describes an actionable defect it could address and
ends by asking the user for permission instead of following the supplied Found Work ladder.
Return block=false for destructive-action confirmations, user-reserved decisions, explicit user
deferral instructions, assessment-only deliverables, or enhancement ideas with nothing broken.
Use only the supplied text and evidence. Return JSON matching the schema."""


@dataclass(frozen=True)
class FoundWorkStopFacts:
    """Transient facts consumed by declarative stop-gate rules."""

    shirk: bool = False
    terminal_validation_failures: tuple[str, ...] = ()


def capture_turn_prompt(event: HookEvent, variables: dict[str, Any]) -> None:
    """Persist the current user instruction for later stop-time exemptions."""
    variables["_current_user_prompt"] = ""
    variables["_rule4_owner_handoff_turn"] = False
    variables["_rule4_fix_commit_turn"] = False
    if not isinstance(event.data, dict):
        return
    for key in ("prompt_text", "prompt", "user_prompt"):
        value = event.data.get(key)
        if isinstance(value, str) and value.strip():
            variables["_current_user_prompt"] = value.strip()
            return


def capture_rule4_handoff(event: HookEvent, variables: dict[str, Any]) -> None:
    """Track tool activity and successful owner handoff within the current turn."""
    revision = variables.get("_rule4_activity_revision", 0)
    variables["_rule4_activity_revision"] = int(revision) + 1 if isinstance(revision, int) else 1
    if not isinstance(event.data, dict):
        return
    if event.data.get("mcp_server") != "gobby-agents":
        return
    if event.data.get("mcp_tool") != "send_message":
        return
    from gobby.hooks.tool_outcomes import normalize_tool_outcome

    is_failure = event.metadata.get("is_failure")
    explicit_success = not is_failure if isinstance(is_failure, bool) else None
    outcome = normalize_tool_outcome(
        event.data,
        explicit_success=explicit_success,
        provenance="hook_event.metadata.is_failure" if explicit_success is not None else None,
    )
    if outcome.succeeded is True:
        variables["_rule4_owner_handoff_turn"] = True


def is_permission_deferral_candidate(message: str) -> bool:
    """Cheap, conservative fast path before an LLM confirmation."""
    if not isinstance(message, str) or not message.strip():
        return False
    tail = message.strip()[-800:]
    if "?" not in tail:
        return False
    closer = _PERMISSION_CLOSER_RE.search(tail)
    if closer is None or not re.search(r"\?\s*[*_`]*\s*$", tail[closer.start() :]):
        return False
    return bool(_DEFECT_RE.search(message))


def unresolved_validation_failures(
    runs: Sequence[TranscriptValidationRun],
    *,
    owner_handoff: bool,
    foreign_paths: AbstractSet[str] = frozenset(),
) -> tuple[TranscriptValidationRun, ...]:
    """Return failures without a later covering green run."""
    ordered = sorted(runs, key=lambda run: run.order)
    unresolved: list[TranscriptValidationRun] = []
    for index, failed in enumerate(ordered):
        if failed.outcome != "failure":
            continue
        later_greens = [
            run
            for run in ordered[index + 1 :]
            if run.outcome == "success" and set(run.categories) & set(failed.categories)
        ]
        if any(_run_covers(green, failed) for green in later_greens):
            continue
        if owner_handoff and _verified_foreign_clearance(
            failed,
            later_greens,
            foreign_paths,
        ):
            continue
        unresolved.append(failed)
    return tuple(unresolved)


def resolve_stop_validation_config(
    *,
    daemon_config: Any | None,
    project_path: str | None,
) -> ValidationDetectionConfig:
    """Merge standard detection with exact project verification commands."""
    resolved = resolve_validation_detection_config(
        daemon_config=daemon_config,
        project_path=project_path,
    )
    for name, command in _project_verification_commands(project_path).items():
        if classify_validation_command(command, resolved) is not None:
            continue
        prefix = _project_command_prefix(command)
        if not prefix:
            continue
        matcher_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        resolved.custom_matchers.append(
            ValidationCommandMatcher(
                id=f"project-verification-{matcher_id}",
                label=f"Project verification: {name}",
                categories=[_PROJECT_CATEGORY.get(name, name)],
                prefixes=[prefix],
            )
        )
    return resolved


class FoundWorkStopAnalyzer:
    """Derive Rule-4 facts without persisting policy state."""

    def __init__(
        self,
        *,
        llm_service_resolver: Callable[[], Any | None],
        config_resolver: Callable[[], Any | None],
        session_manager: Any | None,
        session_task_manager: Any | None,
        db: Any | None = None,
    ) -> None:
        self._llm_service_resolver = llm_service_resolver
        self._config_resolver = config_resolver
        self._session_manager = session_manager
        self._session_task_manager = session_task_manager
        self._db = db

    async def analyze(
        self,
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        project_path: str | None,
    ) -> FoundWorkStopFacts:
        message = await _assistant_message(event, self._session_manager, session_id)
        user_prompt = str(variables.get("_current_user_prompt") or "")
        cache_key = _analysis_cache_key(message, user_prompt, variables)
        if variables.get("_rule4_analysis_cache_key") == cache_key:
            cached_failures = variables.get("_rule4_terminal_validation_failures")
            return FoundWorkStopFacts(
                shirk=variables.get("_rule4_found_work_shirk") is True,
                terminal_validation_failures=(
                    tuple(str(item) for item in cached_failures)
                    if isinstance(cached_failures, list | tuple)
                    else ()
                ),
            )

        labeled_deferral = await asyncio.to_thread(
            self._has_labeled_deferral_task,
            session_id,
        )
        task_disposition = bool(variables.get("task_claimed")) or labeled_deferral
        primary_compliance = bool(
            task_disposition
            or variables.get("_rule4_fix_commit_turn")
            or variables.get("_rule4_owner_handoff_turn")
        )
        shirk = False
        if not primary_compliance and is_permission_deferral_candidate(message):
            if not _deterministic_exemption(message, user_prompt):
                shirk = await self._confirm_shirk(message, user_prompt)

        failures: tuple[str, ...] = ()
        if not task_disposition:
            failures = await self._terminal_failures(
                session_id=session_id,
                variables=variables,
                project_path=project_path,
                project_id=event.project_id,
            )
        variables["_rule4_analysis_cache_key"] = cache_key
        variables["_rule4_found_work_shirk"] = shirk
        variables["_rule4_terminal_validation_failures"] = list(failures)
        return FoundWorkStopFacts(shirk=shirk, terminal_validation_failures=failures)

    def _has_labeled_deferral_task(self, session_id: str) -> bool:
        if self._session_task_manager is None:
            return False
        try:
            links = self._session_task_manager.get_session_tasks(session_id)
        except Exception:
            logger.debug("Could not inspect session tasks for Rule-4 deferrals", exc_info=True)
            return False
        for link in links:
            task = link.get("task") if isinstance(link, Mapping) else None
            labels = getattr(task, "labels", None)
            created_in = getattr(task, "created_in_session_id", None)
            if (
                created_in == session_id
                and isinstance(labels, Sequence)
                and not isinstance(labels, str | bytes)
            ):
                if {"needs-decision", "clean-window"} & set(labels):
                    return True
        return False

    async def _confirm_shirk(self, message: str, user_prompt: str) -> bool:
        service = self._llm_service_resolver()
        daemon_config = self._config_resolver()
        if service is None or daemon_config is None:
            return True
        try:
            validation = daemon_config.get_gobby_tasks_config().validation
            if not validation.enabled:
                return True
            prompt = (
                f"USER INSTRUCTION:\n{user_prompt or '(unavailable)'}\n\n"
                f"FINAL ASSISTANT MESSAGE:\n{message}"
            )
            payload = await service.call_json_feature(
                validation,
                prompt,
                system_prompt=_SHIRK_SYSTEM_PROMPT,
                json_schema=_SHIRK_CONFIRM_SCHEMA,
                caller="workflows.found_work_gate",
                total_timeout_seconds=min(
                    float(validation.close_review_total_timeout_seconds),
                    8.0,
                ),
            )
        except Exception:
            logger.debug(
                "Rule-4 shirk confirmation unavailable; using fast-path verdict", exc_info=True
            )
            return True
        if isinstance(payload, Mapping) and isinstance(payload.get("block"), bool):
            return payload["block"] is True
        return True

    async def _terminal_failures(
        self,
        *,
        session_id: str,
        variables: Mapping[str, Any],
        project_path: str | None,
        project_id: str | None,
    ) -> tuple[str, ...]:
        if self._session_manager is None or not project_path:
            return ()
        try:
            session = await asyncio.to_thread(self._session_manager.get, session_id)
            if session is None:
                return ()
            config = await asyncio.to_thread(
                resolve_stop_validation_config,
                daemon_config=self._config_resolver(),
                project_path=project_path,
            )
            evidence = await derive_transcript_evidence(
                session,
                session.created_at,
                config,
                set(),
                project_path,
            )
        except TranscriptEvidenceUnavailable:
            logger.debug("Rule-4 validation evidence unavailable for session %s", session_id)
            return ()
        except Exception:
            logger.debug("Could not derive Rule-4 validation evidence", exc_info=True)
            return ()

        owner_handoff = variables.get("_rule4_owner_handoff_turn") is True
        foreign_paths: set[str] = set()
        if owner_handoff and self._db is not None:
            foreign_paths = await self._foreign_owned_dirty_paths(
                session_id=session_id,
                project_id=project_id or getattr(session, "project_id", None),
                project_path=project_path,
            )
        unresolved = unresolved_validation_failures(
            evidence.validation_runs,
            owner_handoff=owner_handoff,
            foreign_paths=foreign_paths,
        )
        return tuple(dict.fromkeys(run.command for run in unresolved))

    async def _foreign_owned_dirty_paths(
        self,
        *,
        session_id: str,
        project_id: str | None,
        project_path: str,
    ) -> set[str]:
        db = self._db
        if not project_id or db is None:
            return set()
        try:
            from gobby.workflows.commit_guard import foreign_owned_dirty_paths
            from gobby.workflows.git_utils import get_dirty_files_categorized

            dirty = await asyncio.to_thread(get_dirty_files_categorized, project_path)
            ownership = await asyncio.to_thread(
                foreign_owned_dirty_paths,
                db,
                session_id=session_id,
                project_id=project_id,
                checkout_root=project_path,
                paths=dirty.all,
            )
        except Exception:
            logger.debug("Could not verify confined foreign validation failure", exc_info=True)
            return set()
        return set(ownership)


async def _assistant_message(
    event: HookEvent,
    session_manager: Any | None,
    session_id: str,
) -> str:
    data = event.data if isinstance(event.data, Mapping) else {}
    for key in (
        "last_assistant_message",
        "last_assistant_content",
        "assistant_response",
        "assistant_response_text",
        "response",
        "output",
        "message",
        "content",
        "log",
    ):
        text = _coerce_text(data.get(key))
        if text:
            return text
    if session_manager is not None:
        try:
            session = await asyncio.to_thread(session_manager.get, session_id)
        except Exception:
            session = None
        if session is not None:
            for value in (
                getattr(session, "last_assistant_content", None),
                getattr(session, "last_turn_markdown", None),
            ):
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return "\n".join(text for item in value if (text := _coerce_text(item)))
    if isinstance(value, Mapping):
        return "\n".join(
            text
            for key in ("text", "content", "message", "response", "output")
            if (text := _coerce_text(value.get(key)))
        )
    return ""


def _deterministic_exemption(message: str, user_prompt: str) -> bool:
    if _DESTRUCTIVE_CONFIRMATION_RE.search(message[-800:]):
        return True
    return bool(user_prompt and _USER_DEFERRAL_RE.search(user_prompt))


def _analysis_cache_key(
    message: str,
    user_prompt: str,
    variables: Mapping[str, Any],
) -> str:
    payload = {
        "message": message,
        "prompt": user_prompt,
        "activity_revision": variables.get("_rule4_activity_revision", 0),
        "task_claimed": variables.get("task_claimed") is True,
        "fix_commit": variables.get("_rule4_fix_commit_turn") is True,
        "owner_handoff": variables.get("_rule4_owner_handoff_turn") is True,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _run_covers(success: TranscriptValidationRun, failure: TranscriptValidationRun) -> bool:
    success_targets = _command_targets(success.command)
    failure_targets = _command_targets(failure.command)
    if not success_targets:
        return True
    if not failure_targets:
        return False
    return all(
        any(_target_covers(green, red) for green in success_targets) for red in failure_targets
    )


def _verified_foreign_clearance(
    failure: TranscriptValidationRun,
    later_greens: Sequence[TranscriptValidationRun],
    foreign_paths: AbstractSet[str],
) -> bool:
    reported_paths = _reported_failure_paths(failure)
    if not reported_paths or not foreign_paths:
        return False
    if any(path not in foreign_paths for path in reported_paths):
        return False
    return any(_green_scope_avoids_foreign_paths(run, foreign_paths) for run in later_greens)


def _reported_failure_paths(run: TranscriptValidationRun) -> set[str]:
    output = run.output or ""
    paths: set[str] = set()
    for match in _REPORTED_FAILURE_PATH_RE.finditer(output):
        raw = match.group(1) or match.group(2)
        if raw:
            paths.add(raw.removeprefix("./").split("::", 1)[0])
    if paths:
        return paths
    return {target for target in _command_targets(run.command) if Path(target).suffix}


def _green_scope_avoids_foreign_paths(
    run: TranscriptValidationRun,
    foreign_paths: AbstractSet[str],
) -> bool:
    targets = tuple(
        target for target in _command_targets(run.command) if not target.startswith("-")
    )
    if not targets:
        return False
    return all(
        not _target_covers(target, foreign_path) and not _target_covers(foreign_path, target)
        for target in targets
        for foreign_path in foreign_paths
    )


def _command_targets(command: str) -> tuple[str, ...]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ()
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if token in _SCOPE_OPTION_NAMES and index + 1 < len(tokens):
            targets.append(f"{token}:{tokens[index + 1]}")
            continue
        if token.startswith("-") or "=" in token or token in {"&&", "||", ";"}:
            continue
        normalized = token.removeprefix("./").rstrip("/")
        if "/" in normalized or Path(normalized).suffix:
            targets.append(normalized)
    return tuple(dict.fromkeys(targets))


def _target_covers(success: str, failure: str) -> bool:
    if success == failure:
        return True
    if success.startswith("-") or failure.startswith("-"):
        return False
    return failure.startswith(success.rstrip("/") + "/")


def _project_verification_commands(project_path: str | None) -> dict[str, str]:
    if not project_path:
        return {}
    path = Path(project_path) / ".gobby" / "project.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    verification = payload.get("verification")
    if not isinstance(verification, Mapping):
        return {}
    commands = {
        str(name): command
        for name, command in verification.items()
        if name != "custom" and isinstance(command, str) and command.strip()
    }
    custom = verification.get("custom")
    if isinstance(custom, Mapping):
        commands.update(
            {
                str(name): command
                for name, command in custom.items()
                if isinstance(command, str) and command.strip()
            }
        )
    return commands


def _project_command_prefix(command: str) -> str:
    parsed = parse_shell_command(command)
    if not parsed.segments:
        return ""
    tokens = list(parsed.segments[-1])
    while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
        tokens.pop(0)
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if not tokens:
        return ""
    for index, token in enumerate(tokens):
        if "{" in token:
            tokens = tokens[:index]
            break
    return shlex.join(tokens)
