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

pytestmark = pytest.mark.unit

EXPECTED_PUBLIC_METHOD_SIGNATURES = {
    "__init__": "(self, db: 'DatabaseProtocol | None' = None, *, session_storage: "
    "'SessionManager | None' = None, logger_instance: 'logging.Logger | None' = None, "
    "config: 'DaemonConfig | None' = None)",
    "add_usage_delta": "(self, session_id: 'str', input_tokens: 'int' = 0, "
    "output_tokens: 'int' = 0, cache_creation_tokens: 'int' = 0, "
    "cache_read_tokens: 'int' = 0, context_window: 'int | None' = None, "
    "model: 'str | None' = None) -> 'bool'",
    "backfill_terminal_context": "(self, session_id: 'str', terminal_context: "
    "'dict[str, Any] | None') -> 'tuple[Session | None, bool]'",
    "cache_session_mapping": "(self, external_id: 'str', source: 'str', "
    "session_id: 'str') -> 'None'",
    "clear_had_edits": "(self, session_id: 'str') -> 'None'",
    "count": "(self, project_id: 'str | None' = None, status: 'str | None' = None, "
    "source: 'str | None' = None) -> 'int'",
    "count_by_status": "(self) -> 'dict[str, int]'",
    "create_web_chat_session": "(self, *, machine_id: 'str', project_id: 'str', "
    "source: 'str', title: 'str | None' = None, model: 'str | None' = None, "
    "chat_mode: 'str | None' = None, sandbox_enabled: 'bool', "
    "sandbox_policy_hash: 'str') -> 'Session'",
    "delete": "(self, session_id: 'str') -> 'bool'",
    "expire_empty_sessions": "(self, timeout_hours: 'int' = 2) -> 'int'",
    "expire_orphaned_handoff_sessions": "(self, timeout_minutes: 'int' = 30) -> 'int'",
    "expire_stale_sessions": "(self, timeout_hours: 'int' = 24) -> 'int'",
    "find_active_by_external_id": "(self, external_id: 'str', source: 'str') -> 'Session | None'",
    "find_by_external_id": "(self, external_id: 'str', machine_id: 'str', "
    "project_id: 'str | None', source: 'str', session_type: 'str | None' = None) "
    "-> 'Session | None'",
    "find_by_external_id_all_sources": "(self, external_id: 'str', machine_id: 'str', "
    "project_id: 'str | None', session_type: 'str | None' = None) -> 'list[Session]'",
    "find_by_external_id_any_project": "(self, external_id: 'str', machine_id: 'str', "
    "source: 'str', session_type: 'str | None' = None) -> 'Session | None'",
    "find_children": "(self, parent_session_id: 'str') -> 'list[Session]'",
    "find_parent": "(self, machine_id: 'str', project_id: 'str', "
    "source: 'str | None' = None, status: 'str' = 'handoff_ready', "
    "max_age_minutes: 'int' = 10) -> 'Session | None'",
    "find_parent_session": "(self, machine_id: 'str', source: 'str', "
    "project_id: 'str', max_attempts: 'int' = 30) -> 'tuple[str, str | None] | None'",
    "get": "(self, session_id: 'str') -> 'Session | None'",
    "get_pending_transcript_sessions": "(self, limit: 'int' = 10) -> 'list[Session]'",
    "get_session_id": "(self, external_id: 'str', source: 'str') -> 'str | None'",
    "get_sessions_since": "(self, since: 'datetime', project_id: 'str | None' = None) "
    "-> 'list[Session]'",
    "is_ancestor": "(self, ancestor_id: 'str', descendant_id: 'str') -> 'bool'",
    "list": "(self, project_id: 'str | None' = None, status: 'str | None' = None, "
    "source: 'str | None' = None, limit: 'int' = 100, "
    "exclude_subagents: 'bool' = False) -> 'list[Session]'",
    "lookup_session_id": "(self, external_id: 'str', source: 'str', machine_id: 'str', "
    "project_id: 'str | None') -> 'str | None'",
    "mark_had_edits": "(self, session_id: 'str') -> 'Session | None'",
    "mark_session_expired": "(self, session_id: 'str') -> 'bool'",
    "mark_transcript_processed": "(self, session_id: 'str') -> 'Session | None'",
    "pause_inactive_active_sessions": "(self, timeout_minutes: 'int' = 30) -> 'int'",
    "prune_empty_sessions": "(self, min_age_hours: 'int' = 1) -> 'int'",
    "recalculate_stats": "(self, session_id: 'str') -> 'Session | None'",
    "record_skills_used": "(self, session_id: 'str', skill_names: 'list[str]') -> 'int'",
    "register": "(self, external_id: 'str', machine_id: 'str', source: 'str', "
    "project_id: 'str | None', title: 'str | None' = None, "
    "transcript_path: 'str | None' = None, git_branch: 'str | None' = None, "
    "parent_session_id: 'str | None' = None, agent_depth: 'int' = 0, "
    "spawned_by_agent_id: 'str | None' = None, "
    "terminal_context: 'dict[str, Any] | None' = None, "
    "workflow_name: 'str | None' = None, session_type: 'str' = 'terminal', "
    "sandbox_enabled: 'bool | None' = None, "
    "sandbox_policy_hash: 'str | None' = None) -> 'Session'",
    "register_session": "(self, external_id: 'str', machine_id: 'str', source: 'str', "
    "project_id: 'str | None', parent_session_id: 'str | None' = None, "
    "transcript_path: 'str | None' = None, title: 'str | None' = None, "
    "git_branch: 'str | None' = None, project_path: 'str | None' = None, "
    "terminal_context: 'dict[str, Any] | None' = None, "
    "workflow_name: 'str | None' = None, agent_depth: 'int' = 0, "
    "sandbox_enabled: 'bool | None' = None) -> 'str'",
    "register_title_listener": "(self, listener: 'TitleChangeCallback') -> 'None'",
    "reset_transcript_processed": "(self, session_id: 'str') -> 'Session | None'",
    "recover_session": "(self, external_id: 'str', source: 'str', machine_id: 'str', "
    "project_id: 'str | None', session_type: 'str | None' = None) -> 'Session | None'",
    "resolve_session_reference": "(self, ref: 'str', project_id: 'str | None' = None) -> 'str'",
    "touch": "(self, session_id: 'str') -> 'None'",
    "unregister_title_listener": "(self, listener: 'TitleChangeCallback') -> 'None'",
    "update": "(self, session_id: 'str', *, external_id: 'str | None' = None, "
    "source: 'str | None' = None, model: 'str | None' = None, "
    "chat_mode: 'str | None' = None, session_type: 'str | None' = None, "
    "transcript_path: 'str | None' = None, status: 'str | None' = None, "
    "title: 'str | None' = None, title_source: 'str | None' = None, "
    "git_branch: 'str | None' = None, terminal_context: 'dict[str, Any] | None' = None, "
    "project_id: 'str | None' = None, sandbox_enabled: 'bool | None' = None, "
    "sandbox_policy_hash: 'str | None' = None) -> 'Session | None'",
    "update_approved_tools": "(self, session_id: 'str', tools: 'set[str]') -> 'None'",
    "update_chat_mode": "(self, session_id: 'str', chat_mode: 'str') -> 'None'",
    "update_digest_markdown": "(self, session_id: 'str', digest_markdown: 'str') -> "
    "'Session | None'",
    "update_last_digest_input_hash": "(self, session_id: 'str', hash_value: 'str') -> 'None'",
    "update_last_turn_markdown": "(self, session_id: 'str', last_turn_markdown: 'str') "
    "-> 'Session | None'",
    "update_model": "(self, session_id: 'str', model: 'str') -> 'Session | None'",
    "update_parent_session_id": "(self, session_id: 'str', parent_session_id: 'str') "
    "-> 'Session | None'",
    "update_session_status": "(self, session_id: 'str', status: 'str') -> 'bool'",
    "update_stats": "(self, session_id: 'str', message_count: 'int | None' = None, "
    "turn_count: 'int | None' = None, tool_call_count: 'int | None' = None, "
    "last_assistant_content: 'str | None' = None) -> 'Session | None'",
    "update_status": "(self, session_id: 'str', status: 'str') -> 'Session | None'",
    "update_summary": "(self, session_id: 'str', summary_path: 'str | None' = None, "
    "summary_markdown: 'str | None' = None) -> 'Session | None'",
    "update_terminal_pickup_metadata": "(self, session_id: 'str', "
    "workflow_name: 'str | None' = None, agent_run_id: 'str | None' = None, "
    "context_injected: 'bool | None' = None, original_prompt: 'str | None' = None) "
    "-> 'Session | None'",
    "update_title": "(self, session_id: 'str', title: 'str', *, "
    "title_source: 'str | None' = None) -> 'Session | None'",
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
    return str(signature.replace(parameters=parameters))


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
