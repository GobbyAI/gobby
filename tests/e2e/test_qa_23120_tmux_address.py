"""#23120 QA: gclient pane addresses for an adopted tmux pane.

Evidence driver for R2, run only under an explicit QA admission. It needs
``QA_GCLIENT``, exactly this worktree's ``target/debug/gclient``, used only as
build input: the viewer launches a fixture-owned copy verified against the build
receipt's ``QA_GCLIENT_SHA256`` and ``QA_GCLIENT_VERSION``. Evidence goes to
``QA_23120_EVIDENCE_DIR``, which must resolve physically inside the worktree.
Everything else is disposable: the e2e daemon's own
database, home and ports, a private tmux server holding the external panes,
and a second private tmux server running the viewer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from tests._timing import wait_for_condition
from tests.e2e import test_external_terminal_attach as attach
from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, daemon_token

# The gterm host and its socket dir come from the external-attach setup.
e2e_pre_daemon_setup = attach.e2e_pre_daemon_setup
isolated_tmux = attach.isolated_tmux

WORKTREE = Path(__file__).resolve().parents[2]
WORKTREE_GCLIENT = WORKTREE / "target" / "debug" / "gclient"
WORKSPACE = "qa-23120-tmux"
VIEWER_COLS, VIEWER_ROWS = 150, 46
ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")
TMUX_ADDRESS = re.compile(r"tmux\s*·\s*(\d+(?::\d+)+)")
PANE_FORMAT = "\t".join(
    (
        "#{pane_id}",
        "#{window_id}",
        "#{session_name}",
        "#{pane_pid}",
        "#{pane_width}x#{pane_height}",
        "#{pane_pipe}",
        "#{pane_title}",
        "#{pane_active}",
        "#{pane_dead}",
    )
)


def _admitted(name: str) -> str:
    raw = os.environ.get(name)
    if not raw:
        pytest.skip(f"{name} is unset; this QA driver runs only under admission")
    return raw


def _qa_gclient(raw: str, expected: Path) -> Path:
    """The worktree's own gclient build, matched by its lexical path.

    The worktree's `target` is a dedicated symlinked Cargo cache, so the
    binary's resolved path legitimately leaves the worktree.
    """
    path = Path(os.path.normpath(os.path.abspath(raw)))
    if path != expected:
        raise ValueError(f"QA_GCLIENT={path} is not {expected}")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"QA_GCLIENT={path} is not an executable file")
    return path


def _fixture_gclient(source: Path, sha256: str, version: str, directory: Path) -> dict[str, str]:
    """Copy the build into a fixture-owned, read-only path and prove it is the receipt's build.

    The worktree build stays input only: a rebuild mid-run cannot change what
    the viewer launches.
    """
    directory.mkdir(parents=True, exist_ok=True)
    copy = directory / f"gclient-{uuid.uuid4().hex[:12]}"
    shutil.copyfile(source, copy)
    copy.chmod(0o555)
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    if digest != sha256:
        raise ValueError(f"fixture gclient sha256 {digest} does not match receipt {sha256}")
    reported = subprocess.run(
        [str(copy), "--version"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    if reported != f"gclient {version}":
        raise ValueError(f"fixture gclient reports {reported!r}, receipt says {version}")
    return {
        "source": str(source),
        "source_resolved": str(source.resolve()),
        "fixture_copy": str(copy),
        "sha256": digest,
        "version": reported,
    }


def _qa_evidence_dir(raw: str, worktree: Path) -> Path:
    """Evidence must physically live inside the worktree, never behind a symlink out."""
    path = Path(raw).resolve()
    if not path.is_relative_to(worktree.resolve()):
        raise ValueError(f"QA_23120_EVIDENCE_DIR={raw} resolves to {path}, outside {worktree}")
    return path


def test_qa_gclient_allows_only_the_symlinked_worktree_build(tmp_path: Path) -> None:
    worktree, cache = tmp_path / "worktree", tmp_path / "cargo-cache"
    (cache / "debug").mkdir(parents=True)
    worktree.mkdir()
    (worktree / "target").symlink_to(cache)
    binary = cache / "debug" / "gclient"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    expected = worktree / "target" / "debug" / "gclient"

    assert _qa_gclient(str(worktree / "target" / "." / "debug" / "gclient"), expected) == expected
    with pytest.raises(ValueError, match="is not"):
        _qa_gclient(str(binary), expected)
    binary.chmod(0o644)
    with pytest.raises(ValueError, match="not an executable file"):
        _qa_gclient(str(expected), expected)


def test_fixture_gclient_launches_only_a_verified_immutable_copy(tmp_path: Path) -> None:
    source = tmp_path / "target" / "gclient"
    source.parent.mkdir()
    source.write_text("#!/bin/sh\necho 'gclient 9.9.9'\n")
    source.chmod(0o755)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    fixture = tmp_path / "fixture"

    record = _fixture_gclient(source, sha, "9.9.9", fixture)
    copy = Path(record["fixture_copy"])
    assert copy.parent == fixture
    assert copy.stat().st_mode & 0o777 == 0o555
    # Rebuilding the input afterwards leaves the launched copy untouched.
    source.write_text("#!/bin/sh\necho 'gclient 0.0.0'\n")
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == sha
    with pytest.raises(ValueError, match="does not match receipt"):
        _fixture_gclient(source, sha, "9.9.9", fixture)
    with pytest.raises(ValueError, match="receipt says 1.0.0"):
        _fixture_gclient(copy, sha, "1.0.0", fixture)


def test_qa_evidence_dir_rejects_a_path_that_resolves_outside(tmp_path: Path) -> None:
    worktree, cache = tmp_path / "worktree", tmp_path / "cargo-cache"
    cache.mkdir()
    worktree.mkdir()
    (worktree / "target").symlink_to(cache)

    inside = worktree / ".qa-evidence" / "23120"
    assert _qa_evidence_dir(str(inside), worktree) == inside.resolve()
    with pytest.raises(ValueError, match="outside"):
        _qa_evidence_dir(str(worktree / "target" / "qa-evidence" / "23120"), worktree)


class Evidence:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.log = directory / "run.log"
        self.log.write_text("")

    def note(self, line: str) -> None:
        with self.log.open("a") as handle:
            handle.write(line + "\n")

    def json(self, name: str, value: object) -> None:
        (self.directory / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        self.note(f"wrote {name}")

    def text(self, name: str, value: str) -> None:
        (self.directory / name).write_text(value)
        self.note(f"wrote {name}")


def _tmux_metadata(server: attach.IsolatedTmux) -> dict[str, Any]:
    panes = attach._tmux(server.socket, "list-panes", "-a", "-F", PANE_FORMAT)
    return {
        "panes": sorted(panes.splitlines()),
        "clients": server.clients(),
        "control_pane": server.display(attach.PANE_PROPS, target=server.control_pane),
    }


def _external_rows(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """Live external tmux rows by id.

    List items carry `attach: null` (r2 rows-timeout.json), so a pane's row is
    told apart by when it appears, never by its locator.
    """
    response = client.get("/api/terminals", params={"project_id": attach.E2E_PROJECT_ID})
    response.raise_for_status()
    return {
        str(item["id"]): cast(dict[str, Any], item)
        for item in response.json().get("items") or []
        if item.get("ownership") == "external"
        and item.get("backend") == "tmux"
        and item.get("state") == "live"
    }


def _terminal(client: httpx.Client, terminal_id: str) -> dict[str, Any]:
    response = client.get(f"/api/terminals/{terminal_id}")
    return cast(dict[str, Any], response.json()) if response.status_code == 200 else {}


def _seed(cli_events: CLIEventSimulator, context: dict[str, object], cwd: str) -> None:
    """Register a CLI session in one pane; its first prompt materializes the row."""
    external_id = f"ext-cli-{uuid.uuid4().hex[:8]}"
    cli_events.session_start(
        session_id=external_id,
        machine_id=attach.MACHINE_ID,
        cli_source="claude",
        project_id=attach.E2E_PROJECT_ID,
        cwd=cwd,
        terminal_context=context,
    )
    cli_events.user_prompt_submit(
        external_id,
        "SHOW_PROMPT",
        source="claude",
        machine_id=attach.MACHINE_ID,
        cwd=cwd,
        project_id=attach.E2E_PROJECT_ID,
        terminal_context=context,
    )


def _workspace_tool(client: httpx.Client, tool: str, **arguments: object) -> dict[str, Any]:
    response = client.post(f"/api/mcp/gobby-workspaces/tools/{tool}", json=arguments)
    body = cast(dict[str, Any], response.json())
    inner = body.get("result")
    payload = dict(cast(dict[str, Any], inner)) if isinstance(inner, dict) else dict(body)
    # As in the workspaces CLI: the route drops a successful tool's own flag and
    # keeps only a refusal's `success: false`.
    payload["success"] = body.get("success") is True and payload.get("success") is not False
    return payload


class Viewer:
    """The gclient under test, on its own tmux server (so its prefix is ctrl+])."""

    def __init__(self, gclient: Path, daemon: DaemonInstance, token_file: Path) -> None:
        self.socket = attach._short_socket_dir() / "viewer.sock"
        self.command = [
            "env",
            "-u",
            "GOBBY_PANE_ID",
            f"GOBBY_HOME={daemon.gobby_home}",
            str(gclient),
            "--daemon-url",
            daemon.http_url,
            "--token-file",
            str(token_file),
            "--workspace",
            WORKSPACE,
            # The tabs belong to the e2e project; without this the viewer takes
            # the project of whatever checkout it was started in.
            "--project",
            attach.E2E_PROJECT_ID,
        ]

    def tmux(self, *args: str) -> str:
        return attach._tmux(self.socket, *args)

    def start(self) -> None:
        self.tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "viewer",
            "-x",
            str(VIEWER_COLS),
            "-y",
            str(VIEWER_ROWS),
            "--",
            *self.command,
        )
        self.tmux("set-option", "-t", "viewer", "remain-on-exit", "on")

    def capture(self) -> str:
        return self.tmux("capture-pane", "-p", "-e", "-N", "-t", "viewer")

    def keys(self, *keys: str) -> None:
        self.tmux("send-keys", "-t", "viewer", *keys)

    def dead(self) -> bool:
        return self.tmux("display-message", "-p", "-t", "viewer", "#{pane_dead}") == "1"

    def relaunch(self) -> None:
        self.tmux("respawn-pane", "-k", "-t", "viewer", *self.command)

    def close(self) -> None:
        attach._tmux(self.socket, "kill-server", check=False)
        self.socket.unlink(missing_ok=True)
        self.socket.parent.rmdir()


def _tmux_addresses(screen: str) -> list[str]:
    return cast(list[str], TMUX_ADDRESS.findall(ANSI.sub("", screen)))


@pytest.mark.e2e
def test_adopted_tmux_pane_keeps_its_workspace_address(
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
    cli_events: CLIEventSimulator,
    isolated_tmux: attach.IsolatedTmux,
    tmp_path: Path,
) -> None:
    build = _qa_gclient(_admitted("QA_GCLIENT"), WORKTREE_GCLIENT)
    evidence = Evidence(_qa_evidence_dir(_admitted("QA_23120_EVIDENCE_DIR"), WORKTREE))
    fixture = _fixture_gclient(
        build,
        _admitted("QA_GCLIENT_SHA256"),
        _admitted("QA_GCLIENT_VERSION"),
        tmp_path / "gclient-fixture",
    )
    evidence.json("gclient-fixture.json", fixture)
    gclient = Path(fixture["fixture_copy"])
    attach._wait_for_host(daemon_client, daemon_instance)
    cwd = str(daemon_instance.project_dir)

    # Pane A is the scripted CLI; pane B, its split shell, is discovered but never held.
    pane_b = isolated_tmux.display("#{pane_id}", target=f"{isolated_tmux.window_id}.1")

    def live_row(pane_id: str, label: str, known: set[str]) -> dict[str, Any]:
        """The one live external tmux row that appeared since `known` was taken."""

        def found() -> dict[str, Any]:
            fresh = [row for key, row in _external_rows(daemon_client).items() if key not in known]
            assert len(fresh) <= 1, f"{label} seeded more than one row: {fresh}"
            return fresh[0] if fresh else {}

        try:
            return wait_for_condition(
                found, timeout=10.0, interval=0.2, description=f"external row for {label}"
            )
        except AssertionError:
            listing = daemon_client.get(
                "/api/terminals", params={"project_id": attach.E2E_PROJECT_ID}
            )
            evidence.json(
                "rows-timeout.json",
                {
                    "waiting_for": {"label": label, "pane_id": pane_id},
                    "terminals_status": listing.status_code,
                    "terminals": listing.text,
                    "daemon_log_tail": daemon_instance.read_logs()[-6000:],
                    "daemon_error_log_tail": daemon_instance.read_error_logs()[-6000:],
                },
            )
            raise

    known = set(_external_rows(daemon_client))
    _seed(cli_events, isolated_tmux.context(), cwd)
    row_a = live_row(isolated_tmux.pane_id, "pane A", known)
    _seed(cli_events, {**isolated_tmux.context(), "tmux_pane": pane_b}, cwd)
    row_b = live_row(pane_b, "pane B", known | {str(row_a["id"])})
    before = {"tmux": _tmux_metadata(isolated_tmux), "rows": {"a": row_a, "b": row_b}}
    evidence.json("tmux-metadata-before.json", before)

    created = _workspace_tool(daemon_client, "create_workspace", name=WORKSPACE)
    home_tab = _workspace_tool(
        daemon_client,
        "create_tab",
        workspace=WORKSPACE,
        project_id=attach.E2E_PROJECT_ID,
        title="adopt",
    )
    evidence.json("workspace-create.json", {"workspace": created, "tab": home_tab})
    assert home_tab.get("success") is True, home_tab
    snapshot = _workspace_tool(daemon_client, "get_workspace", workspace=WORKSPACE)
    native_pane = snapshot["panes"][0]
    adopted = _workspace_tool(
        daemon_client,
        "split_pane",
        pane=str(native_pane["id"]),
        axis="horizontal",
        terminal_id=str(row_a["id"]),
    )
    if adopted.get("success") is not True:
        evidence.json(
            "adopt-error.json",
            {
                "response": adopted,
                "terminal": row_a,
                "workspace_machine_id": snapshot["workspace"].get("machine_id"),
            },
        )
        pytest.fail(f"tmux adoption refused: {adopted}")
    away_tab = _workspace_tool(
        daemon_client,
        "create_tab",
        workspace=WORKSPACE,
        project_id=attach.E2E_PROJECT_ID,
        title="away",
    )
    assert away_tab.get("success") is True, away_tab
    model = _workspace_tool(daemon_client, "get_workspace", workspace=WORKSPACE)
    evidence.json("workspace-model.json", model)
    assert any(pane.get("terminal_id") == row_a["id"] for pane in model["panes"]), model

    token_file = tmp_path / "viewer-token"
    token_file.write_text(daemon_token(daemon_instance.gobby_home))
    token_file.chmod(0o600)
    viewer = Viewer(gclient, daemon_instance, token_file)
    screens: dict[str, str] = {}

    def settled(name: str) -> None:
        try:
            screens[name] = wait_for_condition(
                lambda: (lambda screen: screen if _tmux_addresses(screen) else "")(
                    viewer.capture()
                ),
                timeout=20.0,
                interval=0.25,
                description=f"tmux address on screen ({name})",
            )
        except AssertionError:
            evidence.text(f"{name}-timeout.ans", viewer.capture())
            raise
        evidence.text(f"{name}.ans", screens[name])

    try:
        viewer.start()
        settled("tmux-adopt-1")
        viewer.keys("C-]", "n")
        wait_for_condition(
            lambda: not _tmux_addresses(viewer.capture()),
            timeout=10.0,
            interval=0.25,
            description="away tab shows no tmux pane",
        )
        viewer.keys("C-]", "p")
        settled("tmux-adopt-2")
        viewer.keys("C-]", "Q")
        wait_for_condition(viewer.dead, timeout=10.0, interval=0.2, description="viewer quit")
        evidence.note("viewer quit on prefix+Q")
        viewer.relaunch()
        settled("tmux-adopt-3")
    finally:
        viewer.close()

    addresses = {name: _tmux_addresses(screen) for name, screen in screens.items()}
    evidence.json("addresses.json", addresses)
    first = addresses["tmux-adopt-1"]
    assert len(first) == 1, addresses
    assert all(found == first for found in addresses.values()), addresses
    assert all("tmux %" not in ANSI.sub("", screen) for screen in screens.values())

    closed = _workspace_tool(daemon_client, "close_workspace", workspace=WORKSPACE)
    after = {
        "tmux": _tmux_metadata(isolated_tmux),
        "rows": {
            "a": _terminal(daemon_client, str(row_a["id"])),
            "b": _terminal(daemon_client, str(row_b["id"])),
        },
        "close_workspace": closed,
    }
    evidence.json("tmux-metadata-after.json", after)
    assert after["tmux"] == before["tmux"], {"before": before["tmux"], "after": after["tmux"]}
    assert after["rows"]["a"].get("state") == "live", after["rows"]["a"]
    assert after["rows"]["b"].get("state") == "live", after["rows"]["b"]
    evidence.note("PASS")
