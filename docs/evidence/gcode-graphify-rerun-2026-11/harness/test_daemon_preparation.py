"""Check child environment isolation without importing or starting the daemon."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import types
import urllib.error
import urllib.request
from importlib.abc import MetaPathFinder
from pathlib import Path

import prepare_daemon
import prepare_identity
import pytest
import runtime_gobby
from prepare_daemon import isolated_environment, record_preparation
from runtime_boundary import EvidenceError
from runtime_gobby import known_config_values


def test_child_clears_managed_identity_and_shared_datastores(tmp_path: Path) -> None:
    ambient = {
        "GOBBY_HOME": "/live",
        "GOBBY_SESSION_ID": "live-session",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/live/grant.json",
        "GOBBY_NATIVE_BIN_DIR": "/live/bin",
        "DATABASE_URL": "live-database",
        "OPENAI_API_KEY": "private",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "live-telemetry",
        "HOME": "/Users/operator",
        "CODEX_HOME": "/Users/operator/.codex",
        "PATH": "/bin",
    }
    child = isolated_environment(tmp_path, ambient)
    assert child["GOBBY_HOME"] == str(tmp_path / "gobby-home")
    assert child["GOBBY_NATIVE_BIN_DIR"] == str(tmp_path / "gobby-home/bin")
    assert child["GOBBY_TEST_PROTECT"] == "1"
    assert not {
        "DATABASE_URL",
        "GOBBY_SESSION_ID",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "OPENAI_API_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    }.intersection(child)
    assert child["HOME"] == str(tmp_path / "home")
    assert (tmp_path / "home").is_dir()
    assert "CODEX_HOME" not in child
    assert child["PATH"].split(":") == [str(tmp_path / "tools/gobby/venv/bin"), "/usr/bin", "/bin"]
    assert ambient["GOBBY_HOME"] == "/live"


def test_child_inherits_only_allowlisted_ambient_names(tmp_path: Path) -> None:
    unexpected = {
        "AWS_SECRET_ACCESS_KEY": "synthetic-aws",
        "GH_TOKEN": "synthetic-gh",
        "SSH_AUTH_SOCK": "/tmp/synthetic-agent.sock",
        "HTTPS_PROXY": "http://synthetic-proxy:3128",
        "PYTHONSTARTUP": "/tmp/synthetic-startup.py",
        "UV_CACHE_DIR": "/tmp/synthetic-uv-cache",
        "FOO_TOKEN": "synthetic-foo",
    }
    allowed = {"LANG": "en_US.UTF-8", "LC_ALL": "C", "TERM": "xterm", "TMPDIR": "/tmp/x"}
    child = isolated_environment(tmp_path, {**unexpected, **allowed, "HOME": "/Users/operator"})
    assert not set(unexpected).intersection(child)
    assert not any(value in child.values() for value in unexpected.values())
    assert {name: child[name] for name in allowed} == allowed
    assert child["HOME"] == str(tmp_path / "home")
    harness_keys = {
        "HOME",
        "GOBBY_HOME",
        "GOBBY_NATIVE_BIN_DIR",
        "GOBBY_TEST_PROTECT",
        "PYTHONDONTWRITEBYTECODE",
        "PATH",
    }
    assert set(child) == set(allowed) | harness_keys


def test_preparation_receipts_preserve_each_revision_and_resume(tmp_path: Path) -> None:
    record_preparation(tmp_path, 1, [])
    first = (tmp_path / "receipts/daemon-preparation-rev-1.json").read_bytes()
    record_preparation(tmp_path, 4, ["retired.key"])
    record_preparation(tmp_path, 4, ["retired.key"])
    assert (tmp_path / "receipts/daemon-preparation-rev-1.json").read_bytes() == first
    latest = json.loads((tmp_path / "receipts/daemon-preparation-rev-4.json").read_text())
    assert latest["config_revision"] == 4
    assert latest["gobby_home"] == str(tmp_path / "gobby-home")
    assert latest["automatic_indexing"] is False
    assert latest["dropped_config_keys"] == ["retired.key"]


def test_known_config_values_drops_unregistered_keys() -> None:
    kept, dropped = known_config_values({"test_mode": True, "zz_retired_bakeoff_key": 1})
    assert kept == {"test_mode": True}
    assert dropped == ["zz_retired_bakeoff_key"]


class _NoGobbyImports(MetaPathFinder):
    """Fails any gobby import in this process, so step 3 can reach gobby only through the runtime."""

    def find_spec(self, fullname: str, path: object, target: object = None) -> None:
        if fullname == "gobby" or fullname.startswith("gobby."):
            raise AssertionError(f"in-process import of {fullname} during step 3")
        return None


def _forbid_in_process_gobby(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cached gobby modules would satisfy an import without consulting the finder, so drop them.
    for name in [name for name in sys.modules if name == "gobby" or name.startswith("gobby.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoGobbyImports(), *sys.meta_path])


def _runtime_root(tmp_path: Path) -> Path:
    """A runtime root whose interpreter stands in for the gobby-sync environment.

    The stand-in logs the interpreter path and operation of each call, then answers with the
    output the real worker prints.
    """
    root = tmp_path / "root"
    (root / "sources/gobby").mkdir(parents=True)
    interpreter = root / "tools/gobby/venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    identity = json.dumps(
        {
            "gobby": str(root / "sources/gobby/src/gobby/__init__.py"),
            "gdaemon": str(root / "gobby-home/bin/gdaemon"),
        }
    )
    answers = {
        "prepare-daemon": json.dumps({"revision": 1, "dropped_keys": []}),
        "prepare-identity": json.dumps(
            {"user_id": "user-1", "email": "bakeoff-22948@example.invalid", "created": True}
        ),
    }
    cases = "".join(
        f"  {operation}) printf '%s\\n' '{answer}' ;;\n" for operation, answer in answers.items()
    )
    interpreter.write_text(
        "#!/bin/sh\n"
        f"printf '%s %s\\n' \"$0\" \"$3\" >> '{root / 'invocations.txt'}'\n"
        'case "$3" in\n'
        f"{cases}"
        f"  *) printf '%s\\n' '{identity}' ;;\n"
        "esac\n"
    )
    interpreter.chmod(0o755)
    return root


def _invocations(root: Path) -> list[str]:
    return (root / "invocations.txt").read_text().splitlines()


def _services_env(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config/services.env").write_text(
        "BAKEOFF_POSTGRES_USER=gobby_bakeoff\n"
        "BAKEOFF_POSTGRES_PASSWORD=bakeoff-secret\n"
        "BAKEOFF_POSTGRES_DB=gobby_bakeoff_22948\n"
        "BAKEOFF_FALKORDB_PASSWORD=falkor-secret\n"
    )


def _bootstrap(root: Path) -> None:
    (root / "gobby-home").mkdir(parents=True)
    (root / "gobby-home/bootstrap.yaml").write_text(
        "database_url: postgresql://gobby_bakeoff:bakeoff-secret@127.0.0.1:61234/gobby_bakeoff_22948\n"
    )


class _Listing:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Listing:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _provider_answers(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[str]:
    requested: list[str] = []

    class _Opener:
        def open(self, url: str, timeout: float) -> _Listing:
            requested.append(url)
            return _Listing(body)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_h: _Opener())
    return requested


def test_the_configured_embedding_provider_is_ollama_with_its_model_and_dimension() -> None:
    assert prepare_daemon.EMBEDDING_VALUES == {
        "ai.embeddings.model": "qwen3-embedding:0.6b-q8_0",
        "ai.embeddings.api_base": "http://localhost:11434/v1",
        "ai.embeddings.dim": 1024,
    }


def test_readiness_passes_when_the_configured_provider_lists_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = json.dumps({"data": [{"id": "qwen3-embedding:0.6b-q8_0"}]}).encode()
    requested = _provider_answers(monkeypatch, listing)

    prepare_daemon.embedding_readiness("http://localhost:11434/v1", "qwen3-embedding:0.6b-q8_0")

    assert requested == ["http://localhost:11434/v1/models"]


def test_readiness_refuses_a_provider_that_does_not_list_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider_answers(monkeypatch, json.dumps({"data": [{"id": "nomic-embed-text"}]}).encode())

    with pytest.raises(EvidenceError, match="qwen3-embedding:0.6b-q8_0"):
        prepare_daemon.embedding_readiness("http://localhost:11434/v1", "qwen3-embedding:0.6b-q8_0")


def test_readiness_refuses_a_listing_whose_ids_are_not_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = {"data": [{"id": ["qwen3-embedding:0.6b-q8_0"]}]}
    _provider_answers(monkeypatch, json.dumps(listing).encode())

    with pytest.raises(EvidenceError, match="does not list"):
        prepare_daemon.embedding_readiness("http://localhost:11434/v1", "qwen3-embedding:0.6b-q8_0")


def test_readiness_refuses_an_unreachable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Refused:
        def open(self, url: str, timeout: float) -> _Listing:
            raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_h: _Refused())

    with pytest.raises(EvidenceError, match="unreachable"):
        prepare_daemon.embedding_readiness("http://localhost:11434/v1", "qwen3-embedding:0.6b-q8_0")


def test_prepare_checks_the_embedding_provider_before_it_writes_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(api_base: str, model: str) -> None:
        raise EvidenceError("embedding provider refused")

    monkeypatch.setattr(prepare_daemon, "embedding_readiness", refused)

    with pytest.raises(EvidenceError, match="refused"):
        prepare_daemon.prepare(tmp_path)

    assert not (tmp_path / "gobby-home").exists()


def test_prepare_runs_every_gobby_step_under_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    _services_env(root)
    _forbid_in_process_gobby(monkeypatch)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    monkeypatch.setattr(prepare_daemon, "preserve_image_probe", lambda _dsn: None)
    monkeypatch.setattr(prepare_daemon, "embedding_readiness", lambda _api, _model: None)
    runtime = str(root / "tools/gobby/venv/bin/python")

    prepare_daemon.prepare(root)

    assert _invocations(root) == [f"{runtime} identity", f"{runtime} prepare-daemon"]
    assert (root / "receipts/daemon-preparation-rev-1.json").is_file()


def test_worker_refuses_a_gobby_outside_the_runtime_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # This process's gobby is the epic tree, so the worker's own check must refuse it.
    (tmp_path / "sources/gobby").mkdir(parents=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert runtime_gobby.main(["runtime_gobby.py", "identity", str(tmp_path)]) == 2
    assert "resolves outside sources/gobby" in capsys.readouterr().err


def test_identity_runs_every_gobby_step_under_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    _bootstrap(root)
    _forbid_in_process_gobby(monkeypatch)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    runtime = str(root / "tools/gobby/venv/bin/python")

    prepare_identity.prepare_identity(root)

    assert _invocations(root) == [f"{runtime} prepare-identity"]
    assert json.loads((root / "receipts/daemon-identity.json").read_text()) == {"user_id": "user-1"}


ACCOUNT = {"email": "bakeoff-22948@example.invalid", "password": "generated-secret"}
DATABASE_URL = "postgresql://gobby_bakeoff:bakeoff-secret@127.0.0.1:61234/gobby_bakeoff_22948"


def _fake_identity_modules(
    monkeypatch: pytest.MonkeyPatch, created_seen: list[bool], account_path: Path
) -> None:
    """Stand-ins for the gobby identity modules; create records whether the account file exists."""

    class Database:
        def __init__(self, _url: str) -> None:
            pass

        def close(self) -> None:
            pass

    class Users:
        def __init__(self, _db: object) -> None:
            pass

        def list(self) -> list[object]:
            return []

        def create(self, **_fields: object) -> None:
            created_seen.append(account_path.exists())

    fakes: dict[str, dict[str, object]] = {
        "gobby.identity": {"hash_password": lambda password: f"hash:{password}"},
        "gobby.cli.install_identity": {
            "ensure_install_identity": lambda _db, no_interactive: types.SimpleNamespace(
                id="user-1", email="bakeoff-22948@example.invalid"
            )
        },
        "gobby.storage.hub.postgres": {"PostgresHubDatabase": Database},
        "gobby.storage.users": {"LocalUserManager": Users},
    }
    for name, attributes in fakes.items():
        module = types.ModuleType(name)
        vars(module).update(attributes)
        monkeypatch.setitem(sys.modules, name, module)


def test_worker_writes_a_new_account_before_it_creates_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_path = tmp_path / "config/daemon-account.json"
    created_seen: list[bool] = []
    _fake_identity_modules(monkeypatch, created_seen, account_path)
    monkeypatch.setattr(runtime_gobby, "verify", lambda _root: {})

    runtime_gobby.prepare_identity(
        tmp_path,
        {"database_url": DATABASE_URL, "account": ACCOUNT, "account_path": str(account_path)},
    )

    assert created_seen == [True]
    assert json.loads(account_path.read_text()) == ACCOUNT


def test_worker_keeps_an_account_file_that_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_path = tmp_path / "config/daemon-account.json"
    account_path.parent.mkdir(parents=True)
    kept = {"email": "bakeoff-22948@example.invalid", "password": "kept-secret"}
    account_path.write_text(json.dumps(kept))
    _fake_identity_modules(monkeypatch, [], account_path)
    monkeypatch.setattr(runtime_gobby, "verify", lambda _root: {})

    runtime_gobby.prepare_identity(
        tmp_path, {"database_url": DATABASE_URL, "account": kept, "account_path": str(account_path)}
    )

    assert json.loads(account_path.read_text()) == kept


def test_binary_set_names_come_from_the_gobby_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name

    monkeypatch.setattr(runtime_gobby, "verify_gobby", lambda _root: {})

    result = runtime_gobby.binary_set_files(tmp_path, {})

    assert result == {
        "set_files": [native_bin_name(member) for member in SET_MEMBERS] + [IDENTITY_STAMP_NAME]
    }


def test_module_origins_are_resolved_by_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_gobby, "verify", lambda _root: {})

    result = runtime_gobby.module_origins(
        tmp_path, {"modules": ["json", "gobby_no_such_module_for_bakeoff"]}
    )

    assert result == {"origins": {"json": json.__file__, "gobby_no_such_module_for_bakeoff": None}}


def test_a_kill_after_the_create_leaves_no_account_file_under_its_name(tmp_path: Path) -> None:
    # os._exit skips every cleanup, so the child is killed without any exception handler running.
    account_path = tmp_path / "config/daemon-account.json"
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "import runtime_gobby\n"
        "real_open = os.open\n"
        "def open_then_die(*args, **kwargs):\n"
        "    real_open(*args, **kwargs)\n"
        "    os._exit(137)\n"
        "os.open = open_then_die\n"
        f"runtime_gobby.write_account_once(Path({str(account_path)!r}), {ACCOUNT!r})\n"
    )

    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert completed.returncode == 137, completed.stderr
    assert not account_path.exists()


def test_package_files_are_hashed_by_the_runtime_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "sources/gobby/src/gobby"
    (package / "utils").mkdir(parents=True)
    (package / "__pycache__").mkdir()
    (package / "__init__.py").write_bytes(b"init")
    (package / "utils/text.py").write_bytes(b"text")
    (package / "storage").mkdir()
    (package / "storage/schema_expected_identity.json").write_bytes(b"identity")
    (package / "utils/stray.pyc").write_bytes(b"stray")
    (package / "__pycache__/text.cpython-313.pyc").write_bytes(b"compiled")
    monkeypatch.setattr(
        runtime_gobby, "verify_gobby", lambda _root: {"gobby": str(package / "__init__.py")}
    )

    result = runtime_gobby.package_files(tmp_path, {})

    assert result == {
        "files": {
            "gobby/__init__.py": hashlib.sha256(b"init").hexdigest(),
            "gobby/storage/schema_expected_identity.json": hashlib.sha256(b"identity").hexdigest(),
            "gobby/utils/text.py": hashlib.sha256(b"text").hexdigest(),
        }
    }


def test_a_stale_staging_file_from_a_killed_run_is_removed(tmp_path: Path) -> None:
    account_path = tmp_path / "config/daemon-account.json"
    account_path.parent.mkdir(parents=True)
    stale = account_path.parent / ".daemon-account.json.x1y2z3.tmp"
    stale.write_text(json.dumps(ACCOUNT))
    fresh = {"email": "bakeoff-22948@example.invalid", "password": "fresh-secret"}

    runtime_gobby.write_account_once(account_path, fresh)

    assert not stale.exists()
    assert json.loads(account_path.read_text()) == fresh
