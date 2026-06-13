"""Notification registration and enrichment helpers for CodexAppServerClient."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.adapters.codex_impl.types import NotificationHandler

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient


def add_notification_handler(
    client: CodexAppServerClient,
    method: str,
    handler: NotificationHandler,
) -> None:
    """
    Register a handler for a specific notification method.

    Args:
        method: Notification method name (e.g., "turn/started", "item/completed")
        handler: Callback function(method, params)
    """
    if method not in client._notification_handlers:
        client._notification_handlers[method] = []
    client._notification_handlers[method].append(handler)


def remove_notification_handler(
    client: CodexAppServerClient,
    method: str,
    handler: NotificationHandler,
) -> None:
    """Remove a notification handler."""
    if method in client._notification_handlers:
        client._notification_handlers[method] = [
            h for h in client._notification_handlers[method] if h != handler
        ]


def register_approval_handler(client: CodexAppServerClient, handler: Any | None) -> None:
    """Register an async handler for incoming approval requests.

    The handler receives JSON-RPC requests from Codex (messages with both
    id and method) and returns a decision dict.

    Args:
        handler: Async callback with signature:
            async def handler(method: str, params: dict) -> dict
            Returns {"decision": "accept"} or {"decision": "decline"}.
            Pass None to clear the handler.
    """
    client._approval_handler = handler


def enrich_notification(
    client: CodexAppServerClient,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Attach best-effort client-side context to app-server notifications."""
    thread = params.get("thread")
    thread_id = params.get("threadId")
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = thread.get("id") if isinstance(thread, dict) else None

    cwd = params.get("cwd")
    if not isinstance(cwd, str) and isinstance(thread, dict):
        cwd = thread.get("cwd")
    if isinstance(thread_id, str) and thread_id:
        if isinstance(cwd, str) and cwd:
            client._thread_cwds[thread_id] = cwd
        else:
            cached_cwd = client._thread_cwds.get(thread_id)
            if cached_cwd:
                enriched = dict(params)
                enriched["cwd"] = cached_cwd
                params = enriched

    if method == "thread/started" and isinstance(thread_id, str) and thread_id:
        terminal_context = client._thread_terminal_contexts.get(thread_id)
        if terminal_context is None:
            for idx, pending in enumerate(client._pending_thread_terminal_contexts):
                pending_cwd = pending.get("cwd")
                if not pending_cwd or not cwd or pending_cwd == cwd:
                    terminal_context = pending.get("terminal_context")
                    client._pending_thread_terminal_contexts.pop(idx)
                    break
        if isinstance(terminal_context, dict) and terminal_context:
            client._thread_terminal_contexts[thread_id] = dict(terminal_context)
            if not isinstance(params.get("terminal_context"), dict):
                enriched = dict(params)
                enriched["terminal_context"] = dict(terminal_context)
                params = enriched

    if method == "turn/started":
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(turn_id, str) and turn_id:
                prompt = client._turn_prompts.get(turn_id)
            if not prompt:
                thread_id = params.get("threadId")
                if isinstance(thread_id, str) and thread_id:
                    prompt = client._pending_turn_prompts_by_thread.get(thread_id)
            if prompt:
                enriched = dict(params)
                enriched["prompt"] = prompt
                params = enriched

        thread_id = params.get("threadId")
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str) and turn_id and isinstance(thread_id, str) and thread_id:
            client._pending_turn_prompts_by_thread.pop(thread_id, None)
        return params

    if method == "turn/completed":
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str) and turn_id:
            client._turn_prompts.pop(turn_id, None)
        return params

    if method in ("thread/archive", "thread/closed") and isinstance(thread_id, str) and thread_id:
        client._thread_cwds.pop(thread_id, None)
        client._thread_terminal_contexts.pop(thread_id, None)

    return params


def notification_thread_id(params: dict[str, Any]) -> str | None:
    thread_id = params.get("threadId")
    if isinstance(thread_id, str):
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        thread_id = thread.get("id")
        if isinstance(thread_id, str):
            return thread_id
    return None
