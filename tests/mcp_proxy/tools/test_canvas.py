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

    # Start a mock interaction in the background that fires after a small delay
    async def mock_interaction():
        await asyncio.sleep(0.05)
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
    """_context.conversation_id should be used when conversation_id param is not passed."""
    import types

    tool = registry.get_tool("render_surface")
    ctx = types.SimpleNamespace(session_id="sess-1", conversation_id="conv-from-ctx")

    result = await tool(
        components={"t": {"type": "Text"}},
        root_id="t",
        blocking=False,
        _context=ctx,
    )

    assert result["success"] is True
    canvas = get_canvas(result["canvas_id"])
    assert canvas is not None
    assert canvas.conversation_id == "conv-from-ctx"


async def test_render_surface_with_context_session_id_fallback(registry):
    """_context.session_id should be used as fallback when conversation_id is absent."""
    import types

    tool = registry.get_tool("render_surface")
    ctx = types.SimpleNamespace(session_id="sess-fallback")

    result = await tool(
        components={"t": {"type": "Text"}},
        root_id="t",
        blocking=False,
        _context=ctx,
    )

    assert result["success"] is True
    canvas = get_canvas(result["canvas_id"])
    assert canvas is not None
    assert canvas.conversation_id == "sess-fallback"


async def test_render_surface_explicit_conversation_id_takes_priority(registry):
    """Explicit conversation_id param should take priority over _context."""
    import types

    tool = registry.get_tool("render_surface")
    ctx = types.SimpleNamespace(session_id="sess-1", conversation_id="conv-from-ctx")

    result = await tool(
        components={"t": {"type": "Text"}},
        root_id="t",
        conversation_id="explicit-conv",
        blocking=False,
        _context=ctx,
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
