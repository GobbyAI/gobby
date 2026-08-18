"""Hub files_home wiki-home resolution for gwiki and the daemon owner entry."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException

from gobby.servers.http import HTTPServer

from gobby.config.bootstrap_io import write_bootstrap_yaml
from gobby.paths import FilesHomeError, require_files_home
from gobby.runner_pid_file import claim_pid_file
from gobby.servers.routes.wiki import _gateway_from_scope, _resolve_scope
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.wiki.scope_resolution import resolve_wiki_scope

pytestmark = pytest.mark.unit

_WORKTREE = Path(__file__).resolve().parents[2]
_GWIKI = _WORKTREE / "target" / "debug" / "gwiki"


def _local_mapping(files_home: Path) -> dict[str, object]:
    return {
        "datastore_mode": "local",
        "files_home": str(files_home),
        "daemon_port": 60887,
        "bind_host": "127.0.0.1",
    }


def _remote_mapping() -> dict[str, object]:
    return {
        "datastore_mode": "remote",
        "hub_daemon_url": "https://hub.example.test:7443",
        "daemon_port": 60887,
        "bind_host": "127.0.0.1",
    }


def _owner_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "gobby-home"
    files_home = tmp_path / "files"
    home.mkdir()
    files_home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(files_home))
    return home, files_home


def _gwiki() -> Path:
    if not _GWIKI.is_file():
        pytest.skip("gwiki debug binary is not built")
    return _GWIKI


def test_default_hub_path_is_files_home_wiki_not_home_wiki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    (tmp_path / "user-home").mkdir()
    monkeypatch.delenv("GOBBY_WIKI_HUB", raising=False)

    from gobby.wiki.scope_resolution import owner_wiki_home

    assert owner_wiki_home() == require_files_home() / "wiki"
    assert owner_wiki_home() != Path.home() / "wiki"


@pytest.mark.asyncio
async def test_personal_wiki_scope_is_files_home_wiki_personal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_home(tmp_path, monkeypatch)
    resolved = await resolve_wiki_scope(None, project=PERSONAL_PROJECT_ID)
    assert resolved.project_id == PERSONAL_PROJECT_ID
    assert resolved.project_root == require_files_home() / "_personal"
    assert resolved.topic is None
    from gobby.wiki.scope_resolution import owner_personal_wiki_root

    assert owner_personal_wiki_root() == require_files_home() / "wiki" / "personal"


@pytest.mark.asyncio
async def test_daemon_topic_and_personal_use_same_wiki_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_home(tmp_path, monkeypatch)
    server = cast(
        HTTPServer,
        SimpleNamespace(services=SimpleNamespace(database=None, project_id=None)),
    )

    topic = await _resolve_scope(server, None, "foo")
    personal = await _resolve_scope(server, PERSONAL_PROJECT_ID, None)

    assert topic.topic == "foo"
    assert personal.project_root == require_files_home() / "_personal"
    topic_gateway = _gateway_from_scope(topic)
    personal_gateway = _gateway_from_scope(personal)
    assert topic_gateway._topic == "foo"
    assert personal_gateway._project_root == str(require_files_home() / "_personal")
    assert personal_gateway._topic is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["personal", "_personal", "wiki"])
async def test_reserved_topic_names_refuse_at_daemon_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _owner_home(tmp_path, monkeypatch)
    server = cast(
        HTTPServer,
        SimpleNamespace(services=SimpleNamespace(database=None, project_id=None)),
    )
    with pytest.raises(HTTPException) as exc:
        await _resolve_scope(server, None, name)
    assert exc.value.status_code == 400


def test_remote_mode_refuses_topic_filesystem_even_with_hub_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _remote_mapping())
    hub = tmp_path / "override-hub"
    hub.mkdir()
    binary = _gwiki()
    result = subprocess.run(
        [str(binary), "init", "--topic", "foo", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GOBBY_HOME": str(home), "GOBBY_WIKI_HUB": str(hub)},
        check=False,
    )
    assert result.returncode != 0
    combined = f"{result.stdout}\n{result.stderr}".lower()
    assert "remote" in combined or "files owner" in combined
    assert not (hub / "foo").exists()
    assert not (hub / "topics" / "foo").exists()


@pytest.mark.asyncio
async def test_owner_entry_refuses_missing_files_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby-home"
    missing = tmp_path / "missing-files"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(missing))
    server = cast(
        HTTPServer,
        SimpleNamespace(services=SimpleNamespace(database=None, project_id=None)),
    )
    with pytest.raises(HTTPException):
        await _resolve_scope(server, None, "foo")
    assert not missing.exists()


def test_gwiki_init_refuses_missing_files_home_and_does_not_create_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby-home"
    missing = tmp_path / "missing-files"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(missing))
    binary = _gwiki()
    result = subprocess.run(
        [str(binary), "init", "--topic", "foo", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GOBBY_HOME": str(home)},
        check=False,
    )
    assert result.returncode != 0
    assert not missing.exists()


def test_owner_init_does_not_make_wiki_home_a_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, files_home = _owner_home(tmp_path, monkeypatch)
    binary = _gwiki()
    result = subprocess.run(
        [str(binary), "init", "--topic", "foo", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GOBBY_HOME": str(home)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    wiki_home = files_home / "wiki"
    assert (wiki_home / "foo" / "_gwiki" / "scope.json").is_file()
    assert not (wiki_home / "_gwiki" / "scope.json").exists()
    assert (wiki_home / "wikis.json").is_file()


def test_personal_root_maps_to_wiki_personal_and_project_stays_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_home(tmp_path, monkeypatch)
    from gobby.wiki.scope_resolution import owner_personal_wiki_root

    assert owner_personal_wiki_root() == require_files_home() / "wiki" / "personal"
    project = tmp_path / "checkout"
    (project / ".gobby").mkdir(parents=True)
    (project / ".gobby" / "gcode.json").write_text(
        '{"id": "project-123", "name": "demo"}\n', encoding="utf-8"
    )
    from gobby.utils.wiki_vault import resolve_vault_dir

    assert resolve_vault_dir(project) == project / "wiki"


def test_local_bootstrap_refuses_mismatched_hub_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, files_home = _owner_home(tmp_path, monkeypatch)
    other = tmp_path / "other-hub"
    other.mkdir()
    binary = _gwiki()
    result = subprocess.run(
        [str(binary), "init", "--topic", "foo", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GOBBY_HOME": str(home),
            "GOBBY_WIKI_HUB": str(other),
        },
        check=False,
    )
    assert result.returncode != 0
    assert not (other / "foo").exists()
    assert not (files_home / "wiki" / "foo").exists()


def test_init_refuses_live_daemon_holder_with_zero_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, files_home = _owner_home(tmp_path, monkeypatch)
    claim = claim_pid_file(home / "gobby.pid", role="daemon")
    assert claim is not None
    try:
        binary = _gwiki()
        result = subprocess.run(
            [str(binary), "init", "--topic", "foo", "--format", "json"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**os.environ, "GOBBY_HOME": str(home)},
            check=False,
        )
        assert result.returncode != 0
        assert not (files_home / "wiki" / "foo").exists()
        assert not (files_home / "wiki" / "wikis.json").exists()
    finally:
        claim.release()


def test_replaced_files_home_refuses_and_does_not_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_home(tmp_path, monkeypatch)
    files_home = require_files_home()
    files_home.rename(tmp_path / "files.old")
    with pytest.raises(FilesHomeError):
        require_files_home()
    assert not files_home.exists()


def test_init_refuses_symlink_and_non_directory_files_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby-home"
    home.mkdir()
    real = tmp_path / "real-files"
    real.mkdir()
    linked = tmp_path / "linked-files"
    linked.symlink_to(real)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(linked))
    binary = _gwiki()
    result = subprocess.run(
        [str(binary), "init", "--topic", "foo", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GOBBY_HOME": str(home)},
        check=False,
    )
    assert result.returncode != 0
    assert not (linked / "wiki").exists()
    assert not (real / "wiki").exists()

    file_home = tmp_path / "file-home"
    file_home.write_text("not a dir", encoding="utf-8")
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(file_home))
    result = subprocess.run(
        [str(binary), "init", "--topic", "foo", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GOBBY_HOME": str(home)},
        check=False,
    )
    assert result.returncode != 0
    assert file_home.is_file()


def test_inherited_daemon_claim_survives_parent_exit(tmp_path: Path) -> None:
    home = tmp_path / "gobby-home"
    home.mkdir()
    child = tmp_path / "hold.py"
    child.write_text(
        "\n".join(
            [
                "import os, time",
                "from pathlib import Path",
                "from gobby.runner_pid_file import claim_pid_file",
                f"os.environ['GOBBY_HOME'] = {str(home)!r}",
                "claim = claim_pid_file(Path(os.environ['GOBBY_HOME']) / 'gobby.pid', role='daemon')",
                "assert claim is not None",
                "pid = os.fork()",
                "if pid == 0:",
                "    time.sleep(4)",
                "    os._exit(0)",
                "os._exit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(child)],
        cwd=str(_WORKTREE),
        check=True,
        env={**os.environ, "GOBBY_HOME": str(home)},
    )
    time.sleep(0.2)
    from gobby.runner_pid_file import claim_pid_file

    blocked = claim_pid_file(home / "gobby.pid", role="maintenance")
    assert blocked is None
