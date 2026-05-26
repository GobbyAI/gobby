# Memory Injection Deduplication Spec

## Problem Statement

Memory injection currently recalls and injects relevant memories on every user prompt. This causes:
- **Repetitive context**: Same memories appear in every response
- **Token waste**: Duplicate memory content consumes context window
- **Poor UX**: Users see the same "remembered" facts repeatedly

## Proposed Solution

Track which memories have been injected per session using workflow state variables. Each memory ID is recorded after injection and filtered from subsequent recalls. Reset tracking on:
- `pre_compact` hook (context is being compressed, memories need re-injection)
- `/clear` command (fresh start)

## Implementation

### 1. Modify `memory_recall_relevant` Action

**File:** `src/gobby/workflows/memory_actions.py`

Add state parameter and deduplication logic:

```python
async def memory_recall_relevant(
    memory_manager: Any,
    session_manager: Any,
    session_id: str,
    prompt_text: str | None = None,
    project_id: str | None = None,
    limit: int = 5,
    min_importance: float = 0.3,
    state: Any = None,  # NEW: WorkflowState for tracking
) -> dict[str, Any] | None:
    # ... existing validation ...

    # Get previously injected memory IDs from state
    injected_ids: set[str] = set()
    if state and hasattr(state, 'variables') and state.variables:
        injected_ids = set(state.variables.get("injected_memory_ids", []))

    # Recall memories
    memories = memory_manager.recall(...)

    # Filter out already-injected memories
    new_memories = [m for m in memories if m.id not in injected_ids]

    if not new_memories:
        logger.debug("memory_recall_relevant: All memories already injected this session")
        return {"injected": False, "count": 0, "filtered": len(memories)}

    # Update tracking with newly injected IDs
    new_injected_ids = injected_ids | {m.id for m in new_memories}
    if state and hasattr(state, 'variables'):
        if state.variables is None:
            state.variables = {}
        state.variables["injected_memory_ids"] = list(new_injected_ids)

    # Build context from new memories only
    memory_context = build_memory_context(new_memories)

    return {
        "inject_context": memory_context,
        "injected": True,
        "count": len(new_memories),
        "filtered": len(memories) - len(new_memories),
    }
```

### 2. Add Reset Action

**File:** `src/gobby/workflows/memory_actions.py`

```python
async def reset_memory_injection_tracking(state: Any = None) -> dict[str, Any]:
    """Clear the per-session injected memory tracking.

    Called on pre_compact to allow re-injection after context loss.
    """
    old_count = 0
    if state and hasattr(state, 'variables') and state.variables:
        old_count = len(state.variables.get("injected_memory_ids", []))
        state.variables["injected_memory_ids"] = []

    logger.info(f"reset_memory_injection_tracking: Cleared {old_count} tracked memories")
    return {
        "memory_tracking_reset": True,
        "previous_count": old_count,
    }
```

### 3. Register Action in ActionExecutor

**File:** `src/gobby/workflows/actions.py`

Add handler mapping in `_ACTION_MAP`:
```python
"reset_memory_injection_tracking": self._handle_reset_memory_injection_tracking,
```

Add handler method:
```python
async def _handle_reset_memory_injection_tracking(
    self, context: ActionContext, **kwargs: Any
) -> dict[str, Any] | None:
    """Reset memory injection tracking for the session."""
    return await reset_memory_injection_tracking(state=context.state)
```

Update `_handle_memory_recall_relevant` to pass state:
```python
async def _handle_memory_recall_relevant(
    self, context: ActionContext, **kwargs: Any
) -> dict[str, Any] | None:
    # ... existing code ...
    return await memory_recall_relevant(
        memory_manager=context.memory_manager,
        session_manager=context.session_manager,
        session_id=context.session_id,
        prompt_text=prompt_text,
        project_id=kwargs.get("project_id"),
        limit=kwargs.get("limit", 5),
        min_importance=kwargs.get("min_importance", 0.3),
        state=context.state,  # NEW: pass state for tracking
    )
```

### 4. Update Workflow YAML

**Files:**
- `src/gobby/install/shared/workflows/lifecycle/session-lifecycle.yaml`
- `~/.gobby/workflows/lifecycle/session-lifecycle.yaml`

Add reset action to `on_pre_compact`:
```yaml
on_pre_compact:
  # Reset memory injection tracking before compaction
  # This allows re-injection after context is compressed
  - action: reset_memory_injection_tracking

  # Extract structured context before compaction
  - action: extract_handoff_context
  # ... rest of existing actions ...
```

## Files to Modify

### Core Implementation

| File | Changes |
|------|---------|
| `src/gobby/workflows/memory_actions.py` | Add `state` param to `memory_recall_relevant`, implement dedup logic, add `reset_memory_injection_tracking` function |
| `src/gobby/workflows/actions.py` | Register `reset_memory_injection_tracking` handler, pass `state` to `memory_recall_relevant` |

### Workflow Configuration

| File | Changes |
|------|---------|
| `src/gobby/install/shared/workflows/lifecycle/session-lifecycle.yaml` | Add `reset_memory_injection_tracking` to `on_pre_compact` |
| `~/.gobby/workflows/lifecycle/session-lifecycle.yaml` | Same (local copy) |

## State Variable

| Variable | Type | Purpose |
|----------|------|---------|
| `injected_memory_ids` | `list[str]` | UUIDs of memories already injected this session |

The variable is stored in `WorkflowState.variables` which persists to PostgreSQL via the `workflow_state` table.

## Design Decisions

1. **Session-scoped tracking**: Uses workflow state variables tied to session, not global state
2. **Reset on compact**: Memories may be relevant after context compression loses earlier context
3. **Filter after recall**: Recall still uses semantic search, filtering happens on result set
4. **Additive tracking**: Only adds IDs, never removes (except on reset)
5. **Graceful fallback**: Works without state param for backwards compatibility

## Verification Plan

1. Start fresh session, send prompt that matches memories → memories injected
2. Send another prompt that matches same memories → should NOT inject again
3. Run `/compact` → tracking should reset
4. Send prompt again → memories should inject (fresh start)
5. Check logs: `memory_recall_relevant: All memories already injected this session` on dedupe
6. Check logs: `reset_memory_injection_tracking: Cleared N tracked memories` on reset

## Implementation Order

1. Update `memory_recall_relevant` signature to accept `state` param
2. Add deduplication logic with `injected_memory_ids` tracking
3. Add `reset_memory_injection_tracking` function
4. Register handler in `actions.py`
5. Update `_handle_memory_recall_relevant` to pass state
6. Update workflow YAML with reset action
7. Copy updated YAML to global location
8. Restart daemon and test
