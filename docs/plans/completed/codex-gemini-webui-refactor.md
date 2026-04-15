# Plan: Codex/Gemini Web UI Backend Refactor

**Task:** #11653

## Summary

Refactor web chat provider backends from per-conversation subprocesses to shared, long-lived provider backends.

Target architecture:
- one shared Codex backend per daemon
- one shared Gemini backend per daemon
- lightweight per-conversation wrappers in web chat
- unchanged frontend WebSocket contract

Rollout:
- Phase 1: move Codex web chat to shared backend
- Phase 2: move Gemini web chat to shared backend behind the same abstraction

## Current Problems

- Web chat spawns `codex app-server` per conversation in `CodexCLIChatSession`
- Web chat spawns `gemini --acp` per conversation in `GeminiACPClient`
- Codex protocol logic is duplicated between the shared `CodexAppServerClient` path and the web-chat path
- Gemini ACP parsing is pinned to an older stream shape and drops modern `session/update` events
- Session bugs are easier to introduce because subprocess lifecycle, session routing, and protocol state are mixed together

## Target Design

Add a provider backend manager layer for web chat.

Responsibilities:
- own provider subprocess lifecycle
- create/resume provider-native conversations
- route streamed provider events to the correct web-chat conversation
- expose interrupt and model-switch operations
- keep provider protocol handling in one place

Web chat session wrappers should keep only conversation-local state:
- `conversation_id`
- provider-native session/thread ID
- active turn ID when applicable
- selected model
- project path
- pending approval / plan state

They should no longer own subprocesses directly.

## Phase 1: Codex

Use the existing shared `CodexAppServerClient` as the single Codex web-chat backend.

Changes:
- add a Codex web-chat backend manager on top of `CodexAppServerClient`
- replace per-conversation `CodexCLIChatSession` subprocess ownership with thread/turn wrapper logic
- route all `thread/start`, `thread/resume`, `turn/start`, and `turn/interrupt` calls through the shared client
- track active `turnId` per conversation wrapper
- remove duplicated JSON-RPC handling from the web-chat Codex path
- make the daemon-owned Codex app-server path the only Codex web-chat backend path

Required behavior:
- multiple web conversations share one app-server process
- each web conversation maps to one Codex thread
- reconnect/resume preserves conversation-to-thread mapping
- second and later turns in the same conversation work reliably
- interrupt uses both `threadId` and `turnId`

## Phase 2: Gemini

Add a shared Gemini ACP backend manager using one long-lived `gemini --acp` subprocess.

Changes:
- add a Gemini provider manager with the same internal interface used by Codex
- normalize modern ACP `session/update` notifications in the provider layer
- map:
  - `agent_message_chunk` -> assistant text chunk
  - `agent_thought_chunk` -> thinking chunk
  - `available_commands_update` -> ignored bookkeeping event
- replace per-conversation Gemini subprocess ownership with session wrapper logic
- keep Gemini-specific approval and plan-mode state in the wrapper, not the provider process layer

Required behavior:
- multiple web conversations share one ACP backend
- each web conversation maps to one ACP session
- session resume/load preserves conversation mapping
- backend failures produce explicit chat errors, never silent spinner states

## Internal Interfaces

Add a shared provider backend interface used by web chat:

```python
class WebChatProviderBackend(Protocol):
    provider: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def create_or_resume_conversation(...) -> ProviderConversationHandle: ...
    async def send_message(...) -> AsyncIterator[ProviderEvent]: ...
    async def interrupt(...) -> None: ...
    async def switch_model(...) -> None: ...
```

Implementation rules:
- provider managers own subprocesses
- conversation handles own provider-native conversation identity only
- WebSocket chat code talks to wrappers, not raw provider subprocess clients
- frontend message types stay unchanged

## Files

Primary changes:
- `src/gobby/servers/websocket/chat/_session.py`
- `src/gobby/servers/websocket/chat/_messaging.py`
- `src/gobby/adapters/codex_impl/client.py`
- `src/gobby/adapters/gemini_acp_client.py`
- `src/gobby/servers/codex_cli_chat_session.py`
- `src/gobby/servers/gemini_cli_chat_session.py`
- `src/gobby/runner_init.py`
- `src/gobby/servers/app_factory.py`

New backend manager module(s) should sit under `src/gobby/servers/` or `src/gobby/servers/websocket/chat/` and become the single integration point for provider-backed web chat.

## Tests

Codex:
- one shared app-server process for multiple web conversations
- per-conversation thread mapping
- per-turn `turnId` tracking
- second turn succeeds in the same conversation
- interrupt uses correct thread/turn identity
- reconnect/resume preserves conversation mapping

Gemini:
- one shared ACP process for multiple web conversations
- `session/update` normalization for current Gemini CLI event shapes
- text and thinking streams route correctly
- bookkeeping updates do not surface to users
- provider failure surfaces `chat_error`

Web chat integration:
- frontend WebSocket contract unchanged
- `conversation_id_changed`, `model_switched`, approvals, and plan flows still work
- provider restart/disconnect does not orphan conversation state silently

## Assumptions

- Shared provider backends are the long-term architecture.
- Codex migrates first because the repo already has a reusable shared app-server client.
- Gemini must adopt the same backend-manager abstraction even if the first implementation serializes some operations internally.
- The current per-conversation subprocess model is transitional and should be removed from the web-chat path rather than hardened further.
