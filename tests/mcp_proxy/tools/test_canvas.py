"""Unit tests for the canvas MCP tool registry."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from gobby.mcp_proxy.tools.canvas import (
    MAX_COMPONENT_COUNT,
    MAX_DATA_MODEL_SIZE,
    cancel_conversation_canvases,
    create_canvas_registry,
    get_canvas,
    resolve_interaction,
    sweep_expired,
)
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_canvas_state():
    """Reset the module-level state before and after each test."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    canvas_mod._canvases.clear()
    canvas_mod._canvas_locks.clear()
    canvas_mod._rate_counters.clear()
    canvas_mod._broadcaster_ref["func"] = None
    yield
    canvas_mod._canvases.clear()
    canvas_mod._canvas_locks.clear()
    canvas_mod._rate_counters.clear()
    canvas_mod._broadcaster_ref["func"] = None


class MockBroadcaster:
    def __init__(self):
        self.events = []

    async def __call__(self, **kwargs):
        self.events.append(kwargs)


@pytest.fixture
def broadcaster():
    return MockBroadcaster()


@pytest.fixture
def registry(broadcaster):
    return create_canvas_registry(broadcaster=broadcaster)


async def test_render_surface_success(registry, broadcaster):
    tool = registry.get_tool("render_surface")
    assert tool is not None

    components = {"text1": {"type": "Text", "content": "Hello"}}

    # Run in non-blocking mode so it returns immediately
    result = await tool(
        components=components,
        root_id="text1",
        canvas_id="canvas123",
        conversation_id="conv_1",
        blocking=False,
    )

    assert result["success"] is True
    assert result["canvas_id"] == "canvas123"

    # Check broadcast
    assert len(broadcaster.events) == 1
    event = broadcaster.events[0]
    assert event["event"] == "surface_update"
    assert event["canvas_id"] == "canvas123"
    assert event["conversation_id"] == "conv_1"

    # Check internal state
    canvas = get_canvas("canvas123")
    assert canvas is not None
    assert canvas.mode == "a2ui"
    assert canvas.surface == components
    assert canvas.data_model == {}
    assert canvas.conversation_id == "conv_1"


async def test_render_surface_blocking(registry):
    tool = registry.get_tool("render_surface")

    async def mock_interaction():
        await wait_for_async_condition(
            lambda: get_canvas("canvas-block") is not None,
            description="blocking canvas registration",
        )
        await resolve_interaction("canvas-block", {"type": "click"})

    task = asyncio.create_task(mock_interaction())

    result = await tool(
        components={"btn": {"type": "Button", "label": "Click Me"}},
        root_id="btn",
        canvas_id="canvas-block",
        conversation_id="conv_2",
        blocking=True,
    )

    await task

    assert result["success"] is True
    assert result["canvas_id"] == "canvas-block"
    assert result["action"] == {"type": "click"}


async def test_render_surface_validation(registry):
    tool = registry.get_tool("render_surface")

    # 1. Unknown component type
    result = await tool(
        components={"comp1": {"type": "UnknownBox"}},
        root_id="comp1",
        conversation_id="conv_val",
        blocking=False,
    )
    assert result["success"] is False
    assert "Unknown component type" in result["error"]

    # 2. Too many components
    components = {f"c{i}": {"type": "Text"} for i in range(MAX_COMPONENT_COUNT + 1)}
    result = await tool(
        components=components, root_id="c0", conversation_id="conv_val", blocking=False
    )
    assert result["success"] is False
    assert "Too many components" in result["error"]

    # 3. Data model too large
    large_data = {"key": "x" * (MAX_DATA_MODEL_SIZE + 10)}
    result = await tool(
        components={"c": {"type": "Text"}},
        root_id="c",
        data_model=large_data,
        conversation_id="conv_val",
        blocking=False,
    )
    assert result["success"] is False
    assert "Data model too large" in result["error"]


async def test_update_surface(registry, broadcaster):
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    # Setup initial canvas
    canvas_mod._canvases["canv-up"] = canvas_mod.CanvasState(
        canvas_id="canv-up",
        mode="a2ui",
        surface={"t": {"type": "Text", "content": "old"}},
        data_model={"count": 0},
        root_component_id="t",
        html_url=None,
        conversation_id="conv_up",
        pending_event=None,
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=False,
    )

    tool = registry.get_tool("update_surface")
    result = await tool(
        canvas_id="canv-up",
        components={"t": {"type": "Text", "content": "new"}, "btn": {"type": "Button"}},
        data_model={"count": 1},
        conversation_id="conv_up",
    )

    assert result["success"] is True

    # State should be updated
    state = get_canvas("canv-up")
    assert state.surface["t"]["content"] == "new"
    assert "btn" in state.surface
    assert state.data_model["count"] == 1

    # Broadcast should have fired
    assert len(broadcaster.events) == 1


async def test_close_canvas(registry, broadcaster):
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    # Setup initial canvas
    canvas_mod._canvases["canv-close"] = canvas_mod.CanvasState(
        canvas_id="canv-close",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv_close",
        pending_event=asyncio.Event(),
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=False,
    )

    tool = registry.get_tool("close_canvas")
    result = await tool(canvas_id="canv-close")
    assert result["success"] is True

    # Should be popped
    assert get_canvas("canv-close") is None

    # Pending event should be set
    assert (
        canvas_mod._canvases.get("canv-close") is None
    )  # wait, state object was modified before popping

    # Broadcast fired
    assert len(broadcaster.events) == 1
    assert broadcaster.events[0]["event"] == "close_canvas"


async def test_cancel_conversation_canvases():
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    state1 = canvas_mod.CanvasState(
        canvas_id="c1",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv-cancel",
        pending_event=asyncio.Event(),
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=False,
    )
    state2 = canvas_mod.CanvasState(
        canvas_id="c2",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv-other",
        pending_event=asyncio.Event(),
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=False,
    )
    canvas_mod._canvases["c1"] = state1
    canvas_mod._canvases["c2"] = state2

    count = cancel_conversation_canvases("conv-cancel")
    assert count == 1

    assert state1.completed is True
    assert state1.interaction_result == {"error": "conversation_cancelled"}
    assert state1.pending_event.is_set()

    assert state2.completed is False


async def test_render_surface_with_context_conversation_id(registry):
    """SessionContext conversation_id should be used when conversation_id param is not passed."""
    from gobby.utils.session_context import session_context_for_test

    tool = registry.get_tool("render_surface")

    with session_context_for_test("sess-1", conversation_id="conv-from-ctx"):
        result = await tool(
            components={"t": {"type": "Text"}},
            root_id="t",
            blocking=False,
        )

    assert result["success"] is True
    canvas = get_canvas(result["canvas_id"])
    assert canvas is not None
    assert canvas.conversation_id == "conv-from-ctx"


async def test_render_surface_with_context_session_id_fallback(registry):
    """SessionContext session_id should be used as fallback when conversation_id is absent."""
    from gobby.utils.session_context import session_context_for_test

    tool = registry.get_tool("render_surface")

    with session_context_for_test("sess-fallback"):
        result = await tool(
            components={"t": {"type": "Text"}},
            root_id="t",
            blocking=False,
        )

    assert result["success"] is True
    canvas = get_canvas(result["canvas_id"])
    assert canvas is not None
    assert canvas.conversation_id == "sess-fallback"


async def test_render_surface_explicit_conversation_id_takes_priority(registry):
    """Explicit conversation_id param should take priority over SessionContext."""
    from gobby.utils.session_context import session_context_for_test

    tool = registry.get_tool("render_surface")

    with session_context_for_test("sess-1", conversation_id="conv-from-ctx"):
        result = await tool(
            components={"t": {"type": "Text"}},
            root_id="t",
            conversation_id="explicit-conv",
            blocking=False,
        )

    assert result["success"] is True
    canvas = get_canvas(result["canvas_id"])
    assert canvas is not None
    assert canvas.conversation_id == "explicit-conv"


async def test_sweep_expired():
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    now = datetime.now(UTC)

    canvas_mod._canvases["expired"] = canvas_mod.CanvasState(
        canvas_id="expired",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="c1",
        pending_event=None,
        interaction_result=None,
        created_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=5),
        completed=False,
    )

    canvas_mod._canvases["valid"] = canvas_mod.CanvasState(
        canvas_id="valid",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="c1",
        pending_event=None,
        interaction_result=None,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        completed=False,
    )

    count = sweep_expired()
    assert count == 1
    assert "expired" not in canvas_mod._canvases
    assert "valid" in canvas_mod._canvases


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------


async def test_render_surface_no_conversation_id(registry):
    """render_surface without conversation_id or _context returns error."""
    tool = registry.get_tool("render_surface")
    result = await tool(
        components={"t": {"type": "Text"}},
        root_id="t",
        blocking=False,
    )
    assert result["success"] is False
    assert "conversation_id" in result["error"]


async def test_render_surface_missing_type(registry):
    """Component missing 'type' key should fail validation."""
    tool = registry.get_tool("render_surface")
    result = await tool(
        components={"c1": {"content": "no type field"}},
        root_id="c1",
        conversation_id="conv_1",
        blocking=False,
    )
    assert result["success"] is False
    assert "missing 'type'" in result["error"]


async def test_render_surface_rate_limit(registry):
    """Rate limiting should block after MAX_RENDER_RATE calls."""
    import time

    from gobby.mcp_proxy.tools import canvas as canvas_mod

    tool = registry.get_tool("render_surface")
    # Fill rate counter
    canvas_mod._rate_counters["conv-rl"] = [time.time()] * 10

    result = await tool(
        components={"t": {"type": "Text"}},
        root_id="t",
        conversation_id="conv-rl",
        blocking=False,
    )
    assert result["success"] is False
    assert "Rate limit" in result["error"]


async def test_render_surface_max_canvases(registry):
    """Should fail when max total canvases is reached."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    now = datetime.now(UTC)
    # Fill with non-expired canvases
    for i in range(1000):
        canvas_mod._canvases[f"c-{i}"] = canvas_mod.CanvasState(
            canvas_id=f"c-{i}",
            mode="a2ui",
            surface={},
            data_model={},
            root_component_id=None,
            html_url=None,
            conversation_id="other",
            pending_event=None,
            interaction_result=None,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            completed=False,
        )

    tool = registry.get_tool("render_surface")
    result = await tool(
        components={"t": {"type": "Text"}},
        root_id="t",
        conversation_id="conv-max",
        blocking=False,
    )
    assert result["success"] is False
    assert "Too many active canvases" in result["error"]


async def test_update_surface_not_found(registry):
    """update_surface on nonexistent canvas returns error."""
    tool = registry.get_tool("update_surface")
    result = await tool(
        canvas_id="nonexistent",
        conversation_id="conv_1",
    )
    assert result["success"] is False
    assert "Canvas not found" in result["error"]


async def test_update_surface_completed(registry):
    """update_surface on completed canvas returns error."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    canvas_mod._canvases["done"] = canvas_mod.CanvasState(
        canvas_id="done",
        mode="a2ui",
        surface={"t": {"type": "Text"}},
        data_model={},
        root_component_id="t",
        html_url=None,
        conversation_id="conv_done",
        pending_event=None,
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=True,
    )

    tool = registry.get_tool("update_surface")
    result = await tool(
        canvas_id="done",
        conversation_id="conv_done",
    )
    assert result["success"] is False
    assert "already completed" in result["error"]


async def test_update_surface_no_conversation_id(registry):
    """update_surface without conversation_id returns error."""
    tool = registry.get_tool("update_surface")
    result = await tool(canvas_id="some-id")
    assert result["success"] is False
    assert "conversation_id" in result["error"]


async def test_close_canvas_not_found(registry):
    """close_canvas on nonexistent canvas returns error."""
    tool = registry.get_tool("close_canvas")
    result = await tool(canvas_id="nonexistent")
    assert result["success"] is False
    assert "Canvas not found" in result["error"]


async def test_wait_for_interaction_not_found(registry):
    """wait_for_interaction on nonexistent canvas returns error."""
    tool = registry.get_tool("wait_for_interaction")
    result = await tool(canvas_id="nonexistent")
    assert result["success"] is False
    assert "Canvas not found" in result["error"]


async def test_wait_for_interaction_already_completed(registry):
    """wait_for_interaction on completed canvas returns result immediately."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    canvas_mod._canvases["wf-done"] = canvas_mod.CanvasState(
        canvas_id="wf-done",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv_wf",
        pending_event=None,
        interaction_result={"type": "submit"},
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=True,
    )

    tool = registry.get_tool("wait_for_interaction")
    result = await tool(canvas_id="wf-done")
    assert result["success"] is True
    assert result["action"] == {"type": "submit"}


async def test_wait_for_interaction_timeout(registry):
    """wait_for_interaction should time out and return error."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    canvas_mod._canvases["wf-timeout"] = canvas_mod.CanvasState(
        canvas_id="wf-timeout",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv_to",
        pending_event=None,
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=False,
    )

    tool = registry.get_tool("wait_for_interaction")
    result = await tool(canvas_id="wf-timeout", timeout=0.01)
    assert result["success"] is False
    assert "timeout" in result["error"].lower()


async def test_canvas_present_no_conversation_id(registry):
    """canvas_present without conversation_id returns error."""
    tool = registry.get_tool("canvas_present")
    result = await tool(file_path="/tmp/test.html")
    assert result["success"] is False
    assert "conversation_id" in result["error"]


async def test_canvas_present_invalid_path(registry):
    """canvas_present with non-absolute or missing file returns error."""
    tool = registry.get_tool("canvas_present")
    result = await tool(file_path="relative/path.html", conversation_id="conv_1")
    assert result["success"] is False
    assert "Invalid absolute file path" in result["error"]


async def test_show_file_no_conversation_id(registry):
    """show_file without conversation_id returns error."""
    tool = registry.get_tool("show_file")
    result = await tool(file_path="/tmp/test.py")
    assert result["success"] is False
    assert "conversation_id" in result["error"]


async def test_show_file_not_absolute(registry):
    """show_file with relative path returns error."""
    tool = registry.get_tool("show_file")
    result = await tool(file_path="relative.py", conversation_id="conv_1")
    assert result["success"] is False
    assert "absolute" in result["error"]


async def test_show_file_not_found(registry):
    """show_file with nonexistent file returns error."""
    tool = registry.get_tool("show_file")
    result = await tool(file_path="/nonexistent/file.py", conversation_id="conv_1")
    assert result["success"] is False
    assert "not found" in result["error"].lower() or "absolute" in result["error"].lower()


async def test_show_file_success(registry, tmp_path):
    """show_file with a real file should succeed."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    test_file = tmp_path / "test.py"
    test_file.write_text("print('hello')")

    bc = MockBroadcaster()
    canvas_mod._artifact_broadcaster_ref["func"] = bc

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(test_file), conversation_id="conv_sf")
    assert result["success"] is True
    assert result["type"] == "code"
    assert result["language"] == "python"
    assert result["title"] == "test.py"


async def test_show_file_markdown(registry, tmp_path):
    """show_file with markdown file returns text type."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    test_file = tmp_path / "readme.md"
    test_file.write_text("# Hello")

    canvas_mod._artifact_broadcaster_ref["func"] = MockBroadcaster()

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(test_file), conversation_id="conv_md")
    assert result["success"] is True
    assert result["type"] == "text"
    assert result["language"] == "markdown"


async def test_show_file_too_large(registry, tmp_path):
    """show_file with oversized file returns error."""
    test_file = tmp_path / "large.py"
    test_file.write_text("x" * (1024 * 1024 + 1))  # >1MB

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(test_file), conversation_id="conv_big")
    assert result["success"] is False
    assert "too large" in result["error"].lower()


async def test_resolve_interaction_returns_false_for_nonexistent():
    """resolve_interaction on nonexistent canvas returns False."""
    result = await resolve_interaction("nonexistent", {"type": "click"})
    assert result is False


async def test_resolve_interaction_returns_false_for_completed():
    """resolve_interaction on completed canvas returns False."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    canvas_mod._canvases["ri-done"] = canvas_mod.CanvasState(
        canvas_id="ri-done",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv_ri",
        pending_event=None,
        interaction_result=None,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        completed=True,
    )

    result = await resolve_interaction("ri-done", {"type": "click"})
    assert result is False


async def test_sweep_expired_with_pending_event():
    """Expired canvas with pending_event should have it set."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod

    now = datetime.now(UTC)
    pending = asyncio.Event()
    canvas_mod._canvases["exp-pending"] = canvas_mod.CanvasState(
        canvas_id="exp-pending",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="c1",
        pending_event=pending,
        interaction_result=None,
        created_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=1),
        completed=False,
    )

    count = sweep_expired()
    assert count == 1
    assert pending.is_set()


def test_set_broadcaster():
    """set_broadcaster should update the broadcaster ref."""
    from gobby.mcp_proxy.tools.canvas import _broadcaster_ref, set_broadcaster

    async def my_bc(**kwargs):
        pass

    set_broadcaster(my_bc)
    assert _broadcaster_ref["func"] is my_bc
    set_broadcaster(None)
    assert _broadcaster_ref["func"] is None


def test_get_active_canvases():
    """get_active_canvases returns only active canvases for conversation."""
    from gobby.mcp_proxy.tools import canvas as canvas_mod
    from gobby.mcp_proxy.tools.canvas import get_active_canvases

    now = datetime.now(UTC)
    canvas_mod._canvases["active1"] = canvas_mod.CanvasState(
        canvas_id="active1",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv-ac",
        pending_event=None,
        interaction_result=None,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        completed=False,
    )
    canvas_mod._canvases["completed1"] = canvas_mod.CanvasState(
        canvas_id="completed1",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv-ac",
        pending_event=None,
        interaction_result=None,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        completed=True,
    )
    canvas_mod._canvases["other-conv"] = canvas_mod.CanvasState(
        canvas_id="other-conv",
        mode="a2ui",
        surface={},
        data_model={},
        root_component_id=None,
        html_url=None,
        conversation_id="conv-other",
        pending_event=None,
        interaction_result=None,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        completed=False,
    )

    result = get_active_canvases("conv-ac")
    assert len(result) == 1
    assert result[0].canvas_id == "active1"
