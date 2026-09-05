"""Exercise retirement through an isolated real daemon, optionally exporting purge proof.

Export is opt-in: set GOBBY_WIKI_RETIREMENT_PROOF_INVENTORY to the private inventory
and GOBBY_WIKI_RETIREMENT_PROOF_OUTPUT to a file in its retirement directory. Run
from the clean installed main checkout with GOBBY_NATIVE_BIN_DIR pointing at its
installed binaries and GOBBY_TEST_GDAEMON=installed. Ordinary runs use checkout
binaries and never produce operational evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
    authenticated_daemon_client,
)

pytestmark = pytest.mark.e2e

_REPOSITORY = Path(__file__).resolve().parents[2]
_SENTINEL = "RETIREMENT_E2E_LEGACY_OVERVIEW_MUST_NOT_BE_INJECTED"
_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
_INVENTORY_ENV = "GOBBY_WIKI_RETIREMENT_PROOF_INVENTORY"
_OUTPUT_ENV = "GOBBY_WIKI_RETIREMENT_PROOF_OUTPUT"
_RETIRED_ROUTES = (
    ("GET", "/api/wiki/status"),
    ("GET", "/api/wiki/search?q=legacy"),
    ("GET", "/api/wiki/code/status"),
    ("POST", "/api/wiki/code/refresh"),
    ("POST", "/api/wiki/compile"),
)


def _vaults(project: Path, home: Path) -> tuple[Path, Path]:
    return project / ".gobby" / "wiki", home / "files" / "wiki"


@pytest.fixture
def e2e_pre_daemon_setup(
    monkeypatch: pytest.MonkeyPatch,
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
) -> None:
    """Put legacy content on disk before startup, using only fixture-owned paths."""
    exporting = bool(os.environ.get(_INVENTORY_ENV))
    assert exporting == bool(os.environ.get(_OUTPUT_ENV)), "Set both proof export paths"
    if not exporting:
        monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(_REPOSITORY / "target" / "debug"))
    for vault in _vaults(e2e_project_dir, e2e_config[0].parent):
        vault.mkdir(parents=True)
        (vault / "overview.md").write_text(f"# Legacy overview\n{_SENTINEL}\n")
        (vault / "sessions").mkdir()
    (e2e_project_dir / "original-source.md").write_text("Original source outside wiki storage.\n")


def _snapshot(roots: tuple[Path, Path]) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_no_writer(daemon: DaemonInstance) -> None:
    for process in psutil.Process(daemon.pid).children(recursive=True):
        try:
            args = process.cmdline()
        except psutil.NoSuchProcess:
            continue
        assert not any(Path(arg).name == "gwiki" or "gobby.wiki" in arg for arg in args)


def _assert_fresh_session(daemon: DaemonInstance, cli: CLIEventSimulator) -> str:
    external_id = f"retirement-e2e-{uuid.uuid4()}"
    registered = cli.register_session(
        external_id, project_id=_PROJECT_ID, cwd=str(daemon.project_dir)
    )
    session_id = registered["id"]
    result = cli.session_start(external_id, project_id=_PROJECT_ID, cwd=str(daemon.project_dir))
    assert "continue" in result or "decision" in result
    serialized = json.dumps(result)
    for marker in (_SENTINEL, "<wiki_context>", ".gobby/wiki", "gobby-wiki"):
        assert marker not in serialized
    cli.session_end(external_id)
    with authenticated_daemon_client(daemon) as client:
        response = client.get(f"/api/sessions/{session_id}")
        response.raise_for_status()
        assert external_id in response.text
    return str(session_id)


def _runtime_identity(daemon: DaemonInstance, inventory_path: Path) -> dict[str, Any]:
    """Refuse evidence from a worktree, dirty source, or redirected native binary."""
    from scripts.wiki_retirement_inventory import Inventory
    from scripts.wiki_retirement_receipts import read_private

    inventory = Inventory.model_validate_json(read_private(inventory_path))
    repository = Path(inventory.repository).resolve()
    assert repository == _REPOSITORY, "Proof must test the inventoried installed checkout"
    assert (repository / ".git").is_dir(), "A linked worktree cannot prove installed main"
    assert os.environ.get("GOBBY_TEST_GDAEMON") == "installed", "Proof requires installed gdaemon"
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=repository, check=True)
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(Path(__file__).relative_to(repository))],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    # The child uses the daemon's exact Python executable, environment and cwd.
    # In particular, HOME is isolated and must not redirect native-bin discovery.
    probe = subprocess.run(
        [
            daemon.command[0],
            "-c",
            "import json, gobby; from gobby.utils.native_bin import resolve_native_bin; "
            "print(json.dumps({'source': gobby.__file__, 'binaries': "
            "{n: resolve_native_bin(n) for n in ('gcode','gdaemon','ghook')}}))",
        ],
        cwd=daemon.project_dir,
        env=daemon.env,
        check=True,
        capture_output=True,
        text=True,
    )
    resolved = json.loads(probe.stdout)
    assert Path(resolved["source"]).resolve() == repository / "src" / "gobby" / "__init__.py"
    installed_bin = Path(daemon.env["GOBBY_NATIVE_BIN_DIR"])
    assert installed_bin.is_absolute()
    assert str(installed_bin) in inventory.native_dirs, "Native directory was not inventoried"
    binaries = {}
    for name, selected in resolved["binaries"].items():
        path = installed_bin / name
        assert selected and Path(selected).resolve() == path.resolve()
        assert not path.is_symlink(), "Proof requires the installed binary itself"
        subprocess.run(
            [str(path), "--version"], env=daemon.env, check=True, capture_output=True, timeout=10
        )
        binaries[name] = {
            "path": str(path),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {
        "installed_repository": str(repository),
        "installed_commit": commit,
        "binaries": binaries,
    }


def _export_proof(
    daemon: DaemonInstance, identity: dict[str, Any] | None, checks: dict[str, str]
) -> None:
    if identity is None:
        return
    from scripts.wiki_retirement_inventory import (
        Inventory,
        canonical,
        now,
        retirement_directory,
        sha,
    )
    from scripts.wiki_retirement_receipts import (
        Evidence,
        RuntimeProof,
        read_private,
        runtime_digest,
        write_private,
    )

    inventory_path = Path(os.environ[_INVENTORY_ENV])
    assert _runtime_identity(daemon, inventory_path) == identity, "Runtime changed during checks"
    inventory = Inventory.model_validate_json(read_private(inventory_path))
    output = Path(os.environ[_OUTPUT_ENV]).absolute()
    assert output.parent == retirement_directory(inventory), (
        "Keep evidence in the private inventory"
    )
    assert output != inventory_path.absolute(), "Do not overwrite the inventory"
    proof = RuntimeProof(inventory_digest=inventory.digest, **identity, checks={}, created_at=now())
    binding = runtime_digest(proof)
    command = list(sys.orig_argv)
    for name, checked_at in checks.items():
        artifact = output.with_name(f"{output.stem}-{name}.json")
        data = canonical(
            {
                "check": name,
                "inventory_digest": inventory.digest,
                "runtime_digest": binding,
                "created_at": checked_at,
                "command": command,
                "exit_code": 0,
                "passed": True,
                "output": f"PASS: {name}; isolated daemon, legacy sentinel and restart verified",
            }
        )
        write_private(artifact, data)
        proof.checks[name] = Evidence(path=str(artifact), digest=sha(data))
    proof = proof.model_copy(update={"created_at": now()})
    write_private(output, canonical(proof.model_dump(mode="json")))


def test_wiki_retirement_real_daemon(
    daemon_instance: DaemonInstance,
    cli_events: CLIEventSimulator,
    mcp_client: MCPTestClient,
) -> None:
    """Retired state stays inert across real discovery, session hooks and restart."""
    from scripts.wiki_retirement_inventory import Inventory, canonical, now
    from scripts.wiki_retirement_receipts import write_private

    daemon = daemon_instance
    inventory = os.environ.get(_INVENTORY_ENV)
    identity = _runtime_identity(daemon, Path(inventory)) if inventory else None
    checks: dict[str, str] = {}
    assert daemon.is_alive()
    wiki_spec = importlib.util.find_spec("gobby.wiki")
    assert wiki_spec is None or wiki_spec.origin is None
    assert not list((_REPOSITORY / "src" / "gobby" / "wiki").rglob("*.py"))
    # A passing daemon check cannot be relabeled as evidence for another checkout.
    wrong_inventory = Inventory(
        created_at=now(),
        repository=str(daemon.project_dir),
        baseline="0" * 40,
        uid=os.getuid(),
        gobby_home=str(daemon.gobby_home),
        files_home=str(daemon.gobby_home / "files"),
        checkouts=[],
        roots=[],
        stores=[],
        backend_identities={},
        writers=[],
    )
    wrong_inventory_path = daemon.gobby_home / "foreign-runtime-inventory.json"
    write_private(wrong_inventory_path, canonical(wrong_inventory.model_dump(mode="json")))
    with pytest.raises(AssertionError, match="inventoried installed checkout"):
        _runtime_identity(daemon, wrong_inventory_path)
    roots = _vaults(daemon.project_dir, daemon.gobby_home)
    before = _snapshot(roots)
    assert len(before) == 2
    original = daemon.project_dir / "original-source.md"
    original_bytes = original.read_bytes()

    with authenticated_daemon_client(daemon) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        checks["installed_startup"] = now()
        for method, path in _RETIRED_ROUTES:
            missing = client.request(method, "/api/unknown-retirement-probe")
            retired = client.request(method, path)
            assert retired.status_code == missing.status_code == 404
            assert retired.json() == missing.json()
        checks["http_missing_routes"] = now()
        # The fixture disables indexing, but the surviving graph route is registered.
        graph = client.get("/api/code-index/graph", params={"project_id": _PROJECT_ID})
        assert graph.status_code == 503
        assert graph.json()["detail"] == "Code graph not available"

    names = {server["name"] for server in mcp_client.list_servers()}
    assert {"gobby-workflows", "gobby-hub", "gobby-sessions"} <= names
    assert not any("wiki" in name for name in names)
    tools = mcp_client.list_tools()
    assert tools
    assert not any("wiki" in tool["name"] or "wiki" in tool["server"] for tool in tools)
    checks["mcp_absent"] = now()

    first_session = _assert_fresh_session(daemon, cli_events)
    assert _snapshot(roots) == before
    _assert_no_writer(daemon)
    checks["no_injection"] = now()
    daemon.stop()
    # Remove only the two fixture-created vaults, then prove no writer recreates them.
    for root in roots:
        shutil.rmtree(root)
    daemon.restart()
    assert _assert_fresh_session(daemon, cli_events) != first_session
    with authenticated_daemon_client(daemon) as client:
        assert client.get(f"/api/sessions/{first_session}").status_code == 200
        assert client.get("/api/wiki/status").status_code == 404
    assert not any(root.exists() for root in roots)
    assert original.read_bytes() == original_bytes
    checks["no_regeneration"] = now()
    _assert_no_writer(daemon)
    checks["no_writers"] = now()
    _export_proof(daemon, identity, checks)
