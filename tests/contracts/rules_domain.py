"""The production domain a handler-entry case seeds: project, repo, sessions, tasks, state.

A case's ``domain`` mapping is seeded through the production managers before its first
pass, in a fixed order: identity rows (``rules_seed``), the project and its checkout, the
git fixture, registered sessions with their transcripts, agent definitions and step
instances (``rules_seed``), tasks, session variables, chat mode and context usage, agent
runs. Every definition is synthetic and written in the case file.

Placeholders are substituted into the domain, every event and every between-operation
before use, and reverse-scrubbed out of every recorded value: ``{session:<key>}``,
``{external:<key>}``, ``{task:<key>}``, ``{task_ref:<key>}``, ``{project}``, ``{repo}``,
``{run:<key>}``, ``{commit:<n>}`` (1-based; the full SHA and every hex prefix of seven or
more characters), ``{transcript:<key>}`` and ``{machine}``. ``{ts:+N}`` (input only) is N
seconds after the session row's ``created_at``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.dirty_edit_grants import record_dirty_path_approval
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.contracts import rules_seed

SYNTHETIC_PREFIX = "synthetic-"
SELF = "self"
# Recorded values of these keys are wall-clock times; they are masked to "{time}".
CLOCK_KEYS = frozenset(
    {
        "approved_at",
        "step_entered_at",
        "updated_at",
        "created_at",
        "filled_at",
        "armed_at",
        "last_activity",
        "_found_work_gate_armed_at",
    }
)
MASKED_TIME = "{time}"
CLOCK_DIGEST_KEYS = frozenset({"tail_sha256", "_found_work_analysis_cache_key"})
MASKED_DIGEST = "{digest}"
DOMAIN_KEYS = frozenset(
    {
        "project",
        "repo",
        "sessions",
        "agents",
        "tasks",
        "variables",
        "step_instances",
        "agent_runs",
    }
)
SESSION_KEYS = frozenset(
    {"source", "transcript", "chat_mode", "context_tokens", "context_window", "agent", "expired"}
)
TASK_KEYS = frozenset(
    {"title", "claimed_by", "created_by", "labels", "task_type", "linked_to", "status"}
)
_PLACEHOLDER = re.compile(
    r"\{(session|external|task|task_ref|run|commit|transcript):([A-Za-z0-9_-]+)\}"
    r"|\{(project|repo|machine)\}|\{ts:\+(\d+)\}"
)
_HEX_RUN = re.compile(r"\b[0-9a-f]{7,40}\b")
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Synthetic Author",
    "GIT_AUTHOR_EMAIL": "synthetic-author@corpus.invalid",
    "GIT_COMMITTER_NAME": "Synthetic Committer",
    "GIT_COMMITTER_EMAIL": "synthetic-committer@corpus.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
GIT_EPOCH = 1_700_000_000


@dataclass
class DomainState:
    project_id: str
    repo: Path | None
    tmp: Path
    sessions: dict[str, str] = field(default_factory=dict)
    externals: dict[str, str] = field(default_factory=dict)
    tasks: dict[str, tuple[str, str]] = field(default_factory=dict)
    commits: list[str] = field(default_factory=list)
    runs: dict[str, str] = field(default_factory=dict)
    transcripts: dict[str, Path] = field(default_factory=dict)
    created_at: dict[str, datetime] = field(default_factory=dict)

    def session(self, key: str = SELF) -> str:
        return self.sessions[key]


def validate_domain(case_id: str, domain: Any) -> None:
    """Raise ``ValueError`` naming the case and the offending key or name."""
    if not isinstance(domain, Mapping):
        raise ValueError(f"{case_id}: domain must be a mapping")
    unknown = sorted(set(domain) - DOMAIN_KEYS)
    if unknown:
        raise ValueError(f"{case_id}: domain has unknown key(s) {unknown}")
    agents = domain.get("agents", {})
    if not isinstance(agents, Mapping):
        raise ValueError(f"{case_id}: domain.agents must be a mapping")
    for key, spec in agents.items():
        names = [key, spec.get("name") if isinstance(spec, Mapping) else None]
        for name in names:
            if not isinstance(name, str) or not name.startswith(SYNTHETIC_PREFIX):
                raise ValueError(f"{case_id}: domain agent name {name!r} is not synthetic")
        workflow = spec.get("step_workflow") if isinstance(spec, Mapping) else None
        for step in rules_seed._step_names(workflow):
            if not step.startswith(SYNTHETIC_PREFIX):
                raise ValueError(f"{case_id}: domain step name {step!r} is not synthetic")
    for key, session in domain.get("sessions", {}).items():
        extra = sorted(set(session) - SESSION_KEYS)
        if extra:
            raise ValueError(f"{case_id}: domain session {key!r} has unknown key(s) {extra}")
    for key, task in domain.get("tasks", {}).items():
        extra = sorted(set(task) - TASK_KEYS)
        if extra:
            raise ValueError(f"{case_id}: domain task {key!r} has unknown key(s) {extra}")


# --- placeholders ---------------------------------------------------------------------


def _resolve(match: re.Match[str], state: DomainState, origin: datetime | None) -> str:
    kind, key, bare, seconds = match.groups()
    if bare == "project":
        return state.project_id
    if bare == "repo":
        if state.repo is None:
            raise ValueError("{repo} needs domain.repo")
        return str(state.repo)
    if bare == "machine":
        return rules_seed.SYNTHETIC_MACHINE_ID
    if seconds is not None:
        if origin is None:
            raise ValueError("{ts:+N} is only valid in a transcript")
        return (origin + timedelta(seconds=int(seconds))).isoformat().replace("+00:00", "Z")
    lookup: dict[str, Callable[[str], str]] = {
        "session": lambda k: state.sessions[k],
        "external": lambda k: state.externals[k],
        "task": lambda k: state.tasks[k][0],
        "task_ref": lambda k: state.tasks[k][1],
        "run": lambda k: state.runs[k],
        "commit": lambda k: state.commits[int(k) - 1],
        "transcript": lambda k: str(state.transcripts[k]),
    }
    return lookup[kind](key)


def substitute(value: Any, state: DomainState, *, origin: datetime | None = None) -> Any:
    """Return ``value`` with every placeholder replaced, in keys and values."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: _resolve(m, state, origin), value)
    if isinstance(value, Mapping):
        return {
            substitute(key, state, origin=origin): substitute(item, state, origin=origin)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [substitute(item, state, origin=origin) for item in value]
    return value


def scrubber(state: DomainState) -> Callable[[str], str]:
    """Return the text replacement that writes the domain's values back as placeholders."""
    pairs: list[tuple[str, str]] = []
    for key, path in state.transcripts.items():
        pairs.append((str(path), f"{{transcript:{key}}}"))
    if state.repo is not None:
        pairs.append((str(state.repo), "{repo}"))
    for key, session_id in state.sessions.items():
        pairs.append((session_id, f"{{session:{key}}}"))
    for key, (task_id, _ref) in state.tasks.items():
        pairs.append((task_id, f"{{task:{key}}}"))
    for key, run_id in state.runs.items():
        pairs.append((run_id, f"{{run:{key}}}"))
    pairs.append((str(state.tmp), "{tmp}"))
    pairs.append((state.project_id, "{project}"))
    pairs.append((rules_seed.SYNTHETIC_MACHINE_ID, "{machine}"))
    pairs.sort(key=lambda pair: -len(pair[0]))
    commits = list(state.commits)

    def commit_ref(match: re.Match[str]) -> str:
        text = match.group(0)
        for index, sha in enumerate(commits, start=1):
            if sha.startswith(text):
                return f"{{commit:{index}}}"
        return text

    def replace(text: str) -> str:
        for needle, placeholder in pairs:
            text = text.replace(needle, placeholder)
        return _HEX_RUN.sub(commit_ref, text) if commits else text

    return replace


def mask_clock(value: Any) -> Any:
    """Replace the value of every ``CLOCK_KEYS`` key, at any depth, with ``{time}``.

    A ``CLOCK_DIGEST_KEYS`` value is a digest over inputs that carry wall-clock times (the
    transcript's ``{ts:+N}`` records; the found-work cache key's task close time and daemon
    boot instant), so it is masked the same way. The record then holds no opaque hash.
    """
    if isinstance(value, Mapping):
        return {
            key: (
                MASKED_TIME
                if key in CLOCK_KEYS and isinstance(value[key], str | datetime)
                else MASKED_DIGEST
                if key in CLOCK_DIGEST_KEYS and isinstance(value[key], str)
                else mask_clock(value[key])
            )
            for key in value
        }
    if isinstance(value, list):
        return [mask_clock(item) for item in value]
    return value


# --- seeding --------------------------------------------------------------------------


def _git(root: Path, *args: str, when: int | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        stamp = f"{when} +0000"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    completed = subprocess.run(
        ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write(root: Path, files: Mapping[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def git_fixture(root: Path, spec: Mapping[str, Any], stack: contextlib.ExitStack) -> list[str]:
    """Create the case's repository and return the full SHA of each commit, in order.

    Author, committer, dates and config are pinned; HOME is never changed. A commit entry
    may first ``checkout`` a branch (``create`` makes it) and ``write`` files, and may
    ``merge`` a branch with a merge commit instead of committing ``add``.
    """
    global_config = root.parent / "gitconfig"
    global_config.write_text("")
    stack.enter_context(
        mock.patch.dict(os.environ, {**GIT_IDENTITY, "GIT_CONFIG_GLOBAL": str(global_config)})
    )
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "-c", "init.defaultBranch=main", "init", "-q")
    # The project marker the seeder writes is local state, never part of the fixture's tree.
    (root / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text(".gobby/\n")
    _write(root, spec.get("files", {}))
    shas: list[str] = []
    for index, commit in enumerate(spec.get("commits", [])):
        if "checkout" in commit:
            create = ["-b"] if commit.get("create") else []
            _git(root, "checkout", "-q", *create, str(commit["checkout"]))
        _write(root, commit.get("write", {}))
        when = GIT_EPOCH + index
        if "merge" in commit:
            merge = ["merge", "-q", "--no-ff", "-m", str(commit["message"]), str(commit["merge"])]
            _git(root, *merge, when=when)
        else:
            _git(root, "add", "--", *commit["add"])
            _git(root, "commit", "-q", "-m", str(commit["message"]), when=when)
        shas.append(_git(root, "rev-parse", "HEAD"))
    _write(root, spec.get("staged", {}))
    if spec.get("staged"):
        _git(root, "add", "--", *spec["staged"])
    _write(root, spec.get("dirty", {}))
    return shas


def _seed_project(db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState) -> None:
    project = spec.get("project", {})
    name = str(project.get("name", rules_seed.SYNTHETIC_PROJECT_NAME))
    repo = str(state.repo) if state.repo is not None else None
    if state.repo is not None:
        marker = state.repo / ".gobby" / "project.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"id": state.project_id, "name": name}) + "\n")
    LocalProjectManager(db).create(name, repo_path=repo, project_id=state.project_id)


def _seed_sessions(db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState) -> None:
    sessions = SessionManager(db)
    for key, session in spec.get("sessions", {}).items():
        external = f"synthetic-external-{key}"
        transcript = session.get("transcript")
        path = state.tmp / "transcripts" / f"{key}.jsonl" if transcript is not None else None
        session_id = sessions.register_session(
            external_id=external,
            machine_id=rules_seed.SYNTHETIC_MACHINE_ID,
            source=str(session.get("source", "claude")),
            project_id=state.project_id,
            transcript_path=str(path) if path is not None else None,
            agent_depth=1 if session.get("agent") else 0,
        )
        if not session_id:
            raise RuntimeError(f"register_session returned no id for {key!r}")
        state.sessions[key] = session_id
        state.externals[key] = external
        row = sessions.get(session_id)
        if row is None:
            raise RuntimeError(f"no session row for {key!r}")
        created = row.created_at
        state.created_at[key] = (
            created if isinstance(created, datetime) else datetime.fromisoformat(str(created))
        )
        if path is not None:
            state.transcripts[key] = path
        if session.get("expired"):
            sessions.update_status(session_id, "expired")
    for key, session in spec.get("sessions", {}).items():
        transcript = session.get("transcript")
        if transcript is not None:
            _write_transcript(state, key, transcript)
        if session.get("chat_mode") is not None:
            sessions.update_chat_mode(state.sessions[key], str(session["chat_mode"]))
        if session.get("context_tokens") is not None:
            used = int(session["context_tokens"])
            window = int(session.get("context_window", 256_000))
            snapshot = ContextUsageSnapshot(
                source="claude",
                model=None,
                context_window=window,
                context_used_tokens=used,
                context_usage_ratio=used / window,
                confidence="reported",
                timestamp=state.created_at[key],
            )
            sessions.update_context_usage(state.sessions[key], snapshot)


def _write_transcript(state: DomainState, key: str, records: list[Any]) -> None:
    path = state.transcripts[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    origin = state.created_at[key]
    lines = [json.dumps(substitute(record, state, origin=origin)) for record in records]
    path.write_text("".join(line + "\n" for line in lines))


def append_transcript(state: DomainState, key: str, records: list[Any]) -> None:
    path = state.transcripts[key]
    origin = state.created_at[key]
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(substitute(record, state, origin=origin)) + "\n")


def _seed_agents(db: PostgresHubDatabase, spec: Mapping[str, Any]) -> None:
    agents = spec.get("agents", {})
    if agents:
        rules_seed.seed_agent_definitions(db, list(agents.values()))


def _seed_tasks(db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState) -> None:
    tasks = LocalTaskManager(db)
    links = SessionTaskManager(db)
    for key, task in spec.get("tasks", {}).items():
        claimed = task.get("claimed_by")
        created = task.get("created_by")
        row = tasks.create_task(
            state.project_id,
            str(task.get("title", f"synthetic task {key}")),
            created_in_session_id=state.sessions[created] if created else None,
            claimed_by_session_id=state.sessions[claimed] if claimed else None,
            labels=list(task.get("labels", [])) or None,
            task_type=str(task.get("task_type", "task")),
        )
        state.tasks[key] = (row.id, f"#{row.seq_num}")
        if task.get("status"):
            tasks.update_task(row.id, status=str(task["status"]))
        for session in task.get("linked_to", []):
            links.link_task(state.sessions[session], row.id)


def _seed_variables(db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState) -> None:
    manager = SessionVariableManager(db)
    for key, values in spec.get("variables", {}).items():
        if values:
            manager.merge_variables(state.sessions[key], substitute(values, state))


def _seed_step_instances(
    db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState
) -> None:
    definitions = list(spec.get("agents", {}).values())
    for key, instance in spec.get("step_instances", {}).items():
        rules_seed.seed_step_instance(
            db, instance, session_id=state.sessions[key], definitions=definitions
        )


def _seed_agent_runs(db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState) -> None:
    runs = LocalAgentRunManager(db)
    for key, run in spec.get("agent_runs", {}).items():
        created = runs.create(
            parent_session_id=state.sessions[str(run.get("parent", SELF))],
            provider=str(run.get("provider", "claude")),
            prompt=str(run.get("prompt", "synthetic prompt")),
            child_session_id=(
                state.sessions[str(run["child"])] if run.get("child") is not None else None
            ),
        )
        state.runs[key] = created.id
        if run.get("status") == "running":
            runs.start(created.id)


def seed_domain(
    db: PostgresHubDatabase, spec: Mapping[str, Any], tmp: Path, stack: contextlib.ExitStack
) -> DomainState:
    """Seed ``spec`` and return the identities the placeholders resolve to."""
    root = Path(os.path.realpath(tmp))
    repo_spec = spec.get("repo")
    state = DomainState(
        project_id=rules_seed.SYNTHETIC_PROJECT_ID,
        repo=root / "repo" if repo_spec is not None else None,
        tmp=root,
    )
    rules_seed.seed_identity(db)
    if state.repo is not None and repo_spec is not None:
        state.commits = git_fixture(state.repo, repo_spec, stack)
    _seed_project(db, spec, state)
    _seed_sessions(db, spec, state)
    _seed_agents(db, spec)
    _seed_tasks(db, spec, state)
    _seed_variables(db, spec, state)
    _seed_step_instances(db, spec, state)
    _seed_agent_runs(db, spec, state)
    return state


# --- between-pass operations ----------------------------------------------------------


async def apply_between(
    db: PostgresHubDatabase, op: Mapping[str, Any], domain: DomainState | None, *, engine: Any
) -> None:
    """Apply one state operation before a pass; an operation that raises fails the run.

    An engine-entry case has no domain and may only cache a late MCP injection.
    """
    kind = op.get("op")
    if kind == "cache_late_mcp_injection":
        await _cache_late_mcp_injection(db, op, engine)
        return
    if domain is None:
        raise ValueError(f"between operation {kind!r} needs a handler-entry domain")
    state = domain
    variables = SessionVariableManager(db)
    if kind == "claim_task":
        LocalTaskManager(db).claim_task(
            state.tasks[str(op["task"])][0],
            state.session(str(op.get("session", SELF))),
            select_claim=bool(op.get("select", True)),
        )
    elif kind == "link_commit":
        LocalTaskManager(db).link_commit(
            state.tasks[str(op["task"])][0], state.commits[int(op["commit"]) - 1]
        )
    elif kind == "set_variable":
        variables.set_variable(
            state.session(str(op.get("session", SELF))),
            str(op["name"]),
            substitute(op.get("value"), state),
        )
    elif kind == "git":
        if state.repo is None:
            raise ValueError("a git operation needs domain.repo")
        _git(state.repo, *[str(arg) for arg in op["args"]])
    elif kind == "write_file":
        if state.repo is None:
            raise ValueError("a write_file operation needs domain.repo")
        _write(state.repo, {str(op["path"]): str(op["text"])})
    elif kind == "approve_dirty_path":
        record_dirty_path_approval(
            db,
            state.session(str(op.get("session", SELF))),
            checkout_root=substitute(str(op.get("checkout", "{repo}")), state),
            paths=[str(path) for path in op["paths"]],
            approved_by=str(op["approved_by"]),
            reason=str(op["reason"]),
        )
    elif kind == "queue_memory_review":
        variables.queue_memory_review(
            state.session(str(op.get("session", SELF))), {"closure_id": str(op["closure_id"])}
        )
    elif kind == "record_memory_review":
        variables.record_memory_review(
            state.session(str(op.get("session", SELF))),
            {"closure_id": str(op["closure_id"])},
            max_records=50,
        )
    elif kind == "append_transcript":
        append_transcript(state, str(op.get("session", SELF)), list(op["records"]))
    elif kind == "register_wait":
        CoordinationWaitManager(db).register(
            state.session(str(op["waiter"])),
            state.session(str(op["owner"])),
            reply=bool(op.get("reply", True)),
        )
    elif kind == "close_task":
        LocalTaskManager(db).update_task(state.tasks[str(op["task"])][0], status="closed")
    elif kind == "queue_terminal_denial":
        # The third identical denial's hand-off, which the next enforcement pass flushes.
        storage = LocalAgentRunManager(db)
        run = storage.get(state.runs[str(op["run"])])
        engine._pending_terminal_denials[state.session(str(op.get("session", SELF)))] = (
            run,
            storage,
            str(op["error"]),
        )
    else:
        raise ValueError(f"unknown between operation {kind!r}")


async def _cache_late_mcp_injection(
    db: PostgresHubDatabase, op: Mapping[str, Any], engine: Any
) -> None:
    import asyncio

    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.workflows.definitions import RuleDefinitionBody
    from gobby.workflows.engine.mcp_injections import _CompletedMcpInjection

    row = RuleDefinitionManager(db).get_by_name(str(op["rule"]))
    if row is None:
        raise ValueError(f"no seeded rule named {op['rule']!r}")
    effect = RuleDefinitionBody.model_validate(row.definition_json).resolved_effects[
        int(op.get("effect_index", 0))
    ]
    session_id = str(op.get("session_id", rules_seed.SESSION_ID))
    key = str(op.get("call_key", "synthetic-late-call"))
    engine._mcp_injection_abandoned.add(key)
    filled = asyncio.get_running_loop().time() - float(op.get("age_seconds", 0))
    engine._cache_mcp_injection_result(
        session_id,
        key,
        _CompletedMcpInjection(op["raw_result"], filled),
        effect=effect,
        row=row,
    )


# --- observation ----------------------------------------------------------------------


def stored_variables(db: PostgresHubDatabase, session_id: str) -> dict[str, Any]:
    """The session's stored variable row, read directly (no defaults layered in)."""
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s", (session_id,)
    )
    if row is None:
        return {}
    value = row["variables"]
    return dict(json.loads(value) if isinstance(value, str) else value)


def observe_rows(
    db: PostgresHubDatabase, spec: Mapping[str, Any], state: DomainState
) -> dict[str, Any]:
    """The rows a case's ``observe`` names, projected to stable fields."""
    rows: dict[str, Any] = {}
    for key in spec.get("session_variables", []):
        rows.setdefault("session_variables", {})[key] = stored_variables(
            db, state.session(str(key))
        )
    if spec.get("step_instance"):
        instance = AgentStepInstanceManager(db).get_for_session(state.session())
        rows["step_instance"] = (
            None
            if instance is None
            else {
                "current_step": instance.current_step,
                "step_action_count": instance.step_action_count,
                "variables": dict(instance.variables),
            }
        )
    for key in spec.get("tasks", []):
        task = LocalTaskManager(db).get_task(state.tasks[str(key)][0])
        rows.setdefault("tasks", {})[key] = {
            "closed": task.closed_at is not None,
            "claimed_by_session_id": task.claimed_by_session_id,
            "commits": list(task.commits or []),
            "labels": list(task.labels or []),
        }
    for key in spec.get("agent_runs", []):
        run = LocalAgentRunManager(db).get(state.runs[str(key)])
        rows.setdefault("agent_runs", {})[key] = (
            None if run is None else {"status": run.status, "terminal_reason": run.terminal_reason}
        )
    return rows
