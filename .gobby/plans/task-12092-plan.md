# Consolidate SessionManager and LocalSessionManager (Gobby #12092)

## Overview

Gobby has two overlapping session-manager classes: `SessionManager` (service wrapper at `src/gobby/sessions/manager.py`, 511 LOC, 12 public methods) and `LocalSessionManager` (storage layer at `src/gobby/storage/sessions.py`, 1287 LOC — over the 1000-line monolith limit per CLAUDE.md principle #2). They have overlapping method names with diverging return types and diverging failure semantics (`register_session → str` with hook-friendly UUID fallback on DB failure vs `register → Session` with persisted-or-raise semantics; `get_session → dict` vs `get → Session`). This shape is what made bug #12082 trivial to introduce: `mcp_proxy/services/tool_proxy.py` called `.get()` on `_session_manager` (the service wrapper, which has no `.get` method), the `AttributeError` was swallowed at DEBUG level, and source silently defaulted to CODEX, breaking pipeline-originated MCP call validation. The narrow bug was fixed in commit `c724cf7e5`; this plan removes the underlying class duplication so the category of bug cannot recur.

The outcome is a single public `SessionManager` class that owns both storage and the thin service-layer value-add (in-memory caches, a hook-friendly `register_session()` wrapper with UUID fallback, and a `find_parent_session()` polling helper). The 1287-LOC storage file is split into a `storage/sessions/` package of mixin files, each under 400 LOC, with no task boundary leaving any file above 1000 LOC.

## Constraints

- **Preserve the fabricated-UUID fallback** on registration failure — but **only** on the hook-friendly entry point. Two hook callers (`hooks/session_lookup.py:191`, `hooks/event_handlers/_session_start.py` at lines 347, 400, 413, 680, 724) depend on `register_session` returning a string even when the DB insert raises, so hook dispatch keeps moving. This contract is CLAUDE.md-level and must be covered by a regression test (today it has none).
- **Preserve `register()` as persisted-or-raise.** Non-hook callers — `workflows/pipeline_executor.py:338-359` (wraps in try/except and falls back to caller session on failure), `servers/routes/sessions/core.py:298-333` (turns failures into HTTP 500) — rely on `register()` raising on storage failure. Absorbing the UUID fallback into `register()` would silently break both paths. `register_session()` stays as a **first-class method with distinct documented semantics**, not a compat shim.
- **No file may import both class names** after the refactor. This is the explicit validation criterion and prevents regression to the dual-class shape. Enforced by a meta test.
- **No file may exceed 1000 LOC at any task boundary.** CLAUDE.md principle #2 applies to every commit, not just the final one. Every task in Phase 1 ends with `_manager.py` below 1000 LOC.
- **Zero external behavior change.** Pipelines, hooks, agents, and session handoffs must behave identically before and after. The refactor is mechanical; the goal is internal shape, not features.
- **TmuxSessionManager and ChildSessionManager are out of scope.** Both (`src/gobby/agents/tmux/session_manager.py:36`, `src/gobby/agents/session.py:62`) are independent classes with zero inheritance from `SessionManager`. Only `ChildSessionManager`'s `session_storage: LocalSessionManager` parameter annotation changes (to `SessionManager`) during call-site migration — no behavior change.
- **Cache invalidation semantics are frozen.** The `_session_mapping` / `_session_metadata` caches stay best-effort (miss falls through to `find_by_external_id`). Do not add new invalidation calls from storage mutations in this refactor; that's a separate follow-up.
- **Full pytest suite is never run.** Per CLAUDE.md, each task's verification uses targeted pytest paths — never `uv run pytest` without a path filter. The final verification matrix is bounded to the touched subsystems.

## Phase 1: Split storage/sessions.py into mixin package

**Goal**: Break the 1287-LOC monolith into a `storage/sessions/` package of mixin files, each under 400 LOC, with zero external behavior change. **Every task in this phase ends with `_manager.py` below 1000 LOC.** Everything still imports `from gobby.storage.sessions import LocalSessionManager` and gets the same class back.

### 1.1 Create sessions package with constants, bootstrap, and CRUD mixin [category: refactor]

Target: `src/gobby/storage/sessions/__init__.py`, `src/gobby/storage/sessions/_constants.py`, `src/gobby/storage/sessions/_bootstrap.py`, `src/gobby/storage/sessions/_crud.py`, `src/gobby/storage/sessions/_manager.py`

Replace the file `src/gobby/storage/sessions.py` with a package at `src/gobby/storage/sessions/`. This task extracts enough content to leave the transitional `_manager.py` below 1000 LOC: constants (ranges 1-68), bootstrap (81-90), CRUD (92-322), and delete (1002-1005) come out in a single commit. Original file is 1287 LOC; after extraction ~313 lines are removed. The hard gate is the `wc -l` check below, not the estimate — if imports and the composition stanza push `_manager.py` close to 1000 LOC, shift the `delete()` method or one additional field-update method (e.g. `touch`) into this commit to keep headroom.

**Files to create:**

- `src/gobby/storage/sessions/__init__.py`

  ```python
  """Session storage package (mixin-split from storage/sessions.py)."""
  from __future__ import annotations

  from gobby.storage.sessions._constants import (
      SYSTEM_SESSION_ID,
      SYSTEM_SESSION_EXTERNAL_ID,
      SYSTEM_SESSION_MACHINE_ID,
      SYSTEM_SESSION_SOURCE,
      ensure_system_session,
  )
  from gobby.storage.sessions._manager import LocalSessionManager

  __all__ = [
      "LocalSessionManager",
      "SYSTEM_SESSION_ID",
      "SYSTEM_SESSION_EXTERNAL_ID",
      "SYSTEM_SESSION_MACHINE_ID",
      "SYSTEM_SESSION_SOURCE",
      "ensure_system_session",
  ]
  ```

- `src/gobby/storage/sessions/_constants.py` — move current `src/gobby/storage/sessions.py:1-68` verbatim (SYSTEM_SESSION_* constants + `ensure_system_session` function). ~70 LOC.

- `src/gobby/storage/sessions/_bootstrap.py` — `_BootstrapMixin` with `__init__` plumbing for title listeners (currently at `storage/sessions.py:81-90`). ~50 LOC:

  ```python
  from typing import Callable

  class _BootstrapMixin:
      """Bootstrap/listener state for LocalSessionManager."""
      _title_listeners: list[Callable[[str, str], None]]

      def register_title_listener(self, listener: Callable[[str, str], None]) -> None:
          self._title_listeners.append(listener)

      def unregister_title_listener(self, listener: Callable[[str, str], None]) -> None:
          if listener in self._title_listeners:
              self._title_listeners.remove(listener)
  ```

- `src/gobby/storage/sessions/_crud.py` — `_CrudMixin` (~250 LOC) with these methods lifted verbatim from original `storage/sessions.py`:
  - `register` (lines 92-252 — 160-LOC retry loop for seq_num collisions)
  - `create_web_chat_session` (254-305)
  - `get` (307-310)
  - `resolve_session_reference` (312-322)
  - `delete` (1002-1005)

- `src/gobby/storage/sessions/_manager.py` — composed class, ~920 LOC (transitional):

  ```python
  from gobby.storage.sessions._bootstrap import _BootstrapMixin
  from gobby.storage.sessions._crud import _CrudMixin


  class LocalSessionManager(_BootstrapMixin, _CrudMixin):
      def __init__(self, db: DatabaseProtocol) -> None:
          self.db = db
          self._title_listeners: list[Callable[[str, str], None]] = []

      # Remaining methods (discovery, field-update, bulk-update, query,
      # lifecycle-delegate, transcript, usage, terminal) stay here as a single
      # class body for now. Extracted progressively in tasks 1.2–1.4.
  ```

**Files to delete at end of this task:** `src/gobby/storage/sessions.py` (replaced by the package).

**Line-count invariant at task boundary:** `wc -l src/gobby/storage/sessions/_manager.py` must be under 1000. Verify in the commit.

**Behavior contract:** `from gobby.storage.sessions import LocalSessionManager`, `from gobby.storage.sessions import ensure_system_session`, and `from gobby.storage import LocalSessionManager` (via `storage/__init__.py`'s lazy `_EXPORTS`) must all continue to work with identical semantics.

**Verification (bounded):**
```bash
wc -l src/gobby/storage/sessions/*.py  # every file < 1000 LOC
uv run pytest tests/storage/ tests/sessions/ -x
uv run mypy src/gobby/storage/sessions
uv run ruff check src/gobby/storage/sessions
```

### 1.2 Extract Discovery and Field-update mixins [category: refactor] (depends: 1.1)

Target: `src/gobby/storage/sessions/_discovery.py`, `src/gobby/storage/sessions/_field_update.py`, `src/gobby/storage/sessions/_manager.py`

Pull the discovery finders (~230 LOC) and all single-field updates (~210 LOC) out of `_manager.py`. After this task, `_manager.py` drops to ~480 LOC.

**`src/gobby/storage/sessions/_discovery.py`** — `_DiscoveryMixin` with:
- `find_by_external_id` (original 324-361)
- `find_active_by_external_id` (363-389)
- `find_by_external_id_any_project` (391-424)
- `find_by_external_id_all_sources` (426-447)
- `find_parent` (449-485)
- `find_children` (487-505)
- `is_ancestor` (507-536)

**`src/gobby/storage/sessions/_field_update.py`** — `_FieldUpdateMixin` with:
- `update_status` (538-545)
- `touch` (547-558)
- `mark_had_edits` (560-567)
- `clear_had_edits` (569-574)
- `update_chat_mode` (578-587)
- `update_approved_tools` (589-597)
- `update_title` (599-652) — includes title-listener fan-out and tmux-rename scheduling
- `update_model` (654-662)
- `update_summary` (664-682)
- `update_digest_markdown` (684-696)
- `update_last_turn_markdown` (698-710)
- `update_last_digest_input_hash` (712-723)
- `update_parent_session_id` (725-734)

Compose in `_manager.py`:

```python
class LocalSessionManager(
    _BootstrapMixin,
    _CrudMixin,
    _DiscoveryMixin,
    _FieldUpdateMixin,
):
    ...
```

**Shared state typing (TYPE_CHECKING Protocol)** — add to `_manager.py` so mixins can reference `self.db` without circular imports:

```python
if TYPE_CHECKING:
    from typing import Protocol

    class _ManagerState(Protocol):
        db: DatabaseProtocol
        _title_listeners: list[Callable[[str, str], None]]
```

**Line-count invariant at task boundary:** `_manager.py` ~480 LOC; every mixin file under 400 LOC.

**Verification:**
```bash
wc -l src/gobby/storage/sessions/*.py
uv run pytest tests/storage/ tests/sessions/ -x
uv run mypy src/gobby/storage/sessions
```

### 1.3 Extract Bulk-update and Query mixins [category: refactor] (depends: 1.2)

Target: `src/gobby/storage/sessions/_bulk_update.py`, `src/gobby/storage/sessions/_query.py`, `src/gobby/storage/sessions/_manager.py`

**`_bulk_update.py`** — `_BulkUpdateMixin` (~140 LOC):
- `update` (original 738-829, 91 LOC — atomic multi-field update)
- `update_stats` (831-866)
- `recalculate_stats` (868-888)

**`_query.py`** — `_QueryMixin` (~130 LOC):
- `list` (890-946)
- `count` (948-990)
- `count_by_status` (992-1000)

After this task, `_manager.py` holds ~210 LOC (just composition + `__init__` + remaining lifecycle/transcript/usage/terminal methods that move in 1.4).

**Line-count invariant at task boundary:** every file under 400 LOC.

**Verification:**
```bash
wc -l src/gobby/storage/sessions/*.py
uv run pytest tests/storage/ -x
uv run mypy src/gobby/storage/sessions
```

### 1.4 Extract Lifecycle-delegate, Transcript, Usage, Terminal mixins [category: refactor] (depends: 1.3)

Target: `src/gobby/storage/sessions/_lifecycle_delegate.py`, `_transcript.py`, `_usage.py`, `_terminal.py`, `_manager.py`

**`_lifecycle_delegate.py`** — `_LifecycleDelegateMixin` (~40 LOC): the 5 thin delegates to `storage/session_lifecycle.py` at original lines 1007-1037 (`expire_stale_sessions`, `expire_orphaned_handoff_sessions`, `pause_inactive_active_sessions`, `expire_empty_sessions`, `prune_empty_sessions`).

**`_transcript.py`** — `_TranscriptMixin` (~65 LOC): `get_pending_transcript_sessions` (1039-1062), `mark_transcript_processed` (1064-1079), `reset_transcript_processed` (1081-1096).

**`_usage.py`** — `_UsageMixin` (~90 LOC): `update_usage` (1098-1138), `add_usage_delta` (1140-1180).

**`_terminal.py`** — `_TerminalMixin` (~120 LOC): `get_sessions_since` (1182-1219), `update_terminal_pickup_metadata` (1221-1262), `record_skills_used` (1264-1287).

Final compose:

```python
class LocalSessionManager(
    _BootstrapMixin,
    _CrudMixin,
    _DiscoveryMixin,
    _FieldUpdateMixin,
    _BulkUpdateMixin,
    _QueryMixin,
    _LifecycleDelegateMixin,
    _TranscriptMixin,
    _UsageMixin,
    _TerminalMixin,
):
    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db
        self._title_listeners: list[Callable[[str, str], None]] = []
```

**Line-count invariant at task boundary:** `_manager.py` < 100 LOC (just imports, class definition, `__init__`); every mixin < 400 LOC; no file exceeds 1000 LOC.

**Verification:**
```bash
wc -l src/gobby/storage/sessions/*.py
uv run pytest tests/storage/ tests/sessions/ tests/hooks/ -x
```

### 1.5 Phase-1 line-count invariant meta-check [category: refactor] (depends: 1.4)

Target: `tests/meta/test_import_hygiene.py` (new file — create `tests/meta/__init__.py` if needed)

Add a file-size invariant check so the mixin split cannot silently regress:

```python
"""Invariant checks for the session storage package layout."""
import pathlib


def test_session_storage_mixin_file_sizes() -> None:
    """After the #12092 split, no sessions-package file may exceed 1000 LOC
    (CLAUDE.md principle #2) and mixin files must stay under 400 LOC to keep
    the split meaningful.
    """
    sessions_dir = pathlib.Path("src/gobby/storage/sessions")
    assert sessions_dir.is_dir(), "storage/sessions must be a package after #12092"

    HARD_CAP = 1000
    MIXIN_SOFT_CAP = 400
    violations: list[str] = []
    for py_file in sorted(sessions_dir.glob("*.py")):
        loc = len(py_file.read_text().splitlines())
        if loc > HARD_CAP:
            violations.append(f"{py_file}: {loc} > {HARD_CAP} (monolith cap)")
        # _manager.py composes mixins; allow slight overage for imports + class def
        if py_file.name not in {"__init__.py", "_manager.py"} and loc > MIXIN_SOFT_CAP:
            violations.append(f"{py_file}: {loc} > {MIXIN_SOFT_CAP} (mixin cap)")
    assert not violations, "\n".join(violations)
```

**Bounded phase-closing verification (touched subsystems only):**
```bash
uv run pytest tests/meta/test_import_hygiene.py -x
uv run pytest tests/storage/ tests/sessions/ tests/hooks/ -x
uv run mypy src/gobby/storage/sessions src/gobby/sessions src/gobby/hooks
uv run ruff check src/gobby/storage/sessions
```

## Phase 2: Unify service and storage into one SessionManager class

**Goal**: Collapse `sessions/manager.py` into the storage package. Introduce `SessionManager` as the single public class. Keep `LocalSessionManager` as a temporary alias so Phase 3 can migrate call sites independently. **`register_session()` stays as a first-class method with distinct, documented hook-friendly-fallback semantics — not as a compat shim.** `get_session()` (dict adapter) stays as a short-lived compat alias, removed in Phase 4.

### 2.1 Absorb service-layer state and create the unified class [category: refactor] (depends: Phase 1)

Target: `src/gobby/storage/sessions/_registration_cache.py` (new), `src/gobby/storage/sessions/_manager.py`

Rename the composed class `LocalSessionManager` → `SessionManager` in `_manager.py`. Add a new `_RegistrationCacheMixin` holding the absorbed service-layer state (in-memory caches, locks) and the **first-class** methods that depend on it. **Do NOT move the fabricated-UUID fallback into `register()`** — keep it in `register_session()` only.

**`src/gobby/storage/sessions/_registration_cache.py`** — `_RegistrationCacheMixin` (~260 LOC). Move verbatim from `src/gobby/sessions/manager.py`:

- `lookup_session_id` (305-344)
- `recover_session` (346-387)
- `get_session_id` (389-401)
- `cache_session_mapping` (403-413)
- `backfill_terminal_context` (442-475) — returns `tuple[Session | None, bool]` as today
- `find_parent_session` (203-263) — polling wrapper around `self.find_parent(...)`; signature unchanged
- `mark_session_expired(session_id) -> bool` (265-275) — thin `return self.update_status(...) is not None`

**`register_session()` — first-class method with hook-friendly fallback** (lift verbatim from `sessions/manager.py:105-201`, adjust to use `self.register(...)` instead of `self._storage.register(...)`):

```python
def register_session(
    self,
    external_id: str,
    machine_id: str,
    source: str,
    project_id: str | None,
    parent_session_id: str | None = None,
    transcript_path: str | None = None,
    title: str | None = None,
    git_branch: str | None = None,
    project_path: str | None = None,
    terminal_context: dict[str, Any] | None = None,
    workflow_name: str | None = None,
    agent_depth: int = 0,
    sandbox_enabled: bool | None = None,
) -> str:
    """Hook-friendly session registration.

    Returns a str session id. On storage failure, logs the exception and
    returns a freshly-fabricated UUID str so hook dispatch can continue
    without a live DB row. The ephemeral id is NOT persisted and is NOT
    inserted into caches.

    **When to use:** hook event handlers and any code path that must keep
    moving even if the sessions table is briefly unavailable.

    **When NOT to use:** non-hook callers that need persisted-or-raise
    semantics (e.g. pipeline session creation, HTTP POST /sessions/register).
    Those callers must use `register(...)` directly and handle the exception.
    """
    working_dir = project_path or str(Path.cwd())
    if not git_branch:
        try:
            from gobby.utils.git import get_git_branch
            git_branch = get_git_branch(working_dir)
        except Exception as e:
            self.logger.debug(f"Could not extract git_branch: {e}")
    try:
        session = self.register(
            external_id=external_id,
            machine_id=machine_id,
            source=source,
            project_id=project_id,
            title=title,
            transcript_path=transcript_path,
            git_branch=git_branch,
            parent_session_id=parent_session_id,
            terminal_context=terminal_context,
            workflow_name=workflow_name,
            agent_depth=agent_depth,
            sandbox_enabled=sandbox_enabled,
        )
        session_id: str = session.id
        with self._session_mapping_lock:
            self._session_mapping[(external_id, source)] = session_id
        with self._session_metadata_lock:
            self._session_metadata[session_id] = {
                "external_id": external_id,
                "machine_id": machine_id,
                "source": source,
                "parent_session_id": parent_session_id,
                "transcript_path": transcript_path,
                "project_id": project_id,
                "title": title,
                "git_branch": git_branch,
                "workflow_name": workflow_name,
                "agent_depth": agent_depth,
                "sandbox_enabled": sandbox_enabled,
            }
        self.logger.debug(f"Registered session {session_id} (external_id={external_id})")
        return session_id
    except Exception as e:
        self.logger.error(f"Failed to register session: {e}", exc_info=True)
        # Hook-friendly fallback: ephemeral, not persisted, not cached.
        import uuid
        return str(uuid.uuid4())
```

**`update_session_status(session_id, status) -> bool`** — **kept as a first-class method on the unified class**, not removed. The service wrapper signature (returning bool) is preserved. Hook callers (`_agent.py`, `_misc.py`) keep working unchanged. Implementation:

```python
def update_session_status(self, session_id: str, status: str) -> bool:
    """Boolean-shaped status update for hook callers.

    Wraps update_status(session_id, status) which returns Session | None.
    Kept as a first-class method for parity with the pre-#12092 service
    layer — callers (hooks/event_handlers/_agent.py, _misc.py) that just
    want 'did it happen?' rather than the updated row.
    """
    return self.update_status(session_id, status) is not None
```

**`_manager.py`** — final `SessionManager` class with merged state:

```python
from gobby.storage.sessions._registration_cache import _RegistrationCacheMixin


class SessionManager(
    _BootstrapMixin,
    _CrudMixin,
    _DiscoveryMixin,
    _FieldUpdateMixin,
    _BulkUpdateMixin,
    _QueryMixin,
    _LifecycleDelegateMixin,
    _TranscriptMixin,
    _UsageMixin,
    _TerminalMixin,
    _RegistrationCacheMixin,
):
    def __init__(
        self,
        db: DatabaseProtocol,
        *,
        logger_instance: logging.Logger | None = None,
        config: DaemonConfig | None = None,
    ) -> None:
        self.db = db
        self.logger = logger_instance or logging.getLogger(__name__)
        self._config = config
        self._title_listeners: list[Callable[[str, str], None]] = []
        self._session_mapping: dict[tuple[str, str], str] = {}
        self._session_mapping_lock = threading.Lock()
        self._session_metadata: dict[str, dict[str, Any]] = {}
        self._session_metadata_lock = threading.Lock()
```

**Regression test deliverable — mandatory in this task** — create `tests/storage/sessions/test_register_fallback.py`:

```python
"""Regression guard for the register_session hook-friendly UUID fallback.
This behavior is CLAUDE.md-level per the #12092 validation criteria — hooks
rely on register_session() returning a string id even when the DB raises.
register() itself must remain persisted-or-raise.
"""
from unittest.mock import MagicMock, patch
import pytest
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager


@pytest.fixture
def session_manager(temp_db: LocalDatabase) -> SessionManager:
    return SessionManager(temp_db)


def test_register_session_returns_uuid_str_on_storage_failure(
    session_manager: SessionManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """register_session MUST return a string UUID even when register raises."""
    with patch.object(
        session_manager, "register", side_effect=RuntimeError("simulated DB failure")
    ):
        result = session_manager.register_session(
            external_id="ext-fail",
            machine_id="machine-1",
            source="claude",
            project_id=None,
        )
    assert isinstance(result, str)
    assert len(result) == 36  # UUID v4 canonical length
    assert "Failed to register session" in caplog.text


def test_register_session_fallback_does_not_persist(
    session_manager: SessionManager,
) -> None:
    with patch.object(
        session_manager, "register", side_effect=RuntimeError("fail")
    ):
        result = session_manager.register_session(
            external_id="ext-not-persisted",
            machine_id="m",
            source="claude",
            project_id=None,
        )
    assert session_manager.get(result) is None  # DB has no row


def test_register_session_fallback_does_not_populate_caches(
    session_manager: SessionManager,
) -> None:
    with patch.object(
        session_manager, "register", side_effect=RuntimeError("fail")
    ):
        result = session_manager.register_session(
            external_id="ext-cache",
            machine_id="m",
            source="claude",
            project_id=None,
        )
    assert session_manager.get_session_id("ext-cache", "claude") is None
    assert result not in session_manager._session_metadata


def test_register_session_happy_path_populates_caches(
    session_manager: SessionManager,
) -> None:
    result = session_manager.register_session(
        external_id="ext-happy",
        machine_id="m",
        source="claude",
        project_id=None,
    )
    assert isinstance(result, str)
    assert session_manager.get_session_id("ext-happy", "claude") == result
    assert result in session_manager._session_metadata


def test_register_raises_on_storage_failure(
    session_manager: SessionManager,
) -> None:
    """register() MUST NOT catch DB failures. Non-hook callers depend on the
    exception propagating (pipeline_executor falls back to caller session;
    servers/routes/sessions/core turns into HTTP 500).
    """
    with patch.object(
        session_manager.db, "execute", side_effect=RuntimeError("simulated DB failure")
    ):
        with pytest.raises(RuntimeError, match="simulated DB failure"):
            session_manager.register(
                external_id="ext-raise",
                machine_id="m",
                source="claude",
            )
```

**Verification (bounded):**
```bash
uv run pytest tests/storage/sessions/test_register_fallback.py tests/storage/ tests/sessions/ -x
uv run mypy src/gobby/storage/sessions
```

### 2.2 Expose SessionManager via compat aliases and deprecation shims [category: refactor] (depends: 2.1)

Target: `src/gobby/storage/sessions/__init__.py`, `src/gobby/storage/__init__.py`, `src/gobby/sessions/manager.py`, `src/gobby/sessions/__init__.py`, `src/gobby/storage/sessions/_crud.py`

Wire up backward-compatible names so Phase 3 can migrate call sites independently.

**`src/gobby/storage/sessions/__init__.py`:**

```python
from gobby.storage.sessions._manager import SessionManager
# Temporary alias — removed in Phase 4 after all call sites migrate.
LocalSessionManager = SessionManager

__all__ = [
    "SessionManager",
    "LocalSessionManager",  # deprecated alias
    "SYSTEM_SESSION_ID",
    "SYSTEM_SESSION_EXTERNAL_ID",
    "SYSTEM_SESSION_MACHINE_ID",
    "SYSTEM_SESSION_SOURCE",
    "ensure_system_session",
]
```

**`src/gobby/storage/__init__.py`:**

Add `SessionManager` to `__all__`, `_EXPORTS`, and the `TYPE_CHECKING` block; keep `LocalSessionManager` entries until Phase 4.

```python
__all__ = [
    # ...
    "LocalSessionManager",  # deprecated alias, kept until Phase 4
    "SessionManager",       # canonical name
    # ...
]

_EXPORTS = {
    # ...
    "LocalSessionManager": ("gobby.storage.sessions", "LocalSessionManager"),
    "SessionManager": ("gobby.storage.sessions", "SessionManager"),
    # ...
}

# TYPE_CHECKING block: add the SessionManager import alongside LocalSessionManager.
```

**`src/gobby/sessions/manager.py`** — replace the entire file contents with a shim:

```python
"""Deprecation shim — re-exports SessionManager from its canonical location.

This module existed when SessionManager was a service wrapper around
LocalSessionManager. The two classes were consolidated in #12092.
This shim is removed in Phase 4; new code must import from gobby.storage.sessions.
"""
from gobby.storage.sessions import SessionManager

__all__ = ["SessionManager"]
```

**`src/gobby/sessions/__init__.py`:**

```python
"""Sessions package for multi-CLI session management."""
from gobby.storage.sessions import SessionManager

__all__ = ["SessionManager"]
```

**`get_session` dict adapter as a compat alias** — add to `_CrudMixin` in `_crud.py`. Kept ONLY so the Phase 3 migration can proceed one subsystem at a time. Deleted in Phase 4:

```python
# In _crud.py (inside _CrudMixin)
def get_session(self, session_id: str) -> dict[str, Any] | None:
    """DEPRECATED compat alias. Use get(session_id) which returns Session | None.
    Removed in Phase 4 (#12092). Kept during Phase 3 so call-site migration
    can proceed one subsystem at a time.
    """
    session = self.get(session_id)
    if session is None:
        return None
    return {
        "id": session.id,
        "external_id": session.external_id,
        "machine_id": session.machine_id,
        "source": session.source,
        "project_id": session.project_id,
        "title": session.title,
        "status": session.status,
        "transcript_path": session.transcript_path,
        "summary_path": session.summary_path,
        "git_branch": session.git_branch,
        "parent_session_id": session.parent_session_id,
    }
```

**NOTE: `register_session` is NOT added as a compat shim — it already exists as a first-class method on `_RegistrationCacheMixin` (see 2.1).**

**Breaking-change note for the one production call site:** the old `SessionManager(session_storage=..., logger_instance=...)` constructor is replaced with `SessionManager(db, *, logger_instance=None, config=None)`. The sole in-production caller (`hooks/factory.py:205`) is updated in Phase 3 Wave 4. The existing test fixture in `tests/sessions/test_sessions_manager.py:35-40` must be updated in this task:

```python
@pytest.fixture
def session_mgr(temp_db: LocalDatabase) -> SessionManager:
    return SessionManager(temp_db, logger_instance=MagicMock())
```

Existing `session["title"]` dict assertions in `tests/sessions/test_sessions_manager.py` keep working via the `get_session` compat — no test changes needed in this task.

**Verification (bounded):**
```bash
uv run pytest tests/storage/sessions/ tests/sessions/ tests/hooks/ tests/mcp_proxy/services/test_tool_proxy_validation.py tests/mcp_proxy/services/test_tool_proxy_coverage.py -x
uv run mypy src/gobby/storage src/gobby/sessions src/gobby/hooks src/gobby/mcp_proxy
```

## Phase 3: Migrate call sites wave by wave

**Goal**: Migrate every `LocalSessionManager` reference — in all forms — across the `src/gobby/` tree to use the unified `SessionManager`. Each wave is an independently-reviewable, independently-revertible commit. **Complete inventory** (from `grep -rln 'LocalSessionManager' src/gobby/` + the `_session_storage` grep in hooks/): ~70 source files across `agents/`, `app_context.py`, `cli/`, `communications/`, `conductor/`, `events/`, `hooks/`, `mcp_proxy/`, `runner*.py`, `sessions/`, `servers/`, `storage/` (internal), `utils/`, `workflows/`.

**Cross-wave rule #1 (source forms):** migrate `LocalSessionManager` in every form — `import` lines, type annotations, constructor parameter types, `TYPE_CHECKING` blocks, AND docstrings + comments. The Phase 4.2 meta test enforces this via a source-wide raw-string ban; any stale docstring or comment reference will block the final invariant test. Known docstring/comment-only sites (no code-symbol change, but raw-string cleanup still required) are called out by line inside each wave below — but treat them as examples, not as an exhaustive list: the final grep in Phase 4.2 is authoritative.

**Cross-wave rule #2 (test-side migration alongside source):** `unittest.mock.patch` binds to a module+name tuple (e.g. `@patch("gobby.agents.kill.LocalSessionManager")`), so when a wave renames the import in `gobby.agents.kill` from `LocalSessionManager` to `SessionManager`, every test that patches the old target in that module breaks immediately. Each wave below therefore has an explicit **Test migration** subsection listing the test files whose patch targets, imports, type annotations, or `monkeypatch.setattr` calls must be updated in the SAME commit. Rename patch targets from `LocalSessionManager` → `SessionManager` at the module path where the symbol is looked up (not defined) — this is the standard `patch` guidance, just applied mechanically. Inventory is grep-verified against current tests; the Phase 4.1 test-sweep enumerates any stragglers the waves missed, and the Phase 4.2 meta test extends the raw-string ban to `tests/` so the invariant is enforced across both surfaces.

### 3.1 Wave 1 — agents/ subsystem [category: refactor] (depends: Phase 2)

Target files (confirmed via grep — all 6):
- `src/gobby/agents/kill.py` — construction at lines 77, 203; import at line 19
- `src/gobby/agents/lifecycle_monitor.py` — type annotation at line 62 (`session_manager: LocalSessionManager | None`); import at line 38
- `src/gobby/agents/registry.py` — construction at lines 359, 503; imports at lines 356, 500
- `src/gobby/agents/runner.py` — type annotation at line 42 (`session_storage: LocalSessionManager`); import at line 23
- `src/gobby/agents/session.py` — `ChildSessionManager.__init__` parameter annotation at line 77; import at line 18; docstring mentions at lines 72, 84 (update to mention SessionManager)
- `src/gobby/agents/tmux/pane_monitor.py` — type annotation at line 48; import at line 24

For each: swap `from gobby.storage.sessions import LocalSessionManager` → `from gobby.storage.sessions import SessionManager`; replace every `LocalSessionManager` symbol (construction and annotation) with `SessionManager`.

**Test migration (same commit):**
- `tests/agents/test_kill.py` — 4 patch decorators at lines 60, 79, 98, 151: `@patch("gobby.agents.kill.LocalSessionManager")` → `@patch("gobby.agents.kill.SessionManager")`.
- `tests/agents/test_lifecycle_monitor.py` — imports and type annotations: `LocalSessionManager` → `SessionManager`.
- `tests/agents/test_registry.py:1370` — `patch("gobby.storage.sessions.LocalSessionManager")` → `patch("gobby.storage.sessions.SessionManager")`.
- `tests/agents/test_registry_extended.py:197` — `patch("gobby.storage.sessions.LocalSessionManager.get")` → `patch("gobby.storage.sessions.SessionManager.get")`.

**Verification:**
```bash
uv run pytest tests/agents/ -x
uv run mypy src/gobby/agents
```

### 3.2 Wave 2 — cli/ subsystem [category: refactor] (depends: 3.1)

Target files:
- `src/gobby/cli/sessions.py` — `get_session_manager()` at line 16 (return type + construction at :19); secondary construction at :529; imports at lines 13 and 523
- `src/gobby/cli/tasks/expand.py` — construction at line 40; import at line 15
- `src/gobby/cli/memory.py` — construction at line 377; import at line 374
- `src/gobby/cli/tokens.py` — construction at line 146 (import is local)
- `src/gobby/cli/utils.py` — construction at line 189

**Test migration (same commit):**
- `tests/cli/test_sessions_coverage.py:72` — `@patch("gobby.cli.sessions.LocalSessionManager")` → `@patch("gobby.cli.sessions.SessionManager")`.
- `tests/cli/test_utils_coverage.py:520, 537` — `patch("gobby.cli.utils.LocalSessionManager", ...)` → `patch("gobby.cli.utils.SessionManager", ...)`.
- `tests/cli/test_tokens_cli.py:87, 112` — `monkeypatch.setattr(tokens_module, "LocalSessionManager", ...)` → `monkeypatch.setattr(tokens_module, "SessionManager", ...)`.
- `tests/cli/test_cli_utils.py` — audit imports; migrate any `LocalSessionManager` references.

**Verification:**
```bash
uv run pytest tests/cli/ -x
uv run mypy src/gobby/cli
```

### 3.3 Wave 3 — mcp_proxy tools and registries [category: refactor] (depends: 3.2)

Target files (28 total — inventory grep-verified against current source):

Construction sites:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:145`
- `src/gobby/mcp_proxy/tools/tasks/_context.py:65`
- `src/gobby/mcp_proxy/tools/task_readiness.py:393`
- `src/gobby/mcp_proxy/tools/apply_persona.py:192`
- `src/gobby/mcp_proxy/tools/workflows/__init__.py:103`
- `src/gobby/mcp_proxy/tools/skills/__init__.py:85`

Type-only sites (annotations, imports, or passthrough references):
- `src/gobby/mcp_proxy/registries.py` (dual-annotation collapse — see below)
- `src/gobby/mcp_proxy/tools/agent_messaging.py`
- `src/gobby/mcp_proxy/tools/agents.py`
- `src/gobby/mcp_proxy/tools/memory.py` — docstring-only reference at :68 (`session_manager: LocalSessionManager for session lookups (optional)`); no code-symbol change, rewrite to `SessionManager`.
- `src/gobby/mcp_proxy/tools/metrics.py` — docstring-only reference at :31 (`session_storage: Optional LocalSessionManager for usage reporting`); no code-symbol change, rewrite to `SessionManager`.
- `src/gobby/mcp_proxy/tools/sessions/_actions.py`
- `src/gobby/mcp_proxy/tools/sessions/_commits.py`
- `src/gobby/mcp_proxy/tools/sessions/_crud.py`
- `src/gobby/mcp_proxy/tools/sessions/_factory.py`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py`
- `src/gobby/mcp_proxy/tools/sessions/_messages.py`
- `src/gobby/mcp_proxy/tools/sessions/_registration.py`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py`
- `src/gobby/mcp_proxy/tools/sessions/_transcripts.py`
- `src/gobby/mcp_proxy/tools/skills/_context.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`
- `src/gobby/mcp_proxy/tools/workflows/_query.py`
- `src/gobby/mcp_proxy/tools/workflows/_variables.py` — import at :21; annotations at :64, :141; docstrings at :81, :156
- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py` — import at :39; annotations at :63, :117, :552, :594
- `src/gobby/mcp_proxy/tools/workflows/_resolution.py` — import at :12; annotations at :20, :30
- `src/gobby/mcp_proxy/tools/worktrees/_context.py`
- `src/gobby/mcp_proxy/tools/worktrees/_factory.py` — import at :20; annotation at :30

For each: swap import and symbol references to `SessionManager`.

**Dual-annotation collapse at `src/gobby/mcp_proxy/registries.py`** (lines 42, 48):

```python
# Before
_session_manager: SessionManager | None     # the old service wrapper
local_session_manager: LocalSessionManager | None

# After
session_manager: SessionManager | None   # single attribute
```

Propagate the rename everywhere the registry is consumed: `grep -rn 'local_session_manager\|registries\._session_manager\|registries\.session_manager' src/gobby/mcp_proxy/` — update every reader to the unified attribute name.

**Test migration (same commit):**
- `tests/mcp_proxy/tools/test_claim_task.py` — 7 patches at lines 91, 164, 221, 416, 470 (+ 2 more): `patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager")` → `patch("gobby.mcp_proxy.tools.tasks._context.SessionManager")`.
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py:73, 206, 354` — same pattern on `_context.LocalSessionManager`.
- `tests/mcp_proxy/tools/test_tasks_coverage.py:607, 660` — same pattern on `_context.LocalSessionManager`.
- `tests/mcp_proxy/tools/test_skills_coverage.py:71` — `patch("gobby.mcp_proxy.tools.skills.LocalSessionManager")` → `patch("gobby.mcp_proxy.tools.skills.SessionManager")`.
- `tests/mcp_proxy/tools/workflows/test_resolution.py` — audit via grep; migrate references.
- Any other `tests/mcp_proxy/**/*.py` files surfaced by `grep -rn 'LocalSessionManager' tests/mcp_proxy/` at the time of this wave.

**Verification:**
```bash
uv run pytest tests/mcp_proxy/ -x --ignore=tests/mcp_proxy/services/test_tool_proxy_validation.py --ignore=tests/mcp_proxy/services/test_tool_proxy_coverage.py
uv run mypy src/gobby/mcp_proxy
```

(The two excluded files are rewritten in Wave 6.)

### 3.4 Wave 4 — hooks/ subsystem: HookManager attribute unification + update_session_status migration [category: refactor] (depends: 3.3)

**Scope: largest wave.** Collapses `HookManager._session_storage` and `_session_manager` into a single `_session_manager: SessionManager` attribute, migrates every remaining reader, and decides what happens to `update_session_status` callers.

**Target files — construction / factory:**
- `src/gobby/hooks/factory.py` — line 205: delete the `SessionManager(session_storage=..., logger_instance=..., config=...)` double construction. Line 392: `session_storage = LocalSessionManager(db)` → `session_manager = SessionManager(db, logger_instance=..., config=...)`. `HookManagerComponents` dataclass: remove the `session_storage: LocalSessionManager` field, keep one `session_manager: SessionManager`. `_create_storage` helper returns the unified `SessionManager`. Imports: remove `LocalSessionManager`, add `SessionManager` from `gobby.storage.sessions`.
- `src/gobby/hooks/hook_manager.py` — lines 163, 178, 202, 211, and 819: remove `self._session_storage = ...` assignments, keep only `self._session_manager: SessionManager = ...`. Rename every `self._session_storage.<method>` → `self._session_manager.<method>`.
- `src/gobby/hooks/session_coordinator.py` — audit via `grep -n 'LocalSessionManager\|_session_storage' src/gobby/hooks/session_coordinator.py`; update imports and attribute names accordingly.

**Target files — event handlers (_session_storage readers):**
- `src/gobby/hooks/event_handlers/_base.py:15, 17, 26, 29` — collapse TYPE_CHECKING imports and attribute annotations:

  ```python
  # Before
  from gobby.sessions.manager import SessionManager
  from gobby.storage.sessions import LocalSessionManager

  class _BaseEventHandler:
      _session_manager: SessionManager | None
      _session_storage: LocalSessionManager | None

  # After
  from gobby.storage.sessions import SessionManager

  class _BaseEventHandler:
      _session_manager: SessionManager | None
  ```

- `src/gobby/hooks/event_handlers/__init__.py` — four edits:
  1. Line 30: `from gobby.sessions.manager import SessionManager` → `from gobby.storage.sessions import SessionManager` (old shim path is deleted in Phase 4.1 and banned by Phase 4.2 meta test).
  2. Line 32: `from gobby.storage.sessions import LocalSessionManager` → delete this import entirely (the unified `SessionManager` import from step 1 covers it).
  3. Line 57: `session_storage: LocalSessionManager | None = None` → `session_manager: SessionManager | None = None` (rename parameter AND type).
  4. Line 78: docstring `session_storage: LocalSessionManager for session storage` → `session_manager: SessionManager for session storage`.
  5. Line 93: `self._session_storage = session_storage` → `self._session_manager = session_manager`.
- `src/gobby/hooks/event_handlers/_agent.py` — 21 `_session_storage` matches at lines 144, 149, 164, 177, 198, 472 etc.; 4 `update_session_status` call sites at lines 74, 381, 402, 439. Every `_session_storage` reference → `_session_manager`. The 4 `update_session_status` callers continue working because `update_session_status` is a first-class method on the unified class per §2.1 — no caller code change required; just the attribute rename.
- `src/gobby/hooks/event_handlers/_misc.py` — matches at 46, 91, 101, 112. Same treatment: `_session_storage` → `_session_manager`. Line 46's `update_session_status(...)` continues to work unchanged.
- `src/gobby/hooks/event_handlers/_session_end.py` — matches at 45, 47, 124, 133, 136. `_session_storage` → `_session_manager`.
- `src/gobby/hooks/event_handlers/_session_responses.py` — matches at 38, 44. `handler._session_storage` → `handler._session_manager`.
- `src/gobby/hooks/event_handlers/_session_start.py` — ~32 occurrences of `_session_storage` across lines 219, 222, 240, 248, 281 etc. Bulk rename to `_session_manager`. The existing `register_session(...)` calls at lines 347, 400, 413, 680, 724 continue working (first-class method on unified class per §2.1).
- `src/gobby/hooks/event_handlers/_tool.py` — matches at 252, 302, 341. `_session_storage` → `_session_manager`.
- `src/gobby/hooks/event_enrichment.py` — matches at 63, 90, 92, 199, 201. Constructor parameter `session_storage` → `session_manager`; attribute `self._session_storage` → `self._session_manager`. Also comment at :59 (`session_storage: Any,  # Avoid runtime import of LocalSessionManager`) → rewrite to reference `SessionManager` (the `Any`-typed parameter can also be tightened to `SessionManager | None` now that the circular-import concern from two-class split no longer applies — verify no runtime import cycle is introduced).
- `src/gobby/hooks/session_lookup.py` — two changes: (1) line 20 `from gobby.sessions.manager import SessionManager` → `from gobby.storage.sessions import SessionManager` (the old path is removed in Phase 4.1 AND banned by the Phase 4.2 meta test); (2) line 191's `self._session_manager.register_session(...)` call is correct and continues to work unchanged (first-class method on unified class).

**Decision (explicit per adversary F4):** `SessionManager.update_session_status(session_id, status) -> bool` is **preserved as a first-class method** on the unified class (defined in §2.1). Callers in `_agent.py` (4) and `_misc.py` (1) do NOT need to change. No "migrate to `update_status`" step in this wave.

**Test migration (same commit):**
- `tests/workflows/test_workflow_hooks.py:59, 100` — `patch("gobby.hooks.factory.LocalSessionManager")` → `patch("gobby.hooks.factory.SessionManager")`. (This test lives under `tests/workflows/` but patches the hooks factory; migrate it in the hooks wave, not Wave 5.)
- `tests/hooks/test_hooks_manager.py`, `tests/hooks/test_hooks_context.py`, `tests/hooks/test_event_handlers.py`, `tests/hooks/test_hooks_factory.py`, and any other `tests/hooks/**/*.py` that `grep -rn 'LocalSessionManager\|_session_storage\|from gobby.sessions.manager' tests/hooks/` surfaces at the time of this wave — migrate `LocalSessionManager` → `SessionManager` in imports, type annotations, and mock setups. Migrate `_session_storage` attribute mocks → `_session_manager` (since HookManager now has only one attribute). Existing `register_session` / `get_session_id` / `update_session_status` mocks keep working — those methods are first-class on the unified class per §2.1.
- `tests/mcp_proxy/services/test_tool_proxy_validation.py:831-832, 927, 956` — already covered by Wave 6 (3.6); do NOT duplicate here.

**Verification:**
```bash
uv run pytest tests/hooks/ tests/workflows/test_workflow_hooks.py -x
uv run mypy src/gobby/hooks
grep -rn '_session_storage' src/gobby/hooks/  # must return zero hits
grep -rn '_session_storage\|LocalSessionManager' tests/hooks/  # must return zero non-comment hits
```

### 3.5 Wave 5 — sessions/, servers/, utils/, runner, workflows [category: refactor] (depends: 3.4)

**Target files — sessions/:**
- `src/gobby/sessions/liveness_monitor.py:59` — type annotation / import; migrate.
- `src/gobby/sessions/processor.py:52-58` — type annotations / import.
- `src/gobby/sessions/summarize.py:62` — docstring-only reference (`session_manager: LocalSessionManager instance.`); no code-symbol change, rewrite to `SessionManager`.
- `src/gobby/sessions/transcript_reader.py:393` — type annotation; migrate.

**Target files — servers/:**
- `src/gobby/servers/routes/sessions/core.py:298-333` — construction uses `server.session_manager` (already a SessionManager attribute via factory); import updates only.
- `src/gobby/servers/routes/sessions/handoff.py` — audit via grep; migrate.
- `src/gobby/servers/routes/sessions/lifecycle.py` — audit; migrate. Note: lifecycle.py has its own HTTP `update_session_status` route at :282 — that is an HTTP endpoint name, unrelated to the class method. No rename required.
- `src/gobby/servers/routes/sessions/messages.py:23-36` — simplify `_get_session_record` to a single `session_manager.get(...)` call returning `Session | None`. The `_session_attr` helper already tolerates both dict and dataclass; now that only `Session` flows through, audit callers at lines 136-137, 157, 184-185, 226-227 and switch to attribute access. `_session_attr` can be deleted OR kept as a one-liner `getattr` wrapper — prefer deletion.
- `src/gobby/servers/routes/sessions/parent.py` — audit; migrate.
- `src/gobby/servers/routes/mcp/hooks.py:168` — construction `LocalSessionManager(db)` → `SessionManager(db)`.
- `src/gobby/servers/websocket/server.py:73` — type annotation / import; migrate.

**Target files — miscellaneous:**
- `src/gobby/app_context.py:19, 38` — type annotation `session_manager: LocalSessionManager`; migrate.
- `src/gobby/runner.py:109` — type annotation / import; migrate.
- `src/gobby/runner_init.py:225` — construction; migrate.
- `src/gobby/utils/project_context.py:191` — docstring-only reference (`session_manager: LocalSessionManager instance.`); no code-symbol change, rewrite to `SessionManager`.
- `src/gobby/utils/session_context.py` — audit; migrate.
- `src/gobby/communications/identities.py` — audit; migrate.
- `src/gobby/communications/manager.py:50` — type annotation / import; migrate.
- `src/gobby/conductor/manager.py:57` — type annotation / import; migrate.
- `src/gobby/events/wake.py:47` — type annotation / import; migrate.
- `src/gobby/sessions/lifecycle.py:68` — construction; migrate.

**Target files — storage/ helper module docstring cleanup:**

Both files were extracted from the old `LocalSessionManager` and reference the old name in module-level comments/docstrings. Phase 4.2's meta test does a source-wide string ban of `LocalSessionManager`, so the stale references must be rewritten here:

- `src/gobby/storage/session_resolution.py:4` — `"""Extracted from LocalSessionManager.resolve_session_reference()"""` → rewrite to `"""Extracted from the session-management class's resolve_session_reference()"""` (or equivalent wording that does not reference the old class name).
- `src/gobby/storage/session_lifecycle.py:4` — `"""Extracted from LocalSessionManager as part of the Strangler Fig..."""` → rewrite to reference `SessionManager` or drop the historical note.

Neither file imports `LocalSessionManager` as a symbol (these are helper modules called BY the manager methods); the fix is purely string-level.

**Target files — workflows/:**
- `src/gobby/workflows/engine/templating.py:81` — construction; migrate.
- `src/gobby/workflows/git_utils.py` — audit; migrate.
- `src/gobby/workflows/pipeline/handlers.py` — audit; migrate.
- `src/gobby/workflows/pipeline_executor.py` — (1) :338-359 construction (`self.session_manager` already the right type); import swap only. The existing try/except around `self.session_manager.register(...)` continues to work because `register()` preserves its raise-on-failure contract per §2.1. (2) Docstring at :104 (`session_manager: Optional LocalSessionManager for session creation`) → rewrite to `SessionManager`.
- `src/gobby/workflows/pipeline_heartbeat.py` — audit; migrate.

For every file listed: swap `from gobby.storage.sessions import LocalSessionManager` → `from gobby.storage.sessions import SessionManager`; replace every `LocalSessionManager` symbol (annotation or construction) with `SessionManager`.

**Test migration (same commit):**
- `tests/sessions/test_lifecycle.py:33` — `patch("gobby.sessions.lifecycle.LocalSessionManager")` → `patch("gobby.sessions.lifecycle.SessionManager")`.
- `tests/workflows/test_pipeline_heartbeat.py` — audit via grep; migrate `LocalSessionManager` references.
- `tests/workflows/test_pipeline_executor_child_session.py` — audit via grep; migrate.
- `tests/servers/test_http_server.py`, `tests/servers/test_mcp_routes.py` — audit and migrate.
- `tests/test_runner.py:148` — `patch("gobby.runner_init.LocalSessionManager")` → `patch("gobby.runner_init.SessionManager")`.
- `tests/conftest.py` — the `session_manager` fixture body is updated in Phase 4.1 (fixture name stays the same); if the type annotation or import at the top references `LocalSessionManager`, migrate here.
- `tests/storage/test_storage_sessions.py` — audit; the existing `session_storage` fixture may bind to `LocalSessionManager`. Migrate to `SessionManager` since the storage layer IS the unified class post-Phase 2.
- `tests/autonomous/test_autonomous.py`, `tests/integration/test_edit_history.py`, `tests/integration/test_terminal_mode_worktrees.py`, `tests/integration/test_worktree_lifecycle.py` — audit via `grep -rn 'LocalSessionManager' tests/autonomous/ tests/integration/` at the time of this wave; migrate any hits.

**Verification:**
```bash
uv run pytest tests/servers/ tests/sessions/ tests/workflows/ tests/storage/ tests/conductor/ tests/test_runner.py tests/autonomous/ tests/integration/ -x
uv run mypy src/gobby/sessions src/gobby/servers src/gobby/workflows src/gobby/utils src/gobby/conductor src/gobby/communications src/gobby/events src/gobby/app_context src/gobby/runner src/gobby/runner_init
grep -rn 'LocalSessionManager' src/gobby/ | grep -v 'test_' | grep -v 'storage/sessions/' | grep -v '__pycache__'  # should only show the storage package alias line and storage/__init__.py compat export; storage/session_resolution.py and storage/session_lifecycle.py docstrings must be clean after this wave
```

### 3.6 Wave 6 — mcp_proxy/services/tool_proxy.py (the #12082 site) [category: refactor] (depends: 3.5)

Target: `src/gobby/mcp_proxy/services/tool_proxy.py`, `tests/mcp_proxy/services/test_tool_proxy_validation.py`, `tests/mcp_proxy/services/test_tool_proxy_coverage.py`

Collapse the dual `_session_manager` / `_session_storage` reads in `_resolve_tool_event_context` into a single read from the unified attribute.

**`src/gobby/mcp_proxy/services/tool_proxy.py`** (lines 248, 292, 325, 430, 500, 599):

```python
# Before (post-#12082 fix):
session_manager = getattr(hook_manager, "_session_manager", None)
session_storage = getattr(hook_manager, "_session_storage", None)
session = session_storage.get(effective_session_id) if session_storage else None

# After (#12092 unification):
session_manager = getattr(hook_manager, "_session_manager", None)
session = session_manager.get(effective_session_id) if session_manager else None
```

The `_resolve_tool_event_context` return tuple (currently carrying both `session_manager` and `session_storage` as separate elements) collapses to one. Audit every caller of this helper and update the unpacking. The `logger.warning` on storage failure stays — guarded by the regression test below.

**Test rewrites — `tests/mcp_proxy/services/test_tool_proxy_validation.py`:**
- Lines 831-832: delete `hook_manager._session_storage = session_manager`; keep only `hook_manager._session_manager = session_manager`.
- Line 927: `mock_hook_manager._session_storage.get.return_value = pipeline_session` → `mock_hook_manager._session_manager.get.return_value = pipeline_session`.
- Line 956: `mock_hook_manager._session_storage.get.side_effect = RuntimeError(...)` → `mock_hook_manager._session_manager.get.side_effect = RuntimeError(...)`.

Existing test names (`test_call_tool_emits_pipeline_source_when_session_is_pipeline`, `test_call_tool_logs_warning_and_defaults_source_when_storage_raises`) stay — they now guard the unified-manager contract. Update any comments that reference `_session_storage` or "service wrapper without .get".

**`tests/mcp_proxy/services/test_tool_proxy_coverage.py:1127`** — already uses `_session_manager`; no change needed.

**`tests/hooks/test_hooks_context.py:97, 139, 141`** — mocks of `_session_manager.get_session_id` and `.register_session` continue working: `register_session` is a first-class method on the unified class. No change needed in this wave.

**Verification:**
```bash
uv run pytest tests/mcp_proxy/services/test_tool_proxy_validation.py tests/mcp_proxy/services/test_tool_proxy_coverage.py tests/hooks/test_hooks_context.py -x
```

## Phase 4: Remove deprecated names

**Goal**: Delete the `LocalSessionManager` alias and the `get_session` compat shim. The codebase now has exactly one `SessionManager`, one canonical `get()` method, and `register_session` kept as a first-class hook-friendly method. The "no file imports both" invariant is enforced by a meta test.

### 4.1 Remove LocalSessionManager alias and get_session compat shim [category: refactor] (depends: Phase 3)

Target: `src/gobby/storage/sessions/__init__.py`, `src/gobby/storage/__init__.py`, `src/gobby/sessions/manager.py`, `src/gobby/storage/sessions/_crud.py`, `tests/conftest.py`, `tests/sessions/test_sessions_manager.py`, `src/gobby/servers/routes/sessions/messages.py`

**`src/gobby/storage/sessions/__init__.py`** — delete the `LocalSessionManager = SessionManager` line and remove `"LocalSessionManager"` from `__all__`.

**`src/gobby/storage/__init__.py`** — remove `"LocalSessionManager"` from `__all__` and from `_EXPORTS`. Remove the `TYPE_CHECKING` `LocalSessionManager` import.

**`src/gobby/sessions/manager.py`** — DELETE the file. If anything still imports from this path, the import will break — that's the forcing function confirming migration is complete.

**`src/gobby/storage/sessions/_crud.py`** — delete the `get_session` compat shim method. Callers that still use the dict form will fail at type-check time or test runtime.

**Remaining caller migrations:**

- `src/gobby/servers/routes/sessions/messages.py` — complete the `_get_session_record` / `_session_attr` simplification started in Wave 5 (if any dict-style access remained).
- `tests/sessions/test_sessions_manager.py` — convert remaining `session["title"]`, `session["id"]`, `session["source"]`, etc. dict assertions to attribute access (`session.title`, `session.id`, `session.source`). Update `session_mgr` fixture if still using the old service-wrapper construction.
- `tests/conftest.py:102-106` — rename the fixture body only (keep the fixture name `session_manager`):

  ```python
  @pytest.fixture
  def session_manager(temp_db: "LocalDatabase") -> "SessionManager":
      """Create a session manager with temp database."""
      from gobby.storage.sessions import SessionManager
      return SessionManager(temp_db)
  ```

**Final test-alias sweep (same commit as alias removal):**

Enumerate every remaining test site that still references `LocalSessionManager` or the old `gobby.sessions.manager` module path and migrate each. Most were already handled by wave-level test migrations in 3.1–3.5, but the following stragglers may remain and MUST be fixed before running the verification matrix below (the alias is gone at this point, so any stale patch target or import raises `AttributeError` / `ModuleNotFoundError` at collection time):

```bash
# Catalog stragglers — these should output nothing after the sweep completes
grep -rn 'LocalSessionManager' tests/ --include='*.py'
grep -rn 'from gobby.sessions.manager' tests/ --include='*.py'
```

Known sweep targets (grep-verified at plan-time; re-grep at task-time since prior waves may have already handled these):
- `tests/autonomous/test_autonomous.py`
- `tests/integration/test_edit_history.py`, `tests/integration/test_terminal_mode_worktrees.py`, `tests/integration/test_worktree_lifecycle.py`
- `tests/scheduler/test_*.py` — audit via grep
- `tests/tasks/test_*.py` — audit via grep
- Any residual `tests/conftest.py` type hints
- Any file surfaced by the two grep commands above that prior waves did not catch

For each: rename `LocalSessionManager` → `SessionManager` at imports, type annotations, `patch()` targets, and `monkeypatch.setattr` calls. Rewrite `from gobby.sessions.manager import SessionManager` → `from gobby.storage.sessions import SessionManager`.

**Verification (bounded):**
```bash
uv run pytest tests/storage/sessions/ tests/sessions/ tests/hooks/ tests/mcp_proxy/ tests/agents/ tests/cli/ tests/servers/ tests/workflows/ tests/conductor/ tests/autonomous/ tests/integration/ tests/scheduler/ tests/tasks/ tests/test_runner.py -x
uv run mypy src/gobby
uv run ruff check src/gobby tests
# Explicit invariants (also enforced as tests in 4.2):
! grep -rn 'LocalSessionManager' src/gobby/ --include='*.py'
! grep -rn 'LocalSessionManager' tests/ --include='*.py'
! grep -rn 'from gobby.sessions.manager' src/gobby/ --include='*.py'
! grep -rn 'from gobby.sessions.manager' tests/ --include='*.py'
! grep -rn '_session_storage' src/gobby/ --include='*.py'
```

### 4.2 Enforce single-SessionManager invariant via meta checks [category: refactor] (depends: 4.1)

Target: `tests/meta/test_import_hygiene.py`

Extend the meta file from §1.5 with invariants that codify the #12092 validation criteria:

```python
def test_no_file_references_old_session_manager_names() -> None:
    """After #12092 there is a single SessionManager. The old names
    (LocalSessionManager, gobby.sessions.manager module path) must not
    appear anywhere in src/gobby/ OR tests/. A reappearance in src/ is
    a regression back to the dual-class shape that caused bug #12082.
    A reappearance in tests/ means a patch target or import will fail
    at collection time once the alias is gone.
    """
    import pathlib

    roots = [pathlib.Path("src/gobby"), pathlib.Path("tests")]
    violations: list[str] = []
    for root in roots:
        for py_file in root.rglob("*.py"):
            source = py_file.read_text()
            if "LocalSessionManager" in source:
                violations.append(f"{py_file}: contains 'LocalSessionManager'")
            if "gobby.sessions.manager" in source:
                violations.append(f"{py_file}: imports from old 'gobby.sessions.manager' path")
    assert not violations, (
        "After #12092 these names must not appear in src/gobby/ or tests/:\n"
        + "\n".join(violations)
    )


def test_no_session_storage_attribute_in_hooks() -> None:
    """HookManager and its event handlers must expose exactly one session
    manager attribute (`_session_manager`). A `_session_storage` attribute
    or assignment is a regression to the dual-attribute shape that caused
    bug #12082.
    """
    import pathlib

    hooks_dir = pathlib.Path("src/gobby/hooks")
    violations: list[str] = []
    for py_file in hooks_dir.rglob("*.py"):
        source = py_file.read_text()
        if "_session_storage" in source:
            violations.append(f"{py_file}: contains '_session_storage'")
    assert not violations, "\n".join(violations)


def test_hook_manager_has_single_session_attribute(
    hook_manager,  # fixture from tests/hooks/conftest.py
) -> None:
    """Regression for #12092: HookManager exposes exactly one session-manager
    attribute. The split _session_storage / _session_manager is gone.
    """
    from gobby.storage.sessions import SessionManager
    assert hasattr(hook_manager, "_session_manager")
    assert not hasattr(hook_manager, "_session_storage")
    assert isinstance(hook_manager._session_manager, SessionManager)
```

If the `hook_manager` fixture doesn't resolve cleanly from `tests/meta/`, move `test_hook_manager_has_single_session_attribute` to `tests/hooks/test_hooks_context.py` instead.

**Final bounded verification matrix (explicit; replaces full-suite runs):**

```bash
# Targeted per-subsystem test runs — no `uv run pytest` without a path filter.
uv run pytest tests/meta/ -x
uv run pytest tests/storage/sessions/ -x
uv run pytest tests/sessions/ -x
uv run pytest tests/hooks/ -x
uv run pytest tests/mcp_proxy/services/test_tool_proxy_validation.py tests/mcp_proxy/services/test_tool_proxy_coverage.py -x
uv run pytest tests/mcp_proxy/ -x
uv run pytest tests/agents/ tests/cli/ tests/servers/ tests/workflows/ tests/conductor/ tests/communications/ -x

# Type + lint (scoped then repo-wide)
uv run mypy src/gobby/storage/sessions src/gobby/sessions src/gobby/hooks src/gobby/mcp_proxy
uv run mypy src/gobby
uv run ruff check src/gobby tests

# Explicit regression invariants (each is a single test; not the full suite):
uv run pytest tests/storage/sessions/test_register_fallback.py -x
uv run pytest tests/mcp_proxy/services/test_tool_proxy_validation.py::test_call_tool_emits_pipeline_source_when_session_is_pipeline tests/mcp_proxy/services/test_tool_proxy_validation.py::test_call_tool_logs_warning_and_defaults_source_when_storage_raises -x
uv run pytest tests/meta/test_import_hygiene.py -x

# Manual grep sanity (ship in PR description):
! grep -rn 'LocalSessionManager' src/gobby/ --include='*.py'
! grep -rn 'from gobby.sessions.manager' src/gobby/ --include='*.py'
! grep -rn '_session_storage' src/gobby/ --include='*.py'
```

## End-to-end verification (after all phases)

1. **Line-count invariant:** `wc -l src/gobby/storage/sessions/*.py` — every file under 1000 LOC; mixin files under 400 LOC. Each Phase 1 task left `_manager.py` below 1000 LOC at its own commit boundary.
2. **Single public class:** `python -c "from gobby.storage.sessions import SessionManager; from gobby.storage import SessionManager as A; assert A is SessionManager"` succeeds; `from gobby.storage.sessions import LocalSessionManager` raises `ImportError`.
3. **Two-failure-semantics contract preserved:** `tests/storage/sessions/test_register_fallback.py` — `register_session()` returns UUID-str on DB failure; `register()` propagates the exception. Both contracts covered.
4. **Bug #12082 still fixed:** rewritten `tests/mcp_proxy/services/test_tool_proxy_validation.py::test_call_tool_emits_pipeline_source_when_session_is_pipeline` and `test_call_tool_logs_warning_and_defaults_source_when_storage_raises` green against the unified manager.
5. **No-dual-import invariant:** `tests/meta/test_import_hygiene.py::test_no_file_references_old_session_manager_names` green.
6. **Single HookManager attribute:** `tests/hooks/test_hooks_context.py` (or `tests/meta/`) — `test_hook_manager_has_single_session_attribute` green. Also `test_no_session_storage_attribute_in_hooks` passes.
7. **`update_session_status` preserved on unified class:** every caller in `hooks/event_handlers/_agent.py` and `hooks/event_handlers/_misc.py` continues to work against the unified `SessionManager`; no caller code change required.
8. **Non-hook `register()` raise-on-failure preserved:** `workflows/pipeline_executor.py:338-359` still catches `register()` failure and reuses caller session; `servers/routes/sessions/core.py:298-333` still returns HTTP 500 on failure. Smoke-test by running `tests/workflows/` and `tests/servers/routes/sessions/` subsets.
9. **HookManager construction path exercised:** start the daemon (`uv run gobby start --verbose`), confirm no warnings/errors about session-manager construction, run `uv run gobby sessions list`, verify output matches pre-refactor behavior.
10. **End-to-end hook flow:** open a Claude Code session against a clean project, confirm session registration → hook dispatch → MCP tool call with `source=CLAUDE` completes without falling through to `source=codex` default (the #12082 symptom).

## Task Mapping

<!-- Updated after task creation -->

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| 1.1 Create sessions package with constants, bootstrap, and CRUD mixin | | |
| 1.2 Extract Discovery and Field-update mixins | | |
| 1.3 Extract Bulk-update and Query mixins | | |
| 1.4 Extract Lifecycle-delegate, Transcript, Usage, Terminal mixins | | |
| 1.5 Phase-1 line-count invariant meta-check | | |
| 2.1 Absorb service-layer state and create the unified class | | |
| 2.2 Expose SessionManager via compat aliases and deprecation shims | | |
| 3.1 Wave 1 — agents/ subsystem | | |
| 3.2 Wave 2 — cli/ subsystem | | |
| 3.3 Wave 3 — mcp_proxy tools and registries | | |
| 3.4 Wave 4 — hooks/ subsystem: HookManager attribute unification + update_session_status migration | | |
| 3.5 Wave 5 — sessions/, servers/, utils/, runner, workflows | | |
| 3.6 Wave 6 — mcp_proxy/services/tool_proxy.py (the #12082 site) | | |
| 4.1 Remove LocalSessionManager alias and get_session compat shim | | |
| 4.2 Enforce single-SessionManager invariant via meta checks | | |
