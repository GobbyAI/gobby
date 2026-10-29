"""Session-scope condition helpers bound to the rule engine's session manager."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks._path_scope import current_project_root, current_tool_cwd, resolve_tool_path
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.session_resolution import is_session_uuid
from gobby.terminals.actor_scope import SESSION_ACTOR_PREFIX, ActorScopeError, resolve_actor_scope
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import (
    AgentResolutionError,
    is_operator_session,
    resolve_agent,
    resolve_agent_with_row,
    session_has_capability,
    session_has_capability_or_operator,
)
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager


def session_refs_equal(
    session_manager: SessionManager | None, caller_ref: Any, left_ref: Any, right_ref: Any
) -> bool:
    """Compare session identities, resolving local refs in the caller's project."""
    if session_manager is None:
        raise RuntimeError("session reference comparison needs a session manager")
    if any(not isinstance(ref, str) or not ref for ref in (caller_ref, left_ref, right_ref)):
        return False
    try:
        caller_id = session_manager.resolve_session_reference(caller_ref)
        caller = session_manager.get(caller_id)
        if caller is None:
            return False
        left_id = session_manager.resolve_session_reference(left_ref, caller.project_id)
        right_id = session_manager.resolve_session_reference(right_ref, caller.project_id)
    except ValueError:
        return False
    return left_id == right_id


def send_keys_target_in_scope(
    session_manager: SessionManager | None, caller_ref: Any, target_ref: Any
) -> bool:
    """False only when a resolvable caller provably cannot reach a terminal tool's target.

    It scopes ``send_keys`` and ``capture_output``. The caller and target resolve
    exactly as ``send_keys`` resolves them, and the decision is ``ActorScope.admits``:
    the caller's own session, its project, and its agent tree in either direction.
    A missing or unresolvable caller, and an unresolvable target, pass here.
    ``send_keys`` refuses each with its own error. A spawned agent's calls are
    scoped like any other caller's: the opt-in ``no-send-keys`` rule decides
    whether it may use these tools at all. For ``capture_output``, a request with
    no caller is the operator's, and the target lookup takes a session UUID, which
    resolves here by primary key. Any other failure raises, and a raising block
    condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("send_keys target scope needs a session manager")
    if not isinstance(caller_ref, str) or not caller_ref:
        return True
    if not isinstance(target_ref, str) or not target_ref:
        return True
    try:
        scope = resolve_actor_scope(
            session_manager, f"{SESSION_ACTOR_PREFIX}{caller_ref}", allow_autonomous=True
        )
    except ActorScopeError:
        return True
    caller = scope.caller
    if caller is None:
        return True
    try:
        target_id = session_manager.resolve_session_reference(target_ref, caller.project_id)
    except ValueError:
        return True
    target = session_manager.get(target_id)
    if target is None:
        return True
    return scope.admits(project_id=target.project_id, session_id=target_id)


def send_message_target_allowed(
    session_manager: SessionManager | None, caller_ref: Any, agent_type: Any, target: Any
) -> bool:
    """Whether the caller's agent definition admits this send_message target mode.

    A session that is not a spawned agent may use every mode. A spawned agent may
    use only the modes in its definition's ``send_message_targets``; an omitted
    target is ``parent``. An unknown caller or definition raises, and a raising
    block condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("send_message target modes need a session manager")
    if not isinstance(caller_ref, str) or not caller_ref:
        raise ValueError("send_message caller is unknown")
    caller = session_manager.get(session_manager.resolve_session_reference(caller_ref))
    if caller is None:
        raise ValueError(f"send_message caller {caller_ref} is not registered")
    if not caller.agent_run_id and caller.agent_depth == 0:
        return True
    if not isinstance(agent_type, str) or not agent_type:
        raise ValueError(f"spawned session {caller.id} has no agent definition")
    row = AgentDefinitionManager(session_manager.db).get_by_name(agent_type, caller.project_id)
    if row is None:
        raise ValueError(f"agent definition {agent_type} is not registered")
    body = AgentDefinitionBody.model_validate({"name": row.name, **row.definition_json})
    return (target or "parent") in body.send_message_targets


# spawn_agent walks at most this many fallback_agent hops (spawn_agent/_factory.py).
_FALLBACK_CHAIN_MAX_HOPS = 5


def _fallback_chain(target: str, db: Any, project_id: str | None) -> list[str]:
    """``target`` and every fallback agent spawn_agent may start in its place."""
    chain = [target]
    body = resolve_agent(target, db, project_id=project_id)
    for _ in range(_FALLBACK_CHAIN_MAX_HOPS):
        candidate = None if body is None else body.fallback_agent
        if not candidate or candidate in chain:
            break
        body = resolve_agent(candidate, db, project_id=project_id)
        if body is None:
            break
        chain.append(candidate)
    return chain


def _spawn_caller(session_manager: SessionManager, caller_ref: Any) -> tuple[Session, str | None]:
    """The verified caller session and its agent run's definition name, None for a root.

    A root session has no agent run and depth 0. A spawned caller's name comes from
    its agent run record, never from a caller-supplied parent. A run-less session
    below the root, a missing run, or a run that names no definition raises.
    """
    if not isinstance(caller_ref, str) or not caller_ref:
        raise ValueError("spawn scope needs the caller session")
    caller = session_manager.get(session_manager.resolve_session_reference(caller_ref))
    if caller is None:
        raise ValueError(f"Caller session {caller_ref} not found")
    if caller.agent_run_id is None and caller.agent_depth == 0:
        return caller, None
    if caller.agent_run_id is None:
        raise ValueError(f"Spawned session {caller.id} has no agent run")
    run = LocalAgentRunManager(session_manager.db).get(caller.agent_run_id)
    if run is None or not run.agent_name:
        raise ValueError(f"Agent run {caller.agent_run_id} names no agent definition")
    if run.child_session_id != caller.id:
        from gobby.sessions.clear_continuation import resolve_clear_successor

        if (
            run.child_session_id is None
            or resolve_clear_successor(session_manager.db, run.child_session_id) != caller.id
        ):
            raise ValueError(
                f"Agent run {caller.agent_run_id} does not belong to caller {caller.id}"
            )
    return caller, run.agent_name


_TEAM_SEAT_TYPES = frozenset({"test-writer", "developer", "code-reviewer"})
# The devteam runbook titles its team tab "Dev Team"; an epic team's tab is "<this> - #<seq>".
_DEV_TEAM_TAB = "Dev Team"


def own_seat_resume(
    session_manager: SessionManager, caller_ref: Any, agent: Any, resume_session_id: Any
) -> bool:
    """Whether the caller is a team manager resuming a seat of its own team.

    Admission and placement both decide through this check, so an autonomous caller
    passes placement only where admission already allowed the same resume. A caller
    that does not resolve is not an own-seat resume; the actor scope still refuses it.
    """
    try:
        caller, agent_name = _spawn_caller(session_manager, caller_ref)
        return _manager_resumes_own_seat(
            session_manager, caller, agent_name, agent, resume_session_id
        )
    except (ValueError, AgentResolutionError):
        return False


def own_team_tab(session_manager: SessionManager, caller_ref: Any, agent: Any) -> str | None:
    """The team tab a running epic team manager's fresh seat may open into, or None.

    Placement admits an autonomous fresh seat only where its spawn is for the caller's own
    epic team: the caller is a running team manager, the seat is a team seat type, and the
    caller's team is an epic. The tab is the one the devteam runbook titles for that epic.
    A single-task team and any other spawn get no tab.
    """
    from gobby.agents.team_lifecycle import seat_team
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.team_records import EPIC_MODE

    if agent not in _TEAM_SEAT_TYPES:
        return None
    try:
        caller, agent_name = _spawn_caller(session_manager, caller_ref)
    except (ValueError, AgentResolutionError):
        return None
    if not _running_team_manager(session_manager, caller, agent_name):
        return None
    team = seat_team(session_manager.db, caller.id)
    if team is None or team.mode != EPIC_MODE:
        return None
    seq_num = LocalTaskManager(session_manager.db).get_task(team.task_id).seq_num
    return None if seq_num is None else f"{_DEV_TEAM_TAB} - #{seq_num}"


def _running_team_manager(
    session_manager: SessionManager, caller: Session, agent_name: str | None
) -> bool:
    """Whether the caller runs as an enabled team manager definition, bound to its run.

    The definition must set the ``team_manager`` variable, so a session variable alone
    never grants it. The caller's running run must be bound to the caller.
    """
    if agent_name is None:
        return False
    found = resolve_agent_with_row(agent_name, session_manager.db, project_id=caller.project_id)
    if found is None or not found[1].enabled:
        return False
    if found[0].workflows.variables.get("team_manager") is not True:
        return False
    manager_run = (
        LocalAgentRunManager(session_manager.db).get(caller.agent_run_id)
        if caller.agent_run_id
        else None
    )
    return (
        manager_run is not None
        and manager_run.status == "running"
        and manager_run.child_session_id == caller.id
    )


def _manager_resumes_own_seat(
    session_manager: SessionManager,
    caller: Session,
    agent_name: str | None,
    agent: Any,
    resume_session_id: Any,
) -> bool:
    """Whether a running team manager resumes a seat of its own team as the seat's type.

    The caller's enabled installed definition must set the ``team_manager`` variable,
    so a session variable alone never grants it. The caller's running run must be
    bound to the caller, and the seat's run to the seat. The seat must be a test
    writer, developer or code reviewer in the caller's project and machine, launched
    by a runbook of the caller's admitted team. The resume must name the seat's stored
    type, so a resume cannot re-type it. A fresh spawn has no resume reference.
    """
    from gobby.agents.team_lifecycle import seat_team
    from gobby.workflows.state_manager import SessionVariableManager

    if not isinstance(resume_session_id, str) or not resume_session_id:
        return False
    if not _running_team_manager(session_manager, caller, agent_name):
        return False
    runs = LocalAgentRunManager(session_manager.db)
    team = seat_team(session_manager.db, caller.id)
    if team is None:
        return False
    seat = session_manager.get(
        session_manager.resolve_session_reference(resume_session_id, caller.project_id)
    )
    if seat is None or seat.project_id != caller.project_id or seat.machine_id != caller.machine_id:
        return False
    seat_type = (
        SessionVariableManager(session_manager.db)
        .get_variable_subset(seat.id, ("_agent_type",))
        .get("_agent_type")
    )
    if seat_type not in _TEAM_SEAT_TYPES or agent != seat_type:
        return False
    seat_run = runs.get(seat.agent_run_id) if seat.agent_run_id else None
    if seat_run is None or seat_run.child_session_id != seat.id:
        return False
    seat_binding = seat_team(session_manager.db, seat.id)
    return seat_binding is not None and seat_binding.team_id == team.team_id


def spawn_target_allowed(
    session_manager: SessionManager | None,
    caller_ref: Any,
    tool_name: Any,
    agent: Any,
    resume_session_id: Any = None,
    *,
    target_project_id: str | None = None,
) -> bool:
    """Whether the caller may start every agent the gobby-agents ``tool_name`` call can.

    A root session (no agent run, depth 0) spawns anything. A spawned caller's
    definition comes from its agent run record, and its ``spawnable_agents``
    decides: any agent, the listed agents, or none. Every effective target must
    be allowed. Without a resolved target project, a before_tool rule checks
    only requested names. Admission passes the launch's target project to check
    its fallback chain as well. A team manager may also resume an existing seat of
    its own team, which is the one spawn its definition does not list. Anything
    unresolvable raises and fails closed.
    """
    if session_manager is None:
        raise RuntimeError("spawn target scope needs a session manager")
    caller, agent_name = _spawn_caller(session_manager, caller_ref)
    if agent_name is None:
        return True
    if tool_name != "spawn_agent":
        raise ValueError(f"spawn target scope does not cover tool {tool_name!r}")
    if _manager_resumes_own_seat(session_manager, caller, agent_name, agent, resume_session_id):
        return True
    body = resolve_agent(agent_name, session_manager.db, project_id=caller.project_id)
    if body is None:
        raise ValueError(f"Agent definition {agent_name!r} not found")
    target = agent or "default"
    names = (
        [target]
        if target_project_id is None
        else _fallback_chain(target, session_manager.db, target_project_id)
    )
    return all(body.may_spawn(name) for name in names)


def network_override_allowed(session_manager: SessionManager | None, caller_ref: Any) -> bool:
    """Whether the caller may pass an explicit ``network`` profile to a spawn.

    The operator terminal or an enabled definition with override_spawn_network may.
    A spawned caller's definition comes from its verified run, never a caller-supplied
    parent_session_id. Other callers inherit the definition's network profile.
    Anything unresolvable raises, and a raising block condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("network override scope needs a session manager")
    caller, agent_name = _spawn_caller(session_manager, caller_ref)
    if agent_name is None:
        return session_has_capability_or_operator(
            session_manager.db, caller.id, "override_spawn_network"
        )
    resolved = resolve_agent_with_row(agent_name, session_manager.db, project_id=caller.project_id)
    return (
        resolved is not None
        and resolved[1].enabled
        and "override_spawn_network" in resolved[0].capabilities
    )


_SHELL_OPERATORS = re.compile(r"[<>|;&()]")
# One lexical unit of shell text. Quoted spans, comments and escapes are consumed whole, so a
# heredoc-looking fragment inside any of them opens nothing.
_SHELL_UNIT = re.compile(
    r"\$'(?:\\.|[^'\\])*'"  # ANSI-C quoted
    r"|'[^']*'"
    r'|"(?:\\.|[^"\\])*"'
    r"|`(?:\\.|[^`\\])*`"
    r"|\\."
    r"|(?<![^\s;&|()])#[^\n]*"  # a comment starts a word
    r"|(?<!<)<<(?P<dash>-?)[ \t]*(?P<quote>['\"])(?P<tag>\w+)(?P=quote)"  # quoted heredoc header
    r"|.",
    re.DOTALL,
)
# A `<name>` segment is documentation placeholder text: seats write only inside their own
# folders, and no folder is named that way.
_PLACEHOLDER_SEGMENT = re.compile(r"<[A-Za-z_][\w-]*>")


def _skip_heredoc_bodies(command: str, pos: int, pending: list[tuple[str, bool]]) -> int:
    """Position just past the last delimiter line, reading the bodies in header order."""
    for tag, strip_tabs in pending:
        while pos < len(command):
            end = command.find("\n", pos)
            line_end = len(command) if end == -1 else end
            line = command[pos:line_end]
            pos = len(command) if end == -1 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                break
    return pos


def _split_heredoc_bodies(command: str) -> tuple[str, list[str]]:
    """Split each quoted-delimiter heredoc body out of the command text.

    The shell reads a quoted-delimiter body as literal text, so it is returned apart from the
    command. The caller judges a body as shell code when a shell reads it, and as text otherwise.
    A body starts after the unquoted newline that ends its header line and runs to the
    delimiter line, matched exactly as the shell matches it.
    """
    kept: list[str] = []
    bodies: list[str] = []
    pending: list[tuple[str, bool]] = []
    pos = 0
    while pos < len(command):
        unit = _SHELL_UNIT.match(command, pos)
        if unit is None:  # unreachable: the final alternative matches any character
            return command, []
        text = unit.group(0)
        pos = unit.end()
        if unit.group("tag") is not None:
            pending.append((unit.group("tag"), unit.group("dash") == "-"))
        elif text == "\n" and pending:
            start = pos
            pos = _skip_heredoc_bodies(command, pos, pending)
            bodies.append(command[start:pos])
            pending = []
        kept.append(text)
    return "".join(kept), bodies


def _shell_tokens(command: str) -> list[str]:
    """Shell words with operators split off only outside quotes, so quoted text stays whole."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# Commands that run their input as shell code, so a quoted heredoc body they read is shell code.
_BODY_CONSUMERS = _SHELL_WRAPPERS | {"eval", "source", "."}
_SHELL_COMMAND_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def _wrapped_commands(tokens: list[str]) -> list[str]:
    """Command strings a shell runs with `-c`, or `eval` runs, such as `bash -c "cat <path>"`.

    The flag may follow options that take values, so every later token is searched. Reading
    past the real flag only judges one more word.
    """
    commands: list[str] = []
    for index, token in enumerate(tokens):
        name = os.path.basename(os.path.expandvars(token))
        if name == "eval":
            commands.append(" ".join(tokens[index + 1 :]))
        elif name in _SHELL_WRAPPERS:
            for flag_index in range(index + 1, len(tokens)):
                if _SHELL_COMMAND_FLAG.fullmatch(tokens[flag_index]):
                    commands.extend(tokens[flag_index + 1 : flag_index + 2])
                    break
    return commands


def _path_words(tokens: list[str]) -> list[str]:
    """Words that look like paths. A quoted token holding several words is judged word by word."""
    expanded = (part for token in tokens for part in os.path.expandvars(token).split())
    return [part for part in expanded if "/" in part or part.startswith("~")]


def _shell_path_words(command: Any) -> list[str]:
    """Shell words that look like paths: operators split outside quotes, env vars expanded.

    A quoted-delimiter heredoc body is shell code when a shell reads it, and text otherwise. A
    command string that a shell wrapper or `eval` runs is parsed the same way.
    """
    if not isinstance(command, str):
        return []
    text, bodies = _split_heredoc_bodies(command)
    try:
        tokens = _shell_tokens(text)
    except ValueError:
        tokens = _SHELL_OPERATORS.sub(" ", text).split()
    shell_reads = any(os.path.basename(os.path.expandvars(t)) in _BODY_CONSUMERS for t in tokens)
    words = _path_words(tokens)
    for body in bodies:
        words.extend(_shell_path_words(body) if shell_reads else _path_words(body.split()))
    for wrapped in _wrapped_commands(tokens):
        words.extend(_shell_path_words(wrapped))
    return words


def _workspace_read_targets(event_data: Mapping[str, Any] | None, tool_input: Any) -> list[Path]:
    """Resolved paths a tool reads: path-like Bash words, or the path fields of a file tool."""
    if not isinstance(tool_input, Mapping):
        return []
    data = event_data or {}
    base = current_tool_cwd(data) or current_project_root(data)
    raws: list[Any]
    if data.get("tool_name") == "Bash":
        raws = list(_shell_path_words(tool_input.get("command")))
    else:
        raws = [tool_input.get("file_path"), tool_input.get("path")]
        if data.get("tool_name") == "Glob":
            raws.append(tool_input.get("pattern"))
    return [path for raw in raws if (path := resolve_tool_path(raw, base)) is not None]


def _variable_folder(value: Any) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def _placeholder_folder(target: Path, root: Path) -> bool:
    """Whether a path names a ``<placeholder>`` folder directly under the workspace root.

    No folder is named that way, so the path is documentation text. A traversal through a
    placeholder still resolves to a real folder and is judged there.
    """
    first = target.relative_to(root).parts[:1]
    return bool(first) and _PLACEHOLDER_SEGMENT.fullmatch(first[0]) is not None


def _workspace_path_readable(
    target: Path,
    session_manager: SessionManager | None,
    caller_id: str | None,
    folders: tuple[Path | None, Path | None, Path | None],
) -> bool:
    """Whether a caller may read one path under the workspace root.

    ``folders`` is (own session folder, epic folder, anchor). The anchor is the project folder
    holding the definition folders; its README is shared, and a predecessor folder of the same
    definition is readable only for a persistent caller (see ``predecessor_folder_readable``).
    """
    from gobby.agents.agent_workspaces import predecessor_folder_readable

    own, epic, anchor = folders
    if any(folder is not None and target.is_relative_to(folder) for folder in (own, epic)):
        return True
    if anchor is None or not target.is_relative_to(anchor):
        return False
    if target == anchor / "README.md":
        return True
    relative = target.relative_to(anchor)
    if len(relative.parts) < 2 or not caller_id or session_manager is None:
        return False
    folder = anchor / relative.parts[0] / relative.parts[1]
    return predecessor_folder_readable(session_manager, caller_id, str(folder))


def agent_workspace_read_violation(
    session_manager: SessionManager | None,
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    variables: Mapping[str, Any],
    caller_id: str | None,
) -> bool:
    """True when a tool reads an agent workspace folder the caller may not see.

    Only paths under the workspace root are judged. A seat reads its own folder, its epic
    folder and the project README; a caller with no bound folder reads none of the root.
    """
    from gobby.agents.agent_workspaces import (
        WORKSPACE_DIR_VARIABLE,
        WORKSPACE_EPIC_DIR_VARIABLE,
        workspaces_root,
    )

    root = resolve_tool_path(str(workspaces_root()), None)
    if root is None:
        return False
    targets = [
        target
        for target in _workspace_read_targets(event_data, tool_input)
        if target.is_relative_to(root) and not _placeholder_folder(target, root)
    ]
    if not targets:
        return False
    own = _variable_folder(variables.get(WORKSPACE_DIR_VARIABLE))
    epic = _variable_folder(variables.get(WORKSPACE_EPIC_DIR_VARIABLE))
    bound = own or epic
    anchor = bound.parents[1] if bound is not None else None
    folders = (own, epic, anchor)
    return not all(
        _workspace_path_readable(target, session_manager, caller_id, folders) for target in targets
    )


def operator_session(session_manager: SessionManager | None, caller_id: Any) -> bool:
    """Resolve the stored caller identity before granting the operator exemption."""
    if session_manager is None or not isinstance(caller_id, str) or not is_session_uuid(caller_id):
        return False
    session = session_manager.get(caller_id)
    if session is None:
        return False
    variables = SessionVariableManager(session_manager.db).get_variable_subset(
        caller_id, ("_agent_type",)
    )
    return is_operator_session(session, variables.get("_agent_type"))


def session_condition_helpers(
    session_manager: SessionManager | None,
) -> dict[str, Callable[..., Any]]:
    """Bind the session-scope helpers to the evaluator's session manager."""
    from gobby.workflows.engine.idle_declaration import readiness_message_completed

    return {
        "operator_session": lambda caller_id: operator_session(session_manager, caller_id),
        "session_has_capability": lambda caller_id, capability: (
            session_manager is not None
            and isinstance(caller_id, str)
            and is_session_uuid(caller_id)
            and isinstance(capability, str)
            and session_has_capability(session_manager.db, caller_id, capability)
        ),
        "seat_readiness_completed": lambda caller_id, output: readiness_message_completed(
            session_manager, caller_id, output
        ),
        "session_refs_equal": lambda caller_ref, left_ref, right_ref: session_refs_equal(
            session_manager, caller_ref, left_ref, right_ref
        ),
        "send_keys_target_in_scope": lambda caller_ref, target_ref: send_keys_target_in_scope(
            session_manager, caller_ref, target_ref
        ),
        "send_message_target_allowed": lambda caller_ref, agent_type, target: (
            send_message_target_allowed(session_manager, caller_ref, agent_type, target)
        ),
        "spawn_target_allowed": lambda caller_ref,
        tool_name,
        agent,
        resume_session_id=None: spawn_target_allowed(
            session_manager, caller_ref, tool_name, agent, resume_session_id
        ),
        "network_override_allowed": lambda caller_ref: network_override_allowed(
            session_manager, caller_ref
        ),
        "agent_workspace_read_violation": lambda event_data, tool_input, variables, caller_id: (
            agent_workspace_read_violation(
                session_manager, event_data, tool_input, variables, caller_id
            )
        ),
    }
