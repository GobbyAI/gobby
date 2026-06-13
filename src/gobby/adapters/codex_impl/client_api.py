"""Codex app-server API helpers for CodexAppServerClient."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from gobby.adapters.codex_impl.types import CodexThread, CodexTurn

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

logger = logging.getLogger(__name__)
_CANCELLED_TURN_INTERRUPT_TIMEOUT_SECONDS = 2.0


async def start_thread(
    client: CodexAppServerClient,
    cwd: str | None = None,
    model: str | None = None,
    approval_policy: str | None = None,
    sandbox: str | None = None,
    terminal_context: dict[str, Any] | None = None,
    ephemeral: bool = False,
) -> CodexThread:
    """
    Start a new Codex conversation thread.

    Args:
        cwd: Working directory for the session
        model: Model override (e.g., "gpt-5.1-codex")
        approval_policy: Approval policy ("never", "unlessTrusted", etc.)
        sandbox: Sandbox mode ("workspaceWrite", "readOnly", etc.)
        ephemeral: Whether Codex should avoid persisting transcript/history files

    Returns:
        CodexThread object with thread ID
    """
    params: dict[str, Any] = {}
    if cwd:
        params["cwd"] = cwd
    if model:
        params["model"] = model
    if approval_policy:
        params["approvalPolicy"] = approval_policy
    if sandbox:
        params["sandbox"] = {
            "readOnly": "read-only",
            "workspaceWrite": "workspace-write",
            "dangerFullAccess": "danger-full-access",
        }.get(sandbox, sandbox)
    if ephemeral:
        params["ephemeral"] = True

    pending_context: dict[str, Any] | None = None
    if terminal_context:
        pending_context = {
            "cwd": cwd,
            "terminal_context": dict(terminal_context),
        }
        client._pending_thread_terminal_contexts.append(pending_context)

    try:
        result = await client._send_request("thread/start", params)
    except Exception:
        if pending_context in client._pending_thread_terminal_contexts:
            client._pending_thread_terminal_contexts.remove(pending_context)
        raise
    if pending_context in client._pending_thread_terminal_contexts:
        client._pending_thread_terminal_contexts.remove(pending_context)

    thread_data = result.get("thread", {})
    thread = CodexThread(
        id=thread_data.get("id", ""),
        preview=thread_data.get("preview", ""),
        model_provider=thread_data.get("modelProvider", "openai"),
        created_at=thread_data.get("createdAt", 0),
        path=thread_data.get("path"),
        ephemeral=bool(thread_data.get("ephemeral")),
    )

    client._threads[thread.id] = thread
    result_cwd = result.get("cwd") or thread_data.get("cwd") or cwd
    if isinstance(result_cwd, str) and result_cwd:
        client._thread_cwds[thread.id] = result_cwd
    if terminal_context and thread.id:
        client._thread_terminal_contexts[thread.id] = dict(terminal_context)
    logger.debug(f"Started Codex thread: {thread.id}")
    return thread


async def resume_thread(client: CodexAppServerClient, thread_id: str) -> CodexThread:
    """
    Resume an existing Codex conversation thread.

    Args:
        thread_id: ID of the thread to resume

    Returns:
        CodexThread object
    """
    result = await client._send_request("thread/resume", {"threadId": thread_id})

    thread_data = result.get("thread", {})
    thread = CodexThread(
        id=thread_data.get("id", thread_id),
        preview=thread_data.get("preview", ""),
        model_provider=thread_data.get("modelProvider", "openai"),
        created_at=thread_data.get("createdAt", 0),
        path=thread_data.get("path"),
        ephemeral=bool(thread_data.get("ephemeral")),
    )

    client._threads[thread.id] = thread
    result_cwd = result.get("cwd") or thread_data.get("cwd")
    if isinstance(result_cwd, str) and result_cwd:
        client._thread_cwds[thread.id] = result_cwd
    logger.debug(f"Resumed Codex thread: {thread.id}")
    return thread


async def list_threads(
    client: CodexAppServerClient,
    cursor: str | None = None,
    limit: int = 25,
) -> tuple[list[CodexThread], str | None]:
    """
    List stored Codex threads with pagination.

    Args:
        cursor: Pagination cursor from previous call
        limit: Maximum threads to return

    Returns:
        Tuple of (threads list, next_cursor or None)
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    result = await client._send_request("thread/list", params)

    threads = []
    for item in result.get("data", []):
        thread = CodexThread(
            id=item.get("id", ""),
            preview=item.get("preview", ""),
            model_provider=item.get("modelProvider", "openai"),
            created_at=item.get("createdAt", 0),
            ephemeral=bool(item.get("ephemeral")),
        )
        threads.append(thread)
        if thread.id:
            client._threads[thread.id] = thread
        item_cwd = item.get("cwd")
        if isinstance(item_cwd, str) and item_cwd:
            client._thread_cwds[thread.id] = item_cwd

    next_cursor = result.get("nextCursor")
    return threads, next_cursor


async def archive_thread(client: CodexAppServerClient, thread_id: str) -> None:
    """
    Archive a Codex thread.

    Args:
        thread_id: ID of the thread to archive
    """
    await client._send_request("thread/archive", {"threadId": thread_id})
    client._threads.pop(thread_id, None)
    client._thread_cwds.pop(thread_id, None)
    logger.debug(f"Archived Codex thread: {thread_id}")


async def list_models(
    client: CodexAppServerClient,
    *,
    limit: int = 100,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """List Codex app-server models, following pagination when present."""
    models: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        params: dict[str, Any] = {
            "limit": limit,
            "includeHidden": include_hidden,
        }
        if cursor:
            params["cursor"] = cursor

        result = await client._send_request("model/list", params)
        page = result.get("data", [])
        if isinstance(page, list):
            models.extend(item for item in page if isinstance(item, dict))

        next_cursor_raw = result.get("nextCursor")
        cursor = str(next_cursor_raw) if next_cursor_raw else None
        if not cursor:
            break
        if cursor in seen_cursors:
            logger.warning(
                "Codex model/list returned a repeated cursor (%s); stopping pagination",
                cursor,
            )
            break
        seen_cursors.add(cursor)

    return models


async def start_turn(
    client: CodexAppServerClient,
    thread_id: str,
    prompt: str,
    images: list[str] | None = None,
    context_prefix: str | None = None,
    effort: str | None = None,
    **config_overrides: Any,
) -> CodexTurn:
    """
    Start a new turn (send user input and trigger generation).

    Args:
        thread_id: Thread ID to add turn to
        prompt: User's input text
        images: Optional list of image paths or URLs
        context_prefix: Optional context to prepend to instructions field.
                       Used for injecting session metadata and workflow context.
        effort: Optional reasoning effort override for this and subsequent turns.
        **config_overrides: Optional config overrides (cwd, model, etc.)

    Returns:
        CodexTurn object (initial state, updates via notifications)
    """
    inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

    if images:
        for img in images:
            if img.startswith(("http://", "https://")):
                inputs.append({"type": "image", "url": img})
            else:
                inputs.append({"type": "localImage", "path": img})

    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": inputs,
    }
    client._pending_turn_prompts_by_thread[thread_id] = prompt

    if context_prefix:
        params["instructions"] = context_prefix

    # App-server v2 uses `effort` for per-turn reasoning overrides.
    if effort:
        params["effort"] = effort
    elif "reasoningEffort" in config_overrides and "effort" not in config_overrides:
        params["effort"] = config_overrides.pop("reasoningEffort")

    params.update(config_overrides)
    try:
        result = await client._send_request("turn/start", params)
    except Exception:
        client._pending_turn_prompts_by_thread.pop(thread_id, None)
        raise

    turn_data = result.get("turn", {})
    turn = CodexTurn(
        id=turn_data.get("id", ""),
        thread_id=thread_id,
        status=turn_data.get("status", "inProgress"),
        items=turn_data.get("items", []),
        error=turn_data.get("error"),
    )
    if turn.id:
        client._turn_prompts[turn.id] = prompt

    logger.debug(f"Started turn {turn.id} in thread {thread_id}")
    return turn


async def interrupt_turn(client: CodexAppServerClient, thread_id: str, turn_id: str) -> None:
    """
    Interrupt an in-progress turn.

    Args:
        thread_id: Thread ID containing the turn
        turn_id: Turn ID to interrupt
    """
    await client._send_request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
    logger.debug(f"Interrupted turn {turn_id}")


async def _interrupt_cancelled_turn(
    client: CodexAppServerClient,
    thread_id: str,
    turn_id: str | None,
    turn_completed: asyncio.Event,
) -> None:
    if turn_id is None:
        logger.debug("Codex run_turn cancellation cleanup skipped; turn was not started")
        return
    if turn_completed.is_set():
        logger.debug("Codex run_turn cancellation cleanup skipped; turn %s is terminal", turn_id)
        return

    try:
        await asyncio.wait_for(
            interrupt_turn(client, thread_id, turn_id),
            timeout=_CANCELLED_TURN_INTERRUPT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Codex run_turn cancellation cleanup failed; turn %s did not interrupt within %.1fs",
            turn_id,
            _CANCELLED_TURN_INTERRUPT_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "Codex run_turn cancellation cleanup failed for turn %s",
            turn_id,
            exc_info=True,
        )
    else:
        logger.debug("Codex run_turn cancellation cleanup completed for turn %s", turn_id)


async def run_turn(
    client: CodexAppServerClient,
    thread_id: str,
    prompt: str,
    images: list[str] | None = None,
    **config_overrides: Any,
) -> AsyncIterator[dict[str, Any]]:
    """
    Run a turn and yield streaming events.

    This is the primary method for interacting with Codex. It starts a turn
    and yields all events until completion.

    Args:
        thread_id: Thread ID
        prompt: User's input text
        images: Optional image paths/URLs
        **config_overrides: Config overrides

    Yields:
        Event dicts with "type" and event-specific data

    Example:
        async for event in client.run_turn(thread.id, "Help me refactor"):
            if event["type"] == "item.completed":
                print(event["item"]["text"])
    """
    # Queue to receive notifications
    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    turn_completed = asyncio.Event()
    turn_id: str | None = None

    def on_event(method: str, params: dict[str, Any]) -> None:
        turn = params.get("turn")
        if method == "thread/closed":
            if client._notification_thread_id(params) != thread_id:
                return
            event_queue.put_nowait({"type": method, **params})
            turn_completed.set()
            return

        event_thread_id = params.get("threadId")
        if not isinstance(event_thread_id, str) and isinstance(turn, dict):
            event_thread_id = turn.get("threadId")
        event_turn_id = params.get("turnId")
        if not isinstance(event_turn_id, str) and isinstance(turn, dict):
            event_turn_id = turn.get("id")
        if event_thread_id not in (None, thread_id) or (
            turn_id and event_turn_id not in (None, turn_id)
        ):
            return
        event_queue.put_nowait({"type": method, **params})
        if method in {"turn/completed", "turn/failed"} and event_turn_id in (None, turn_id):
            turn_completed.set()

    def raise_for_terminal_error(event: dict[str, Any]) -> None:
        method = event.get("type")
        if method == "thread/closed" and client._notification_thread_id(event) == thread_id:
            raise RuntimeError(f"Codex thread {thread_id} closed before turn completed")
        if method not in {"turn/completed", "turn/failed"}:
            return

        turn = event.get("turn")
        payload = turn if isinstance(turn, dict) else event
        error = payload.get("error") or event.get("error")
        if error:
            raise RuntimeError(f"Codex turn failed: {error}")
        status = payload.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed"}:
            raise RuntimeError(f"Codex turn failed with status {status}")

    # Register handlers for all turn-related events
    event_methods = [
        "turn/started",
        "turn/completed",
        "turn/failed",
        "thread/closed",
        "item/started",
        "item/completed",
        "item/agentMessage/delta",
    ]

    for method in event_methods:
        client.add_notification_handler(method, on_event)

    try:
        # Start the turn
        turn = await client.start_turn(thread_id, prompt, images=images, **config_overrides)
        turn_id = turn.id or None

        yield {"type": "turn/created", "turn": turn.__dict__}

        # Yield events until turn completes
        while not turn_completed.is_set():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                yield event
                raise_for_terminal_error(event)
            except TimeoutError:
                continue

        # Drain remaining events
        while not event_queue.empty():
            event = event_queue.get_nowait()
            yield event
            raise_for_terminal_error(event)

    except asyncio.CancelledError:
        await _interrupt_cancelled_turn(client, thread_id, turn_id, turn_completed)
        raise
    finally:
        # Unregister handlers
        for method in event_methods:
            client.remove_notification_handler(method, on_event)


async def login_with_api_key(client: CodexAppServerClient, api_key: str) -> dict[str, Any]:
    """
    Authenticate using an OpenAI API key.

    Args:
        api_key: OpenAI API key (sk-...)

    Returns:
        Login result dict
    """
    result = await client._send_request(
        "account/login/start", {"type": "apiKey", "apiKey": api_key}
    )
    logger.debug("Logged in with API key")
    return result


async def get_account_status(client: CodexAppServerClient) -> dict[str, Any]:
    """Get current account/authentication status."""
    return await client._send_request("account/status", {})
