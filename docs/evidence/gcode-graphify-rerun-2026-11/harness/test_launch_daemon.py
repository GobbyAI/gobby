"""The isolated daemon launch goes through the SRT runner with a policy confined to R."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import launch_daemon
import pytest
import runtime_gobby
from launch_daemon import SrtRunner, daemon_argv, launch, srt_policy, srt_wrapped_argv
from runtime_boundary import EvidenceError, file_record, verify_file_record

ROOT = Path("/runtime/root")
HOME = Path("/operator/home")
BOOTSTRAP = ROOT / "gobby-home/bootstrap.yaml"
SRT = SrtRunner(
    node=Path("/opt/node/bin/node"),
    runner=Path("/opt/srt/0.0.79/runner.mjs"),
    monitor_socket=None,
)
OPERATOR_DENY_READS = [
    "/operator/home/.gobby/bootstrap.yaml",
    "/operator/home/.gobby/.secret_kek",
    "/operator/home/.gobby/.secret_salt",
    "/operator/home/.gobby/local_cli_token",
    "/operator/home/.codex/auth.json",
]


def test_daemon_argv_runs_the_venv_gobby_runner_on_the_bootstrap_config() -> None:
    assert daemon_argv(ROOT, BOOTSTRAP) == [
        "/runtime/root/tools/gobby/venv/bin/python",
        "-m",
        "gobby.runner",
        "--config",
        "/runtime/root/gobby-home/bootstrap.yaml",
    ]


def test_srt_policy_allows_writes_only_under_the_runtime_root() -> None:
    filesystem = srt_policy(ROOT, HOME)["filesystem"]

    assert filesystem["allowWrite"] == ["/runtime/root"]
    assert filesystem["allowRead"] == ["/runtime/root"]
    assert filesystem["denyWrite"] == []


def test_srt_policy_denies_reads_of_the_five_operator_paths() -> None:
    filesystem = srt_policy(ROOT, HOME)["filesystem"]

    assert filesystem["denyRead"] == OPERATOR_DENY_READS


def test_srt_policy_permits_loopback_network_only() -> None:
    network = srt_policy(ROOT, HOME)["network"]

    assert network["allowedDomains"] == ["localhost", "127.0.0.1"]
    assert network["deniedDomains"] == []
    assert network["strictAllowlist"] is True
    assert network["allowLocalBinding"] is True
    assert network["allowUnixSockets"] == []
    assert network["allowAllUnixSockets"] is False


def test_srt_wrapped_argv_follows_the_runtime_wrap_shape() -> None:
    wrapped = srt_wrapped_argv(
        daemon_argv(ROOT, BOOTSTRAP),
        policy=ROOT / "srt/settings.json",
        violations=ROOT / "logs/violations.jsonl",
        srt=SRT,
    )

    assert wrapped == [
        "/opt/node/bin/node",
        "/opt/srt/0.0.79/runner.mjs",
        "--settings",
        "/runtime/root/srt/settings.json",
        "--violations",
        "/runtime/root/logs/violations.jsonl",
        "--",
        "/runtime/root/tools/gobby/venv/bin/python",
        "-m",
        "gobby.runner",
        "--config",
        "/runtime/root/gobby-home/bootstrap.yaml",
    ]


class _FakeProcess:
    pid = 4242


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def bind(self, address: tuple[str, int]) -> None:
        return None


@pytest.fixture
def staged_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "gobby-home").mkdir(parents=True)
    (root / "gobby-home/bootstrap.yaml").write_text(
        "bind_host: 127.0.0.1\ndaemon_port: 59480\nwebsocket_port: 59481\n"
    )
    (root / "logs/install").mkdir(parents=True)
    (root / "receipts").mkdir()
    return root


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    argvs: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        argvs.append(argv)
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    return argvs


def test_launch_runs_the_daemon_wrapped_by_srt_and_records_that_argv(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    argv = spawned[0]
    assert argv[:7] == [
        "/opt/node/bin/node",
        "/opt/srt/0.0.79/runner.mjs",
        "--settings",
        str(staged_root / "srt/settings-attempt-1.json"),
        "--violations",
        str(staged_root / "logs/violations-attempt-1.jsonl"),
        "--",
    ]
    assert argv[7:] == daemon_argv(staged_root, staged_root / "gobby-home/bootstrap.yaml")
    receipt = json.loads((staged_root / "receipts/daemon-launch-attempt-1.json").read_text())
    assert receipt["argv"] == argv
    assert receipt["daemon_argv"] == argv[7:]


def test_launch_hands_the_daemon_the_shared_monitor_socket_on_darwin(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envs: list[dict[str, str]] = []

    def fake_popen(argv: list[str], *, env: dict[str, str], **kwargs: object) -> _FakeProcess:
        envs.append(env)
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    monitored = SrtRunner(
        node=SRT.node,
        runner=SRT.runner,
        monitor_socket="/private/tmp/srt-monitor/v-abc",
    )

    launch(staged_root, "attempt-1", monitored, home=tmp_path / "home")

    assert envs[0]["GOBBY_SRT_MONITOR_SOCKET"] == "/private/tmp/srt-monitor/v-abc"
    assert envs[0]["GOBBY_SRT_RUN_KEY"] == "attempt-1"


def test_launch_receipt_names_the_monitor_socket_env_and_path(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: _FakeProcess())
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    monitored = SrtRunner(
        node=SRT.node, runner=SRT.runner, monitor_socket="/private/tmp/srt-monitor/v-abc"
    )

    launch(staged_root, "attempt-1", monitored, home=tmp_path / "home")

    sandbox = json.loads((staged_root / "receipts/daemon-launch-attempt-1.json").read_text())[
        "sandbox"
    ]
    assert sandbox["monitor_socket_env"] == "GOBBY_SRT_MONITOR_SOCKET"
    assert sandbox["monitor_socket"] == "/private/tmp/srt-monitor/v-abc"
    assert sandbox["run_key"] == "attempt-1"


def test_launch_indexes_the_daemon_receipt_as_the_active_launch(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    index = json.loads((staged_root / "receipts/active-launches.json").read_text())
    assert set(index) == {"daemon"}
    assert index["daemon"] == file_record(staged_root, "receipts/daemon-launch-attempt-1.json")
    verify_file_record(staged_root, index["daemon"])


def test_launch_refuses_a_root_with_an_active_launch_before_starting_a_daemon(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    with pytest.raises(EvidenceError):
        launch(staged_root, "attempt-2", SRT, home=tmp_path / "home")
    assert len(spawned) == 1


def test_launch_writes_a_root_confined_policy_and_an_empty_violation_log(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    policy_path = staged_root / "srt/settings-attempt-1.json"
    policy = json.loads(policy_path.read_text())
    assert policy == srt_policy(staged_root, tmp_path / "home")
    assert policy["filesystem"]["allowWrite"] == [str(staged_root)]
    violations_path = staged_root / "logs/violations-attempt-1.jsonl"
    assert violations_path.read_text() == ""
    assert policy_path.stat().st_mode & 0o777 == 0o600
    assert violations_path.stat().st_mode & 0o777 == 0o600


GOBBY_MODULE = "gobby.utils.native_bin"


def _module_copy(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_matching_module_copies_pass_and_record_both_hashes(tmp_path: Path) -> None:
    epic = _module_copy(tmp_path / "epic/native_bin.py", b"same bytes")
    venv = _module_copy(tmp_path / "venv/native_bin.py", b"same bytes")

    record = launch_daemon.compare_module_copies(
        [GOBBY_MODULE], {GOBBY_MODULE: epic}, {GOBBY_MODULE: venv}
    )

    digest = hashlib.sha256(b"same bytes").hexdigest()
    assert record == {
        GOBBY_MODULE: {
            "epic_tree": {"path": str(epic), "sha256": digest},
            "runtime_venv": {"path": str(venv), "sha256": digest},
        }
    }


def test_mismatched_module_copy_stops_and_names_the_module(tmp_path: Path) -> None:
    epic = _module_copy(tmp_path / "epic/native_bin.py", b"epic bytes")
    venv = _module_copy(tmp_path / "venv/native_bin.py", b"runtime bytes")

    with pytest.raises(EvidenceError, match=r"gobby\.utils\.native_bin"):
        launch_daemon.compare_module_copies(
            [GOBBY_MODULE], {GOBBY_MODULE: epic}, {GOBBY_MODULE: venv}
        )


def test_missing_module_copy_stops_and_names_the_module(tmp_path: Path) -> None:
    epic = _module_copy(tmp_path / "epic/native_bin.py", b"same bytes")

    with pytest.raises(EvidenceError, match=r"gobby\.utils\.native_bin"):
        launch_daemon.compare_module_copies(
            [GOBBY_MODULE], {GOBBY_MODULE: epic}, {GOBBY_MODULE: None}
        )
    with pytest.raises(EvidenceError, match=r"gobby\.utils\.native_bin"):
        launch_daemon.compare_module_copies(
            [GOBBY_MODULE],
            {GOBBY_MODULE: epic},
            {GOBBY_MODULE: tmp_path / "venv/absent.py"},
        )


def test_provisioning_table_lists_exactly_the_step_module_set() -> None:
    doc = (Path(__file__).parents[1] / "provisioning.md").read_text()
    listed = {line.split("`")[1] for line in doc.splitlines() if line.startswith("| `gobby.")}

    assert listed == set(launch_daemon.GOBBY_STEP_MODULES)


def _origin_path(name: str) -> str:
    return "gobby/__init__.py" if name == "gobby" else name.replace(".", "/") + ".py"


def _package_tree(base: Path) -> Path:
    """A gobby package: its init, the four step modules, one module outside the step set, and the
    schema identity file that step 8 reads from the package."""
    for name in [*launch_daemon.GOBBY_STEP_MODULES, "gobby", "gobby.utils.text"]:
        _module_copy(base / _origin_path(name), b"same bytes")
    _module_copy(base / "gobby/storage/schema_expected_identity.json", b"same bytes")
    return base


def _patch_copies(monkeypatch: pytest.MonkeyPatch, epic: Path, venv: Path) -> None:
    monkeypatch.setattr(
        launch_daemon,
        "epic_origins",
        lambda names: {name: epic / _origin_path(name) for name in names},
    )

    def runtime(
        _root: Path, _env: dict[str, str], operation: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if operation == "package-files":
            return {"files": runtime_gobby.hash_package_files(venv / "gobby")}
        return {"origins": {name: str(venv / _origin_path(name)) for name in payload["modules"]}}

    monkeypatch.setattr(launch_daemon, "run_runtime_gobby", runtime)


def test_module_identity_receipt_records_both_hashes_and_the_package_for_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_copies(monkeypatch, _package_tree(tmp_path / "epic"), _package_tree(tmp_path / "venv"))
    root = tmp_path / "root"
    (root / "receipts").mkdir(parents=True)

    launch_daemon.verify_gobby_modules(root, "attempt-1", {})

    receipt = json.loads((root / "receipts/gobby-module-identity-attempt-1.json").read_text())
    assert set(receipt) == {"modules", "package"}
    for name in launch_daemon.GOBBY_STEP_MODULES:
        module = receipt["modules"][name]
        assert module["epic_tree"]["sha256"] == module["runtime_venv"]["sha256"]
    assert receipt["package"]["files"] == 7
    assert receipt["package"]["epic_tree_digest"] == receipt["package"]["runtime_venv_digest"]


def test_one_changed_byte_in_a_non_direct_module_fails_the_aggregate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epic = _package_tree(tmp_path / "epic")
    venv = _package_tree(tmp_path / "venv")
    (venv / "gobby/utils/text.py").write_bytes(b"same bytez")
    _patch_copies(monkeypatch, epic, venv)
    root = tmp_path / "root"
    (root / "receipts").mkdir(parents=True)

    with pytest.raises(EvidenceError, match=r"^gobby/utils/text\.py: epic-tree and tools"):
        launch_daemon.verify_gobby_modules(root, "attempt-1", {})
    assert not (root / "receipts/gobby-module-identity-attempt-1.json").exists()


def test_a_package_file_on_one_side_only_stops_and_names_the_first_such_path() -> None:
    with pytest.raises(EvidenceError, match=r"^gobby/b\.py: only in the epic-tree"):
        launch_daemon.compare_package_copies(
            {"gobby/a.py": "1", "gobby/b.py": "2"}, {"gobby/a.py": "1", "gobby/c.py": "3"}
        )


def test_matching_package_copies_record_both_digests_and_the_file_count() -> None:
    files = {"gobby/__init__.py": "aa", "gobby/utils/text.py": "bb"}

    record = launch_daemon.compare_package_copies(files, dict(files))

    assert record == {
        "files": 2,
        "epic_tree_digest": runtime_gobby.package_digest(files),
        "runtime_venv_digest": runtime_gobby.package_digest(files),
    }


def test_one_changed_byte_in_the_schema_identity_file_fails_the_aggregate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epic = _package_tree(tmp_path / "epic")
    venv = _package_tree(tmp_path / "venv")
    (venv / "gobby/storage/schema_expected_identity.json").write_bytes(b"same bytez")
    _patch_copies(monkeypatch, epic, venv)
    root = tmp_path / "root"
    (root / "receipts").mkdir(parents=True)

    with pytest.raises(
        EvidenceError,
        match=r"^gobby/storage/schema_expected_identity\.json: epic-tree and tools",
    ):
        launch_daemon.verify_gobby_modules(root, "attempt-1", {})
    assert not (root / "receipts/gobby-module-identity-attempt-1.json").exists()


def test_a_non_python_package_file_on_one_side_only_fails_naming_it(tmp_path: Path) -> None:
    epic = _package_tree(tmp_path / "epic")
    venv = _package_tree(tmp_path / "venv")
    _module_copy(epic / "gobby/install/shared/rule.yaml", b"rule")

    with pytest.raises(
        EvidenceError, match=r"^gobby/install/shared/rule\.yaml: only in the epic-tree"
    ):
        launch_daemon.compare_package_copies(
            runtime_gobby.hash_package_files(epic / "gobby"),
            runtime_gobby.hash_package_files(venv / "gobby"),
        )
