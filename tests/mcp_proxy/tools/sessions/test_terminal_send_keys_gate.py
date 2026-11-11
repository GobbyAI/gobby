"""The target checks an allowed ``send_keys`` caller must pass before any key is written."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.detection.safe_regex import RegexOutcome, RegexSearchResult, SafeRegex
from gobby.events.wake import CONTINUE_WAKE_MESSAGE
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import _terminal_send_keys_gate as gate
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import register_send_keys_tool
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals.composer_ledger import ComposerLedger, bind_composer_ledger, read_composer
from gobby.terminals.pane_io import SendResult, composer_gate_for_write
from gobby.terminals.runtime import Delivered, NamedKey, SnapshotMode
from gobby.utils.dependency_requirements import SRT_RELEASE
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminal_scene import install_agent_clis

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("script_kind", ["installed", "linked", "declared-package", "missing"])
@pytest.mark.parametrize("seat", ["own", "unrecorded", "impostor", "background"])
@pytest.mark.parametrize("busy", [False, True])
async def test_node_claude_counts_only_as_the_seats_own_foreground_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script_kind: str, seat: str, busy: bool
) -> None:
    # Any directory can declare itself @anthropic-ai/claude-code with a bin naming its
    # script, so only the script the daemon's PATH resolves claude to names claude. The
    # script is a path read again at check time: a link node opened while it named another
    # file, then pointed at the installed entrypoint, reads like ``linked``. So node counts
    # only when it is the target session's recorded CLI process leading the foreground.
    installed = tmp_path / "npm" / "claude-code" / "cli.js"
    _install(monkeypatch, "claude", installed)
    planted = tmp_path / "planted" / "cli.js"
    planted.parent.mkdir()
    planted.write_text("// planted\n")
    (planted.parent / "package.json").write_text(
        '{"name":"@anthropic-ai/claude-code","bin":{"claude":"cli.js"}}'
    )
    linked = tmp_path / "bin" / "claude"
    linked.parent.mkdir()
    linked.symlink_to(installed)
    script = {
        "installed": installed,
        "linked": linked,
        "declared-package": planted,
        "missing": tmp_path / "missing" / "cli.js",
    }[script_kind]
    _seat_process(
        monkeypatch,
        Path("/usr/local/bin/node"),
        identity=seat != "impostor",
        group=FOREGROUND_GROUP + 1 if seat == "background" else FOREGROUND_GROUP,
    )
    manager = _scene(
        monkeypatch,
        foreground={TERMINAL_ID: "/usr/local/bin/node"},
        snapshot=HOOK_DENIAL_OVER_LIVE_TURN if busy else IDLE_COMPOSER,
    )
    monkeypatch.setattr(
        gate, "foreground_node_script", lambda pid: str(script) if pid == 4321 else None
    )

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID,
        manager,
        object(),
        terminal_context=None if seat == "unrecorded" else SEAT_CONTEXT,
    )

    admitted = script_kind in ("installed", "linked") and seat == "own"
    if admitted and not busy:
        assert refusal is None
    else:
        assert isinstance(refusal, gate.SendKeysRefusal)
        assert refusal.code == (
            "send_keys_composer_not_idle" if admitted else "send_keys_target_unconfirmed"
        )


SESSION_ID = "33333333-3333-4333-8333-333333333333"
TERMINAL_ID = "terminal-1"
# The framed Claude composer the provider manifest reads as empty (corpus claude.composer_empty).
IDLE_COMPOSER = "────────────────────\n❯ \n────────────────────"
# Hook-denial scrollback that the bundled stop_hook_blocked rule matches first.
STOP_HOOK_DENIAL = "Stop hook error: validation failed"
GOBBY_RULE_DENIAL = "Rule enforced by Gobby: validation required"
LIVE_SPINNER = "✻ Thinking… (12s · esc to interrupt)"
# A hook denial scrolled above a live turn, with the framed empty composer below it.
HOOK_DENIAL_OVER_LIVE_TURN = f"{STOP_HOOK_DENIAL}\n{LIVE_SPINNER}\n{IDLE_COMPOSER}"


class _Manager:
    def __init__(self, terminal: object | None, error: Exception | None) -> None:
        self._terminal = terminal
        self._error = error

    def get_live_for_session(self, session_id: str) -> object | None:
        if self._error is not None:
            raise self._error
        return self._terminal

    def resolve_live_for_session(self, session: object) -> object | None:
        return self._terminal


class _Pane:
    backend = "fake"
    target = TERMINAL_ID

    def __init__(self, text: str | None) -> None:
        self._text = text
        self.keys: list[str] = []

    async def snapshot(self, lines: int = 80, *, mode: SnapshotMode = "text") -> str | None:
        return self._text

    async def send_key(self, key: NamedKey) -> SendResult:
        self.keys.append(key)
        return True, None

    async def type_text(self, text: str) -> SendResult:
        raise AssertionError(f"the gate never types text: {text!r}")


def _terminal(pgid: int | None = 4321) -> SimpleNamespace:
    return SimpleNamespace(id=TERMINAL_ID, process={} if pgid is None else {"pgid": pgid})


@pytest.fixture(autouse=True)
def _installed_clis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_agent_clis(monkeypatch, tmp_path)


def _installed_executable(name: str) -> str:
    """An agent CLI name as the build the gate's PATH lookup finds; anything else as given."""
    installed = gate._installed_cli(name) if name in gate._AGENT_CLIS else None
    return name if installed is None else str(installed)


def _scene(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> _Manager:
    """Install an idle agent CLI in the foreground, then apply ``overrides``."""
    scene: dict[str, Any] = {
        "terminal": _terminal(),
        "foreground": {TERMINAL_ID: "claude"},
        "composer": "empty",
        "snapshot": IDLE_COMPOSER,
        "pane": True,
        "lookup_error": None,
    } | overrides
    pane = _Pane(scene["snapshot"]) if scene["pane"] else None
    foreground = {
        terminal_id: _installed_executable(name)
        for terminal_id, name in scene["foreground"].items()
    }
    monkeypatch.setattr(gate, "foreground_executables", lambda pids: foreground if pids else {})
    if scene["composer"] is not None:  # None reads the bound composer ledger
        monkeypatch.setattr(
            gate, "read_composer", lambda _id: SimpleNamespace(state=scene["composer"])
        )
    monkeypatch.setattr(gate, "live_runtime_pane", lambda *_args: scene.get("live_pane", pane))
    monkeypatch.setattr(gate, "_detection_registry", lambda _manager: BundledDetectionRegistry())
    return _Manager(scene["terminal"], scene["lookup_error"])


REFUSALS = [
    pytest.param({"terminal": None}, "send_keys_no_live_terminal", id="no-live-terminal"),
    pytest.param({"terminal": _terminal(pgid=None)}, "send_keys_target_unconfirmed", id="tmux-pid"),
    pytest.param({"foreground": {}}, "send_keys_target_unconfirmed", id="foreground-unread"),
    pytest.param(
        {"foreground": {TERMINAL_ID: "zsh"}}, "send_keys_target_bare_shell", id="bare-shell"
    ),
    pytest.param({"composer": "draft"}, "send_keys_composer_not_idle", id="human-draft"),
    pytest.param({"composer": "held"}, "send_keys_composer_not_idle", id="daemon-held-text"),
    pytest.param({"composer": "unknown"}, "send_keys_composer_not_idle", id="untracked"),
    pytest.param({"pane": False}, "send_keys_target_unconfirmed", id="pane-missing"),
    pytest.param({"snapshot": None}, "send_keys_target_unconfirmed", id="snapshot-unread"),
    pytest.param({"snapshot": ""}, "send_keys_target_unconfirmed", id="empty-pane"),
    pytest.param({"snapshot": " \n\t"}, "send_keys_target_unconfirmed", id="blank-pane"),
    pytest.param(
        {"foreground": {TERMINAL_ID: "vim"}},
        "send_keys_target_unconfirmed",
        id="non-agent-foreground",
    ),
    pytest.param(
        {"foreground": {TERMINAL_ID: "python"}, "snapshot": IDLE_COMPOSER},
        "send_keys_target_unconfirmed",
        id="exited-cli-stale-composer",
    ),
    pytest.param({"snapshot": "Password:"}, "send_keys_credential_prompt", id="password"),
    pytest.param(
        {"snapshot": "[sudo] password for josh:"}, "send_keys_credential_prompt", id="sudo"
    ),
    pytest.param(
        {"snapshot": "Enter passphrase for key '/home/josh/.ssh/id':"},
        "send_keys_credential_prompt",
        id="passphrase",
    ),
    pytest.param({"snapshot": "Enter API key:"}, "send_keys_credential_prompt", id="api-key"),
    pytest.param(
        {"snapshot": "Thinking..."}, "send_keys_composer_not_idle", id="busy-cli-no-composer"
    ),
    pytest.param(
        {"snapshot": IDLE_COMPOSER + "\nRunning tests... esc to interrupt"},
        "send_keys_composer_not_idle",
        id="stale-composer-live-turn",
    ),
    pytest.param(
        {"snapshot": IDLE_COMPOSER + "\nRunning tests..."},
        "send_keys_composer_not_idle",
        id="stale-composer-active-work",
    ),
    pytest.param(
        {"snapshot": IDLE_COMPOSER + "\nesc to interrupt"},
        "send_keys_composer_not_idle",
        id="stale-composer-turn-in-flight",
    ),
    pytest.param(
        {"snapshot": f"{STOP_HOOK_DENIAL}\n{IDLE_COMPOSER}\nesc to interrupt"},
        "send_keys_composer_not_idle",
        id="stop-hook-over-live-turn",
    ),
    pytest.param(
        {"snapshot": f"{STOP_HOOK_DENIAL}\n{IDLE_COMPOSER}\nRunning tests..."},
        "send_keys_composer_not_idle",
        id="stop-hook-over-active-work",
    ),
    pytest.param(
        {"snapshot": f"{GOBBY_RULE_DENIAL}\n{IDLE_COMPOSER}\nesc to interrupt"},
        "send_keys_composer_not_idle",
        id="gobby-rule-over-live-turn",
    ),
    pytest.param(
        {"snapshot": f"{GOBBY_RULE_DENIAL}\n{IDLE_COMPOSER}\nRunning tests..."},
        "send_keys_composer_not_idle",
        id="gobby-rule-over-active-work",
    ),
    pytest.param(
        {"snapshot": HOOK_DENIAL_OVER_LIVE_TURN},
        "send_keys_composer_not_idle",
        id="hook-denial-over-spinner-above-frame",
    ),
    pytest.param(
        {"lookup_error": RuntimeError("terminal table unavailable")},
        "send_keys_target_unconfirmed",
        id="lookup-raises",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("overrides", "code"), REFUSALS)
async def test_refuses_a_target_that_cannot_take_keys(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], code: str
) -> None:
    manager = _scene(monkeypatch, **overrides)

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == code
    assert "gobby-agents:send_message" in refusal.message


@pytest.mark.asyncio
async def test_admits_a_target_showing_an_idle_agent_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _scene(monkeypatch)

    assert (
        await gate.send_keys_target_refusal(SESSION_ID, manager, object(), terminal_context=None)
        is None
    )


def _install(monkeypatch: pytest.MonkeyPatch, cli: str, build: Path) -> None:
    """Make ``build`` the one installed build the gate's PATH lookup finds, for ``cli`` only."""
    build.parent.mkdir(parents=True, exist_ok=True)
    build.write_bytes(b"")
    monkeypatch.setattr(gate, "_installed_cli", lambda name: build if name == cli else None)


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# The seat's recorded CLI process, and the foreground group its terminal shows. Synthetic.
SEAT_PID = 5150
FOREGROUND_GROUP = 5150
SEAT_CONTEXT = {"parent_pid": SEAT_PID, "parent_create_time": 1.0, "parent_name": "seat"}


def _seat_process(
    monkeypatch: pytest.MonkeyPatch,
    runs: Path,
    *,
    identity: bool = True,
    group: int = FOREGROUND_GROUP,
) -> None:
    """Script the recorded seat process: its identity check, group and on-disk build."""
    monkeypatch.setattr(
        gate, "process_identity_matches", lambda pid, context: identity and pid == SEAT_PID
    )
    monkeypatch.setattr(
        gate,
        "group_and_executable",
        lambda pid: (group, str(runs)) if pid == SEAT_PID else None,
    )
    monkeypatch.setattr(gate, "foreground_group", lambda shell: FOREGROUND_GROUP)


def _seat() -> gate._Seat:
    return gate._Seat(SEAT_CONTEXT, FOREGROUND_GROUP)


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ((".grok", "downloads", "grok-1.0.78-macos-aarch64"), "grok"),
        ((".grok", "downloads", "grok-1.0.77-macos-aarch64"), "grok"),
        ((".grok", "downloads", "grok-macos-aarch64"), None),
        (("tmp", ".grok", "downloads", "grok-1.0.77-macos-aarch64"), None),
        ((".grok", "bin", "grok-1.0.77-macos-aarch64"), None),
    ],
    ids=["installed", "older-sibling", "unversioned", "look-alike-tree", "wrong-dir"],
)
def test_only_the_installed_grok_or_the_seats_own_sibling_build_is_an_agent_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parts: tuple[str, ...], expected: str | None
) -> None:
    # A renamed shell at /tmp/.grok/downloads/grok-<v>-<os>-<arch> has the installer's
    # shape but not its directory, so it is no agent CLI, even as the seat's process.
    _install(monkeypatch, "grok", tmp_path / ".grok" / "downloads" / "grok-1.0.78-macos-aarch64")
    build = _file(tmp_path.joinpath(*parts))
    _seat_process(monkeypatch, build)

    assert gate._agent_cli(str(build), _seat()) == expected
    assert gate._agent_cli(str(tmp_path / "missing" / build.name), _seat()) is None


@pytest.mark.parametrize(
    "seat",
    ["none", "identity-mismatch", "background-group", "runs-the-current-build", "unreadable"],
)
def test_a_planted_versioned_sibling_is_no_agent_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seat: str
) -> None:
    # Anyone who can write the installer directory can copy a shell there under a version
    # name. It counts only as the session's recorded CLI process in the foreground.
    installed = tmp_path / ".grok" / "downloads" / "grok-1.0.78-macos-aarch64"
    _install(monkeypatch, "grok", installed)
    planted = _file(tmp_path / ".grok" / "downloads" / "grok-9.9.9-macos-aarch64")
    if seat == "identity-mismatch":
        _seat_process(monkeypatch, planted, identity=False)
    elif seat == "background-group":
        _seat_process(monkeypatch, planted, group=FOREGROUND_GROUP + 1)
    elif seat == "runs-the-current-build":
        _seat_process(monkeypatch, installed)
    else:
        _seat_process(monkeypatch, planted)
    if seat == "unreadable":
        monkeypatch.setattr(gate, "group_and_executable", lambda pid: None)

    assert gate._agent_cli(str(planted), None if seat == "none" else _seat()) is None


@pytest.mark.parametrize("cli", sorted(gate._AGENT_CLIS))
def test_a_program_named_like_an_agent_cli_is_not_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli: str
) -> None:
    # A shell copied or linked to ``<cli>`` anywhere but the installed build is never typed into.
    _install(monkeypatch, cli, tmp_path / "installed" / cli)
    planted = _file(tmp_path / "planted" / cli)
    linked = tmp_path / "linked" / cli
    linked.parent.mkdir()
    linked.symlink_to(planted)

    assert gate._agent_cli(str(tmp_path / "installed" / cli), None) == cli
    assert gate._agent_cli(str(planted), None) is None
    assert gate._agent_cli(str(linked), None) is None
    assert gate._agent_cli(cli, None) is None


@pytest.mark.asyncio
async def test_a_password_line_above_an_idle_prompt_is_not_a_credential_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _scene(monkeypatch, snapshot=f"Password: \n{IDLE_COMPOSER}")

    assert (
        await gate.send_keys_target_refusal(SESSION_ID, manager, object(), terminal_context=None)
        is None
    )


@pytest.mark.asyncio
async def test_missing_manager_or_registry_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _scene(monkeypatch)

    no_manager = await gate.send_keys_target_refusal(
        SESSION_ID, None, object(), terminal_context=None
    )
    no_registry = await gate.send_keys_target_refusal(
        SESSION_ID, _Manager(_terminal(), None), None, terminal_context=None
    )

    assert isinstance(no_manager, gate.SendKeysRefusal)
    assert no_manager.code == "send_keys_target_unconfirmed"
    assert isinstance(no_registry, gate.SendKeysRefusal)
    assert no_registry.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("via_symlink", [False, True], ids=["direct", "symlink"])
async def test_admits_an_idle_composer_on_the_seats_own_versioned_claude_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, via_symlink: bool
) -> None:
    # The session still runs 2.1.314 after the installer moved the launcher to 2.1.320.
    _install(monkeypatch, "claude", tmp_path / "claude" / "versions" / "2.1.320")
    build = _file(tmp_path / "claude" / "versions" / "2.1.314")
    executable = build
    if via_symlink:
        executable = tmp_path / "bin" / "cli"
        executable.parent.mkdir()
        executable.symlink_to(build)
    _seat_process(monkeypatch, executable)
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: str(executable)})

    assert (
        await gate.send_keys_target_refusal(
            SESSION_ID, manager, object(), terminal_context=SEAT_CONTEXT
        )
        is None
    )
    # Without the session's recorded process, the superseded build proves nothing.
    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )
    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative",
    ["other/versions/2.1.314", "claude/versions/helper", "tmp/claude/versions/2.1.314"],
    ids=["other-tree", "non-version-name", "look-alike-tree"],
)
async def test_refuses_an_unknown_versioned_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative: str
) -> None:
    _install(monkeypatch, "claude", tmp_path / "claude" / "versions" / "2.1.320")
    build = _file(tmp_path / relative)
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: str(build)})

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
async def test_refuses_a_versioned_path_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, "claude", tmp_path / "claude" / "versions" / "2.1.320")
    missing = tmp_path / "claude" / "versions" / "2.1.314"
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: str(missing)})

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_target_unconfirmed"


@pytest.fixture
def busy_rule_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every turn_in_flight pattern report PATTERN_TIMEOUT, as a runaway regex would."""
    real_search = SafeRegex.search

    def search(self: SafeRegex, text: str) -> RegexSearchResult:
        if "esc to interrupt" in self.source:
            return RegexSearchResult(RegexOutcome.PATTERN_TIMEOUT)
        return real_search(self, text)

    monkeypatch.setattr(SafeRegex, "search", search)


@pytest.mark.asyncio
async def test_a_busy_rule_timeout_over_a_hook_denial_refuses(
    monkeypatch: pytest.MonkeyPatch, busy_rule_times_out: None
) -> None:
    manager = _scene(monkeypatch, snapshot=HOOK_DENIAL_OVER_LIVE_TURN)

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_composer_not_idle"


@pytest.mark.asyncio
async def test_public_send_keys_refuses_a_busy_rule_timeout_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
    busy_rule_times_out: None,
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch, snapshot=HOOK_DENIAL_OVER_LIVE_TURN))

    result = await handler(session_id=target_id, keys="resume\n")

    assert result["success"] is False
    assert result["error_code"] == "send_keys_composer_not_idle"
    writer.assert_not_awaited()


@pytest.fixture
def public_send_keys(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> tuple[Callable[..., Any], AsyncMock, str]:
    """Register the public handler with a real caller and an isolated write sink."""
    operator = getattr(request, "param", "capable") == "operator"
    caller = session_manager.register(
        external_id="send-keys-public-caller",
        machine_id=require_machine_id(),
        source="claude",
        project_id=sample_project["id"],
        agent_depth=0 if operator else 1,
    )
    if not operator:
        SessionVariableManager(temp_db).set_variable(caller.id, "_agent_type", "fixture-seat")
    target = session_manager.register(
        external_id="send-keys-public-target",
        machine_id=require_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    )
    monkeypatch.setattr("gobby.utils.session_context.get_current_session_id", lambda: caller.id)
    registry = InternalToolRegistry(name="test-send-keys")
    writer = AsyncMock(return_value=Delivered())

    def register(manager: _Manager) -> Callable[..., Any]:
        register_send_keys_tool(
            registry,
            session_manager,
            terminal_manager=manager,
            terminal_runtime_registry=object(),
            write_coordinator=SimpleNamespace(write=writer),
        )
        handler = registry.get_tool("send_keys")
        assert handler is not None
        return handler

    return register, writer, target.id


@pytest.mark.asyncio
@pytest.mark.parametrize(("overrides", "code"), REFUSALS)
async def test_public_send_keys_refusal_never_writes(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
    overrides: dict[str, Any],
    code: str,
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch, **overrides))

    result = await handler(session_id=target_id, keys="resume\n")

    assert result["success"] is False
    assert result["error_code"] == code
    assert "gobby-agents:send_message" in result["error"]
    writer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("public_send_keys", ["capable", "operator"], indirect=True)
async def test_public_send_keys_delivers_to_idle_cli_composer(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch))

    result = await handler(session_id=target_id, keys="resume\n")

    assert result["success"] is True
    writer.assert_awaited_once()
    write = writer.await_args
    assert write is not None
    assert write.args[0].terminal_id == TERMINAL_ID
    assert write.args[0].payload == "resume"
    assert write.args[0].submit is True


@pytest.mark.asyncio
@pytest.mark.parametrize("public_send_keys", ["operator"], indirect=True)
async def test_public_send_keys_applies_the_composer_gate_to_the_operator(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch, foreground={TERMINAL_ID: "zsh"}))

    result = await handler(session_id=target_id, keys="echo operator\n")

    assert result["success"] is False
    assert result["error_code"] == "send_keys_target_bare_shell"
    assert "gobby-agents:send_message" in result["error"]
    writer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("composer", "route"),
    [
        ("held", "send_keys(keys='ctrl_u', literal=false)"),
        ("draft", "gobby-sessions:release_composer"),
        ("unknown", "gobby-sessions:release_composer"),
    ],
)
async def test_occupied_composer_refusal_names_a_recovery_route(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
    composer: str,
    route: str,
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch, composer=composer))

    result = await handler(session_id=target_id, keys="resume\n")

    assert result["error_code"] == "send_keys_composer_not_idle"
    assert route in result["recovery"]
    writer.assert_not_awaited()


@pytest.fixture
def held_compact() -> Iterator[ComposerLedger]:
    """Bind a ledger whose composer holds a daemon-typed, unsubmitted ``/compact``."""
    ledger = ComposerLedger()
    ledger.release(TERMINAL_ID)
    ledger.observe_write(TERMINAL_ID, origin="daemon", kind="text", payload="/compact")
    bind_composer_ledger(ledger)
    assert read_composer(TERMINAL_ID).state == "held"
    yield ledger
    bind_composer_ledger(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", ["ctrl_u", "C-u"])
async def test_clear_key_drains_a_held_compact_so_a_later_handoff_does_not_replay_it(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
    held_compact: ComposerLedger,
    keys: str,
) -> None:
    register, writer, target_id = public_send_keys
    pane = _Pane(IDLE_COMPOSER)
    handler = register(_scene(monkeypatch, composer=None, live_pane=pane))

    result = await handler(session_id=target_id, keys=keys, literal=False)

    assert result["success"] is True
    assert result["cleared"] is True
    # The drain runs through the pane, never as a raw daemon write that keeps the hold.
    writer.assert_not_awaited()
    assert "ctrl_u" in pane.keys
    assert read_composer(TERMINAL_ID).state == "empty"
    # A later compact handoff types /compact fresh instead of replaying a held command.
    assert await composer_gate_for_write(pane, action="compact", pending_payload="/compact") == (
        True,
        None,
        "empty",
    )


@pytest.fixture
def held_wake_line() -> Iterator[ComposerLedger]:
    """Bind a ledger whose composer holds the daemon's unsubmitted wake line (#21670)."""
    ledger = ComposerLedger()
    ledger.release(TERMINAL_ID)
    ledger.observe_write(TERMINAL_ID, origin="daemon", kind="text", payload=CONTINUE_WAKE_MESSAGE)
    bind_composer_ledger(ledger)
    assert read_composer(TERMINAL_ID).state == "held"
    yield ledger
    bind_composer_ledger(None)


@pytest.mark.asyncio
async def test_clear_key_drains_a_held_wake_line_so_the_next_wake_types_fresh(
    monkeypatch: pytest.MonkeyPatch, held_wake_line: ComposerLedger
) -> None:
    pane = _Pane(IDLE_COMPOSER)
    manager = _scene(monkeypatch, composer=None, live_pane=pane)

    cleared = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None, clear_held=True
    )

    assert isinstance(cleared, gate.ComposerCleared)
    assert "ctrl_u" in pane.keys
    assert read_composer(TERMINAL_ID).state == "empty"
    assert (
        await gate.send_keys_target_refusal(SESSION_ID, manager, object(), terminal_context=None)
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"foreground": {TERMINAL_ID: "zsh"}}, "send_keys_target_bare_shell"),
        ({"snapshot": "Password:"}, "send_keys_credential_prompt"),
    ],
    ids=["bare-shell", "credential"],
)
async def test_a_bare_shell_or_credential_prompt_is_refused_with_an_empty_composer(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], code: str
) -> None:
    # An empty ledger isolates the target check: a draft would refuse first on composer state.
    manager = _scene(monkeypatch, **overrides)

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == code


@pytest.mark.asyncio
async def test_a_visible_draft_refuses_an_idle_agent_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A draft is never typed into or replaced, whichever wake asks (#25830 criterion 3).
    draft_frame = "────────────────────\n❯ hello\n────────────────────"
    manager = _scene(monkeypatch, composer="draft", snapshot=draft_frame)

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=None
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_composer_not_idle"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keys", "literal"),
    [("ctrl_u\n", True), ("/compact", True), ("enter", False), ("escape", False)],
    ids=["clear-with-enter", "text", "enter", "escape"],
)
async def test_only_a_pure_clear_key_may_touch_a_held_composer(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
    held_compact: ComposerLedger,
    keys: str,
    literal: bool,
) -> None:
    register, writer, target_id = public_send_keys
    pane = _Pane(IDLE_COMPOSER)
    handler = register(_scene(monkeypatch, composer=None, live_pane=pane))

    result = await handler(session_id=target_id, keys=keys, literal=literal)

    assert result["error_code"] == "send_keys_composer_not_idle"
    assert pane.keys == []
    writer.assert_not_awaited()
    assert read_composer(TERMINAL_ID).state == "held"


@pytest.mark.asyncio
async def test_clear_key_on_an_operator_draft_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
) -> None:
    register, writer, target_id = public_send_keys
    pane = _Pane(IDLE_COMPOSER)
    handler = register(_scene(monkeypatch, composer="draft", live_pane=pane))

    result = await handler(session_id=target_id, keys="ctrl_u", literal=False)

    assert result["error_code"] == "send_keys_composer_not_idle"
    assert "gobby-sessions:release_composer" in result["recovery"]
    assert pane.keys == []
    writer.assert_not_awaited()


# An SRT-sandboxed provider runs under Gobby's Node runner. The runner leads the pane's
# process group and the native build is its child in that same group, so the group
# leader's command reads as ``node``. Every pid below is synthetic.
SRT_RUNNER_PID = 4321
SRT_CHILD_PID = 4322


class _SrtChild:
    def __init__(self, pid: int, exe: str, *, group: int = SRT_RUNNER_PID) -> None:
        self.pid = pid
        self._exe = exe
        self.group = group

    def exe(self) -> str:
        return self._exe


def _native_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Install a native Claude build as the one the gate's PATH lookup finds."""
    build = tmp_path / "share" / "claude" / "versions" / "2.1.318"
    _install(monkeypatch, "claude", build)
    return build


# An SRT seat records its provider child, the process that ran its hooks, as its CLI.
SRT_CONTEXT = {"parent_pid": SRT_CHILD_PID, "parent_create_time": 1.0, "parent_name": "claude"}


def _srt_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    children: list[_SrtChild],
    *,
    identity: bool = True,
) -> Path:
    """Install a runner under GOBBY_HOME and give the runner pane these children.

    The runner leads the foreground group. ``identity`` is whether a recorded pid's start
    time and name still match the process holding it.
    """
    executables = {child.pid: (child.group, child.exe()) for child in children}
    monkeypatch.setattr(
        gate,
        "process_identity_matches",
        lambda pid, context: identity and pid == context.get("parent_pid"),
    )
    monkeypatch.setattr(gate, "group_and_executable", executables.get)
    monkeypatch.setattr(gate, "foreground_group", lambda shell: SRT_RUNNER_PID)
    home = tmp_path / "home"
    runner = home / "tools" / "srt" / SRT_RELEASE.version / "runner.mjs"
    runner.parent.mkdir(parents=True)
    runner.write_text("// SRT runner fixture\n")
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(
        gate, "foreground_node_script", lambda pid: str(runner) if pid == SRT_RUNNER_PID else None
    )
    monkeypatch.setattr(gate, "node_script", lambda pid: None)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("script_kind", ["installed", "linked", "declared-package", "missing"])
@pytest.mark.parametrize("seat", ["own", "impostor", "another-process"])
async def test_srt_runner_admits_npm_claude_only_as_the_seats_own_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script_kind: str, seat: str
) -> None:
    # Under SRT an npm Claude is node on Claude's script, the runner's child. As in the
    # plain case the script only names Claude: a retargeted link reads like ``linked``.
    # What admits is the seat's own recorded provider process, live in the group.
    installed = tmp_path / "npm" / "claude-code" / "cli.js"
    _install(monkeypatch, "claude", installed)
    planted = tmp_path / "planted" / "cli.js"
    planted.parent.mkdir()
    planted.write_text("// planted\n")
    (planted.parent / "package.json").write_text(
        '{"name":"@anthropic-ai/claude-code","bin":{"claude":"cli.js"}}'
    )
    linked = tmp_path / "bin" / "claude"
    linked.parent.mkdir()
    linked.symlink_to(installed)
    script = {
        "installed": installed,
        "linked": linked,
        "declared-package": planted,
        "missing": tmp_path / "missing" / "cli.js",
    }[script_kind]
    _srt_pane(
        monkeypatch,
        tmp_path,
        [_SrtChild(SRT_CHILD_PID, "/opt/homebrew/bin/node")],
        identity=seat != "impostor",
    )
    monkeypatch.setattr(
        gate, "node_script", lambda pid: str(script) if pid == SRT_CHILD_PID else None
    )
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: "/opt/homebrew/bin/node"})
    context = (
        {**SRT_CONTEXT, "parent_pid": SRT_CHILD_PID + 1}
        if seat == "another-process"
        else SRT_CONTEXT
    )

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=context
    )

    if seat == "own" and script_kind in {"installed", "linked"}:
        assert refusal is None
    else:
        assert isinstance(refusal, gate.SendKeysRefusal)
        assert refusal.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
async def test_srt_runner_admits_an_idle_composer_on_its_native_claude_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _native_build(monkeypatch, tmp_path)
    _srt_pane(monkeypatch, tmp_path, [_SrtChild(SRT_CHILD_PID, str(build))])
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: "/opt/homebrew/bin/node"})

    assert (
        await gate.send_keys_target_refusal(
            SESSION_ID, manager, object(), terminal_context=SRT_CONTEXT
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seat", ["unrecorded", "impostor", "another-process"])
async def test_srt_runner_counts_only_the_seats_own_recorded_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seat: str
) -> None:
    """The runner's script path is read again at check time, so it proves nothing.

    A node started on another script, whose link is then pointed at the installed runner,
    reads like the runner, and it can start a real Claude build as a child in its group.
    Only the target session's recorded CLI process, live in the foreground, counts.
    """
    build = _native_build(monkeypatch, tmp_path)
    _srt_pane(
        monkeypatch,
        tmp_path,
        [_SrtChild(SRT_CHILD_PID, str(build))],
        identity=seat != "impostor",
    )
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: "/opt/homebrew/bin/node"})
    context = {
        "unrecorded": None,
        "impostor": SRT_CONTEXT,
        "another-process": {**SRT_CONTEXT, "parent_pid": SRT_CHILD_PID + 1},
    }[seat]

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=context
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("child", ["none", "shell", "node", "foreign-build", "left-group"])
async def test_srt_runner_refuses_without_a_live_agent_cli_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, child: str
) -> None:
    build = _native_build(monkeypatch, tmp_path)
    children = {
        "none": [],
        "shell": [_SrtChild(SRT_CHILD_PID, "/bin/sh")],
        "node": [_SrtChild(SRT_CHILD_PID, "/opt/homebrew/bin/node")],
        "foreign-build": [
            _SrtChild(SRT_CHILD_PID, str(_file(tmp_path / "elsewhere/claude/versions/2.1.999")))
        ],
        "left-group": [_SrtChild(SRT_CHILD_PID, str(build), group=9999)],
    }[child]
    _srt_pane(monkeypatch, tmp_path, children)
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: "/opt/homebrew/bin/node"})

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=SRT_CONTEXT
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
async def test_a_runner_copy_outside_the_installed_root_admits_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _native_build(monkeypatch, tmp_path)
    _srt_pane(monkeypatch, tmp_path, [_SrtChild(SRT_CHILD_PID, str(build))])
    copy = tmp_path / "elsewhere" / "runner.mjs"
    copy.parent.mkdir()
    copy.write_text("// copied runner\n")
    monkeypatch.setattr(gate, "foreground_node_script", lambda pid: str(copy))
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: "/opt/homebrew/bin/node"})

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=SRT_CONTEXT
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == "send_keys_target_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"snapshot": "Password:"}, "send_keys_credential_prompt", id="password"),
        pytest.param({"composer": "draft"}, "send_keys_composer_not_idle", id="human-draft"),
        pytest.param(
            {"snapshot": HOOK_DENIAL_OVER_LIVE_TURN}, "send_keys_composer_not_idle", id="live-turn"
        ),
    ],
)
async def test_srt_admission_leaves_the_composer_checks_in_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: dict[str, Any], code: str
) -> None:
    build = _native_build(monkeypatch, tmp_path)
    _srt_pane(monkeypatch, tmp_path, [_SrtChild(SRT_CHILD_PID, str(build))])
    manager = _scene(monkeypatch, foreground={TERMINAL_ID: "/opt/homebrew/bin/node"}, **overrides)

    refusal = await gate.send_keys_target_refusal(
        SESSION_ID, manager, object(), terminal_context=SRT_CONTEXT
    )

    assert isinstance(refusal, gate.SendKeysRefusal)
    assert refusal.code == code
