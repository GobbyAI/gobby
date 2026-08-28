"""Import and public API invariants for session storage."""

from __future__ import annotations

import inspect

import pytest

from gobby.storage import SessionManager as LazySessionManager
from gobby.storage.sessions import SessionManager, ensure_system_session
from gobby.storage.sessions import logger as public_logger
from gobby.storage.sessions._constants import (
    ensure_system_session as internal_ensure_system_session,
)
from gobby.storage.sessions._constants import (
    logger as internal_logger,
)
from gobby.storage.sessions._manager import SessionManager as InternalSessionManager
from gobby.storage.sessions._update_sentinel import UNSET

pytestmark = pytest.mark.unit

EXPECTED_PUBLIC_METHOD_SIGNATURES = {
    "__init__": "(self, db: 'HubDatabase | None' = None, *, session_storage: "
    "'SessionManager | None' = None, logger_instance: 'logging.Logger | None' = None, "
    "config: 'DaemonConfig | None' = None)",
    "add_usage_delta": "(self, session_id: 'str', input_tokens: 'int' = 0, "
    "output_tokens: 'int' = 0, cache_creation_tokens: 'int' = 0, "
    "cache_read_tokens: 'int' = 0, context_window: 'int | None' = None, "
    "model: 'str | None' = None) -> 'bool'",
    "backfill_terminal_context": "(self, session_id: 'str', terminal_context: "
    "'dict[str, Any] | None') -> 'tuple[Session | None, bool]'",
    "cache_session_mapping": "(self, external_id: 'str', source: 'str', "
    "session_id: 'str', project_id: 'str | None' = None, "
    "session_type: 'str' = 'terminal') -> 'None'",
    "clear_had_edits": "(self, session_id: 'str') -> 'None'",
    "cleanup_expired_session_state": "(self) -> 'SessionStateCleanupResult'",
    "continue_terminal_session_as_web_chat": "(self, session_id: 'str', *, "
    "source: 'str', model: 'str | None', project_id: 'str', "
    "sandbox_policy_hash: 'str | None') -> 'Session | None'",
    "expire_tmux_pane_sessions": "(self, machine_id: 'str', socket_identity: 'str', "
    "pane: 'str') -> 'list[str]'",
    "expire_tmux_socket_sessions": "(self, machine_id: 'str', "
    "socket_identity: 'str') -> 'list[str]'",
    "rebind_resumed_terminal_session": "(self, session_id: 'str', *, machine_id: 'str', "
    "project_id: 'str', source: 'str', transcript_path: 'str | None', "
    "terminal_context: 'dict[str, Any] | None', workflow_name: 'str | None', "
    "agent_depth: 'int', sandbox_enabled: 'bool | None') -> 'Session | None'",
    "count": "(self, project_id: 'str | None' = None, status: 'str | None' = None, "
    "source: 'str | None' = None, machine_id: 'str | None' = None) -> 'int'",
    "count_by_status": "(self, project_id: 'str | None' = None) -> 'dict[str, int]'",
    "create_web_chat_session": "(self, *, machine_id: 'str | None', project_id: 'str', "
    "source: 'str', title: 'str | None' = None, model: 'str | None' = None, "
    "is_local: 'bool' = False, chat_mode: 'str | None' = None, sandbox_enabled: 'bool', "
    "sandbox_policy_hash: 'str') -> 'Session'",
    "delete": "(self, session_id: 'str') -> 'bool'",
    "expire_empty_sessions": "(self, timeout_hours: 'int' = 2) -> 'int'",
    "expire_if_active": "(self, session_id: 'str') -> 'Session | None'",
    "expire_orphaned_handoff_sessions": "(self, timeout_minutes: 'int' = 30) -> 'int'",
    "expire_stale_sessions": "(self, timeout_hours: 'int' = 24) -> 'int'",
    "prune_stale_compact_workflow_instances": "(self, retention_hours: 'int' = 24) -> 'int'",
    "fetch_task_refs_by_session": "(self, session_ids: 'Sequence[str]') -> "
    "'dict[str, _TaskRefsByRole]'",
    "find_active_by_external_id": "(self, external_id: 'str', source: 'str', "
    "session_type: 'str' = 'terminal') -> 'Session | None'",
    "find_active_by_terminal_context": "(self, project_id: 'str | None', parent_pid: 'Any', "
    "terminal_context: 'dict[str, Any] | str | None' = None) -> 'Session | None'",
    "find_by_terminal_identity": "(self, identity: 'TerminalIdentity') -> 'list[Session]'",
    "resolve_current_terminal_session": "(self, project_id: 'str | None', parent_pid: 'Any', "
    "terminal_context: 'dict[str, Any] | str | None') -> 'Session | None'",
    "find_by_external_id": "(self, external_id: 'str', project_id: 'str | None', "
    "source: 'str', session_type: 'str | None' = 'terminal') -> 'Session | None'",
    "find_by_external_id_all_sources": "(self, external_id: 'str', "
    "project_id: 'str | None', session_type: 'str | None' = 'terminal') -> 'list[Session]'",
    "find_by_external_id_any_project": "(self, external_id: 'str', source: 'str', "
    "session_type: 'str | None' = 'terminal') -> 'Session | None'",
    "find_children": "(self, parent_session_id: 'str') -> 'list[Session]'",
    "find_parent": "(self, machine_id: 'str', project_id: 'str', "
    "source: 'str | None' = None, status: 'str' = 'handoff_ready', "
    "max_age_minutes: 'int' = 10, terminal_context: 'dict[str, Any] | str | None' = None, "
    "candidate_limit: 'int' = 1) -> 'Session | None'",
    "get": "(self, session_id: 'str') -> 'Session | None'",
    "get_pending_transcript_sessions": "(self, limit: 'int' = 10) -> 'list[Session]'",
    "get_summary_revision": "(self, revision_id: 'str') -> 'dict[str, Any] | None'",
    "get_session_id": "(self, external_id: 'str', source: 'str', "
    "project_id: 'str | None' = None, session_type: 'str' = 'terminal') -> 'str | None'",
    "get_sessions_since": "(self, since: 'datetime', project_id: 'str | None' = None) "
    "-> 'list[Session]'",
    "is_ancestor": "(self, ancestor_id: 'str', descendant_id: 'str') -> 'bool'",
    "list": "(self, project_id: 'str | None' = None, status: 'str | None' = None, "
    "source: 'str | None' = None, limit: 'int' = 100, "
    "exclude_subagents: 'bool' = False, machine_id: 'str | None' = None, "
    "cursor_updated_at: 'str | None' = None, "
    "cursor_id: 'str | None' = None, sources: 'Sequence[str] | None' = None, "
    "statuses: 'Sequence[str] | None' = None, modes: 'Sequence[str] | None' = None, "
    "models: 'Sequence[str] | None' = None, session_seq_min: 'int | None' = None, "
    "session_seq_max: 'int | None' = None, task_ref_min: 'int | None' = None, "
    "task_ref_max: 'int | None' = None, task_ref_roles: 'Sequence[str] | None' = None, "
    "created_after: 'str | None' = None, created_before: 'str | None' = None) -> "
    "'list[Session]'",
    "lookup_session_id": "(self, external_id: 'str', source: 'str', "
    "project_id: 'str | None', session_type: 'str' = 'terminal') -> 'str | None'",
    "list_summary_revisions": "(self, session_id: 'str', *, limit: 'int' = 20) -> "
    "'list[dict[str, Any]]'",
    "mark_had_edits": "(self, session_id: 'str') -> 'Session | None'",
    "mark_session_expired": (
        "(self, session_id: 'str', *, cause: 'ContestedExpiryCause') -> 'bool'"
    ),
    "mark_transcript_processed": "(self, session_id: 'str') -> 'Session | None'",
    "move_to_project": "(self, session_id: 'str', project_id: 'str') -> 'Session | None'",
    "pause_inactive_active_sessions": "(self, timeout_minutes: 'int' = 30) -> 'int'",
    "persist_summary_state": "(self, session_id: 'str', *, summary_markdown: 'str', "
    "generation_mode: 'str', source_context_hash: 'str | None' = None, "
    "previous_revision_id: 'str | None' = None, "
    "metadata_json: 'Mapping[str, Any] | None' = None, "
    "summary_path: 'str | None | UnsetType' = UNSET) -> 'Session | None'",
    "prune_empty_sessions": "(self, min_age_hours: 'int' = 1) -> 'int'",
    "record_skills_used": "(self, session_id: 'str', skill_names: 'list[str]') -> 'int'",
    "register": "(self, external_id: 'str', machine_id: 'str | None', source: 'str', "
    "project_id: 'str | None', title: 'str | None | UnsetType' = UNSET, "
    "transcript_path: 'str | None | UnsetType' = UNSET, "
    "git_branch: 'str | None | UnsetType' = UNSET, "
    "parent_session_id: 'str | None | UnsetType' = UNSET, agent_depth: 'int' = 0, "
    "spawned_by_agent_id: 'str | None' = None, "
    "terminal_context: 'dict[str, Any] | None' = None, "
    "workflow_name: 'str | None' = None, session_type: 'str' = 'terminal', "
    "is_local: 'bool' = False, sandbox_enabled: 'bool | None' = None, "
    "sandbox_policy_hash: 'str | None' = None, "
    "title_source: 'str | None | UnsetType' = UNSET) -> 'Session'",
    "register_session": "(self, external_id: 'str', machine_id: 'str | None', source: 'str', "
    "project_id: 'str | None', parent_session_id: 'str | None | UnsetType' = UNSET, "
    "transcript_path: 'str | None' = None, title: 'str | None' = None, "
    "git_branch: 'str | None' = None, project_path: 'str | None' = None, "
    "terminal_context: 'dict[str, Any] | None' = None, "
    "workflow_name: 'str | None' = None, agent_depth: 'int' = 0, "
    "is_local: 'bool' = False, sandbox_enabled: 'bool | None' = None) -> 'str'",
    "register_session_change_listener": "(self, listener: 'SessionChangeCallback') -> 'None'",
    "register_status_transition_listener": "(self, listener: "
    "'SessionStatusTransitionCallback') -> 'None'",
    "register_title_listener": "(self, listener: 'TitleChangeCallback') -> 'None'",
    "renumber_project_sessions": "(self, project_id: 'str', *, dry_run: 'bool' = True) -> "
    "'list[SessionRenumberMapping]'",
    "reset_transcript_processed": "(self, session_id: 'str') -> 'Session | None'",
    "recover_session": "(self, external_id: 'str', source: 'str', "
    "project_id: 'str | None', session_type: 'str | None' = 'terminal') -> 'Session | None'",
    "activate_web_chat_session": "(self, session_id: 'str') -> 'Session | None'",
    "revive_expired_terminal_session": "(self, session_id: 'str') -> 'Session | None'",
    "resolve_session_reference": "(self, ref: 'str', project_id: 'str | None' = None) -> 'str'",
    "touch": "(self, session_id: 'str') -> 'None'",
    "unregister_session_change_listener": "(self, listener: 'SessionChangeCallback') -> 'None'",
    "unregister_status_transition_listener": "(self, listener: "
    "'SessionStatusTransitionCallback') -> 'None'",
    "unregister_title_listener": "(self, listener: 'TitleChangeCallback') -> 'None'",
    "update": "(self, session_id: 'str', *, external_id: 'str | None' = None, "
    "source: 'str | None' = None, model: 'str | None' = None, "
    "chat_mode: 'str | None' = None, session_type: 'str | None' = None, "
    "transcript_path: 'str | None | UnsetType' = UNSET, status: 'str | None' = None, "
    "title: 'str | None | UnsetType' = UNSET, "
    "title_source: 'str | None | UnsetType' = UNSET, "
    "git_branch: 'str | None | UnsetType' = UNSET, "
    "terminal_context: 'dict[str, Any] | None' = None, "
    "project_id: 'str | None' = None, sandbox_enabled: 'bool | None' = None, "
    "sandbox_policy_hash: 'str | None' = None) -> 'Session | None'",
    "update_approved_tools": "(self, session_id: 'str', tools: 'set[str]') -> 'None'",
    "update_chat_mode": "(self, session_id: 'str', chat_mode: 'str') -> 'None'",
    "update_context_usage": "(self, session_id: 'str', snapshot: 'ContextUsageSnapshot') -> 'bool'",
    "update_model": "(self, session_id: 'str', model: 'str') -> 'Session | None'",
    "update_parent_session_id": "(self, session_id: 'str', parent_session_id: 'str | None') "
    "-> 'Session | None'",
    "update_session_status": "(self, session_id: 'str', status: 'str', *, "
    "activity_confirmed: 'bool' = False) -> 'bool'",
    "update_stats": "(self, session_id: 'str', message_count: 'int | None' = None, "
    "turn_count: 'int | None' = None, tool_call_count: 'int | None' = None, "
    "last_assistant_content: 'str | None' = None) -> 'Session | None'",
    "update_status": "(self, session_id: 'str', status: 'str') -> 'Session | None'",
    "update_status_from_activity": "(self, session_id: 'str', status: 'str') -> 'Session | None'",
    "update_status_if_non_terminal": "(self, session_id: 'str', status: 'str') -> 'Session | None'",
    "update_summary": "(self, session_id: 'str', "
    "summary_path: 'str | None | UnsetType' = UNSET, "
    "summary_markdown: 'str | None | UnsetType' = UNSET) -> 'Session | None'",
    "update_terminal_pickup_metadata": "(self, session_id: 'str', "
    "workflow_name: 'str | None' = None, agent_run_id: 'str | None' = None, "
    "context_injected: 'bool | None' = None, original_prompt: 'str | None' = None) "
    "-> 'Session | None'",
    "update_title": "(self, session_id: 'str', title: 'str', *, "
    "title_source: 'str | None' = 'manual') -> 'Session | None'",
    "update_usage": "(self, session_id: 'str', input_tokens: 'int', "
    "output_tokens: 'int', cache_creation_tokens: 'int', cache_read_tokens: 'int', "
    "context_window: 'int | None' = None, model: 'str | None' = None) -> 'bool'",
}


def _normalized_signature(func: object) -> str:
    signature = inspect.signature(func)
    parameters = []
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            parameter = parameter.replace(annotation=inspect.Signature.empty)
        parameters.append(parameter)
    return str(signature.replace(parameters=parameters)).replace(repr(UNSET), "UNSET")


def test_storage_sessions_public_imports_are_compatible() -> None:
    """These imports stay public because callers patch the module namespace directly."""
    assert SessionManager is InternalSessionManager
    assert SessionManager is LazySessionManager
    assert ensure_system_session is internal_ensure_system_session
    assert public_logger is internal_logger


def test_session_manager_public_method_signatures_are_stable() -> None:
    """Public SessionManager signatures are part of the compatibility surface."""
    public_methods = {
        name: _normalized_signature(member)
        for name, member in inspect.getmembers(SessionManager, predicate=inspect.isfunction)
        if not name.startswith("_") or name == "__init__"
    }

    assert public_methods == EXPECTED_PUBLIC_METHOD_SIGNATURES
