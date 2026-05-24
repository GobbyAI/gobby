# Doom Loop Detection + Shadow Git Checkpoints

## Context

Gobby already has substantial stuck detection infrastructure (`StuckDetector`, `PromptDetector`, `IdleDetector`, `StallClassifier`) but has three critical gaps:

1. **Loop prompts are auto-dismissed indefinitely** — `check_loop_prompts()` sends "y\n" every time with no counting or escalation. An agent stuck in a genuine loop will be dismissed forever.
2. **Failed agents' tasks reset to "open" unconditionally** — `_recover_task_from_failed_agent()` always resets to open, enabling infinite re-dispatch cycles where the same task keeps failing.
3. **No way to preserve agent work before killing it** — when a doom-looping agent is terminated, all uncommitted changes are lost.

These two features compose: detect the loop, checkpoint the work, kill the agent, block re-dispatch.

## Implementation

### 1. DB Migration v184 (`src/gobby/storage/migrations.py`)

Single migration adding two tables. Bump `BASELINE_VERSION` to 184 and add tables to `baseline_schema.sql`.

```sql
-- Loop event tracking for doom loop detection
CREATE TABLE IF NOT EXISTS loop_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    session_id TEXT,
    task_id TEXT,
    event_type TEXT NOT NULL,  -- 'loop_prompt', 'agent_failure', 'dispatch'
    details TEXT,              -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_loop_events_run ON loop_events(run_id, event_type);
CREATE INDEX idx_loop_events_task ON loop_events(task_id, event_type, created_at DESC);

-- Shadow git checkpoints
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ref_name TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    parent_sha TEXT NOT NULL,
    files_changed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT 'auto-checkpoint',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, created_at DESC);
```

**Files:** `src/gobby/storage/migrations.py`, `src/gobby/storage/baseline_schema.sql`

---

### 2. Loop Tracker (`src/gobby/agents/loop_tracker.py`) — NEW ~200 lines

DB-backed loop event tracker. Survives daemon restarts.

```python
class LoopTracker:
    def __init__(self, db: HubDatabase): ...
    
    def record_loop_prompt(self, run_id: str, session_id: str, task_id: str | None) -> int:
        """Record loop prompt dismissal. Returns count for this run."""
    
    def record_agent_failure(self, run_id: str, task_id: str, error: str) -> None:
        """Record agent failure for cross-agent tracking."""
    
    def record_dispatch(self, task_id: str, run_id: str) -> None:
        """Record task dispatch."""
    
    def check_loop_prompt_escalation(self, run_id: str, threshold: int = 3) -> bool:
        """True if loop prompts exceed threshold — agent should be killed."""
    
    def should_block_dispatch(self, task_id: str, failure_threshold: int = 3) -> tuple[bool, str | None]:
        """Check if task has failed too many times across different agents.
        Returns (should_block, reason)."""
    
    def clear_task(self, task_id: str) -> None:
        """Clear loop data for a task (manual intervention reset)."""
```

---

### 3. PromptDetector Enhancement (`src/gobby/agents/prompt_detector.py`) — MODIFY ~15 lines

Add in-memory loop prompt counting (fast path; LoopTracker is the durable store):

```python
def __init__(self) -> None:
    self._dismissed: set[str] = set()
    self._loop_counts: dict[str, int] = {}  # NEW

def record_loop_dismiss(self, run_id: str) -> int:  # NEW
    self._loop_counts[run_id] = self._loop_counts.get(run_id, 0) + 1
    return self._loop_counts[run_id]

def clear(self, run_id: str) -> None:
    self._dismissed.discard(run_id)
    self._loop_counts.pop(run_id, None)  # MODIFIED
```

---

### 4. Lifecycle Monitor Integration (`src/gobby/agents/lifecycle_monitor.py`) — MODIFY ~50 lines

**`__init__`**: Accept optional `LoopTracker` and `CheckpointManager` params.

**`check_loop_prompts()`**: After dismissing, record in LoopTracker. If count exceeds threshold (3), checkpoint work then kill agent instead of dismissing:

```python
if pane_output and self._prompt_detector.detect_loop_prompt(pane_output):
    count = self._prompt_detector.record_loop_dismiss(run.id)
    if self._loop_tracker:
        self._loop_tracker.record_loop_prompt(run.id, session_id, run.task_id)
    
    if count >= 3:
        # Escalate: checkpoint then kill
        if self._checkpoint_manager and run.task_id:
            await self._checkpoint_manager.auto_checkpoint(cwd, run.task_id, session_id)
        await self._kill_idle_agent(run, reason="doom loop: dismissed loop prompt 3+ times")
    else:
        await self._tmux.send_keys(tmux_name, PromptDetector.LOOP_DISMISS_KEYS)
```

**`_recover_task_from_failed_agent()`**: Before resetting task to "open", check LoopTracker. If blocked, set task to "blocked" instead:

```python
if self._loop_tracker:
    self._loop_tracker.record_agent_failure(run_id, task_id, db_run.error or "unknown")
    blocked, reason = self._loop_tracker.should_block_dispatch(task_id)
    if blocked:
        await asyncio.to_thread(
            self._task_manager.update_task, task_id, status="blocked", assignee=None
        )
        logger.warning(f"Task {task_ref} blocked from re-dispatch: {reason}")
        return
```

---

### 5. Checkpoint Storage (`src/gobby/storage/checkpoints.py`) — NEW ~120 lines

Follows existing storage pattern (see `task_affected_files.py`):

```python
@dataclass
class Checkpoint:
    id: str
    task_id: str
    session_id: str
    ref_name: str
    commit_sha: str
    parent_sha: str
    files_changed: int
    message: str
    created_at: str

class LocalCheckpointManager:
    def __init__(self, db: HubDatabase): ...
    def create(self, checkpoint: Checkpoint) -> Checkpoint: ...
    def get(self, checkpoint_id: str) -> Checkpoint | None: ...
    def list_for_task(self, task_id: str) -> list[Checkpoint]: ...
    def delete(self, checkpoint_id: str) -> bool: ...
    def delete_old(self, task_id: str, keep_latest: int) -> int: ...
```

---

### 6. Checkpoint Manager (`src/gobby/worktrees/checkpoints.py`) — NEW ~350 lines

Uses `git commit-tree` + `git update-ref` to create commits on hidden refs (`refs/gobby/ckpt/...`) without touching the branch.

```python
class CheckpointManager:
    def __init__(self, checkpoint_storage: LocalCheckpointManager): ...
    
    async def auto_checkpoint(self, cwd: str | Path, task_id: str, session_id: str) -> Checkpoint | None:
        """Create checkpoint if there are uncommitted changes.
        1. Check `git diff --stat` for changes
        2. `git add -A` (stage everything)
        3. `git write-tree` (get tree SHA)
        4. `git commit-tree <tree> -p HEAD -m <msg>` (create detached commit)
        5. `git update-ref refs/gobby/ckpt/<task_id>/<seq> <commit>` (store ref)
        6. `git reset HEAD` (unstage, leave working tree untouched)
        7. Record in DB
        Returns None if no changes."""
    
    async def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        """List all checkpoints for a task."""
    
    async def diff_from_checkpoint(self, cwd: str | Path, checkpoint_id: str) -> str:
        """Diff between checkpoint and current working tree."""
    
    async def restore_checkpoint(self, cwd: str | Path, checkpoint_id: str) -> bool:
        """Restore working tree to checkpoint state. DEFERRED to v0.4."""
        raise NotImplementedError("Checkpoint restore coming in v0.4")
    
    async def cleanup_checkpoints(self, task_id: str, keep_latest: int = 1) -> int:
        """Delete old checkpoint refs, keeping N most recent."""
```

---

### 7. Factory Wiring (`src/gobby/hooks/factory.py`) — MODIFY ~15 lines

Instantiate `LoopTracker` and `CheckpointManager` in `HookManagerFactory.create()`, pass to `AgentLifecycleMonitor`.

---

## Build Order

| Step | What | Type | ~Lines |
|------|------|------|--------|
| 1 | Migration v184 + baseline_schema.sql | modify | 40 |
| 2 | `agents/loop_tracker.py` | new | 200 |
| 3 | `storage/checkpoints.py` | new | 120 |
| 4 | `worktrees/checkpoints.py` | new | 350 |
| 5 | `agents/prompt_detector.py` | modify | 15 |
| 6 | `agents/lifecycle_monitor.py` | modify | 50 |
| 7 | `hooks/factory.py` | modify | 15 |

## Deferred to v0.4

- Checkpoint restore (complex: dirty working tree handling, partial restores)
- MCP tool exposure for manual checkpoint/restore
- Conductor-level dispatch tracking (conductor is LLM-driven, somewhat self-correcting)
- Alternating tool sequence detection in `StuckDetector` (existing detection catches most cases)
- Transcript-level LLM loop analysis

## Verification

1. **Unit tests**: `tests/agents/test_loop_tracker.py`, `tests/worktrees/test_checkpoints.py`, `tests/storage/test_checkpoints.py`
2. **Manual test**: Spawn an agent on a task that will loop, verify:
   - Loop prompt count increments in logs
   - After 3 dismissals, agent is killed (not dismissed)
   - Checkpoint ref exists: `git for-each-ref refs/gobby/ckpt/`
   - Task status is "blocked" after 3 agent failures
3. **Migration test**: Fresh DB gets tables from baseline; existing DB gets v184 migration
