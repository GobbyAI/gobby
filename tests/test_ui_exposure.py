from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from gobby import ui_exposure
from gobby.config.bootstrap import load_bootstrap
from gobby.ui_exposure import UiExposeError

_DNS_NAME = "node.example.ts.net"
_HOST_PORT = f"{_DNS_NAME}:443"
_STATUS = {
    "BackendState": "Running",
    "Self": {"DNSName": f"{_DNS_NAME}."},
    "CurrentTailnet": {"MagicDNSEnabled": True},
}


def _write_bootstrap(path: Path, *, exposed: bool = False) -> None:
    files_home = path.parent / "files"
    files_home.mkdir(exist_ok=True)
    lines = [
        "datastore_mode: local",
        f"files_home: {files_home}",
    ]
    if exposed:
        lines.append("ui_expose: tailscale")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def _serve_config(
    root_proxy: str | None = None,
    *,
    funnel: bool = False,
    tcp: object = None,
    sibling: bool = False,
) -> dict[str, object]:
    handlers: dict[str, object] = {}
    if root_proxy is not None:
        handlers["/"] = {"Proxy": root_proxy}
    if sibling:
        handlers["/metrics"] = {"Proxy": "http://localhost:9999"}
    return {
        "TCP": {"443": {"HTTPS": True}} if tcp is None else tcp,
        "Web": {_HOST_PORT: {"Handlers": handlers}} if handlers else {},
        "AllowFunnel": {_HOST_PORT: True} if funnel else None,
    }


def _mock_json(monkeypatch: pytest.MonkeyPatch, *responses: object) -> Mock:
    mock = Mock(side_effect=responses)
    monkeypatch.setattr(ui_exposure, "_run_json", mock)
    return mock


def test_enable_installs_root_handler_and_persists_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config(tcp={}),
        _STATUS,
        _serve_config("http://localhost:60887", sibling=True),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    result = ui_exposure.enable_tailscale_ui(60887, config_path=path)

    assert result.url == f"https://{_DNS_NAME}/"
    assert result.changed is True
    mutation.assert_called_once_with(
        [
            "serve",
            "--yes",
            "--bg",
            "--https=443",
            "--set-path=/",
            "http://localhost:60887",
        ]
    )
    assert load_bootstrap(str(path)).ui_expose == "tailscale"


def test_enable_adopts_equivalent_manual_mapping_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    probe = _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config("http://127.0.0.1:60887/", sibling=True),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    result = ui_exposure.enable_tailscale_ui(60887, config_path=path)

    assert result.changed is False
    assert probe.call_count == 2
    mutation.assert_not_called()
    assert load_bootstrap(str(path)).ui_expose == "tailscale"


@pytest.mark.parametrize(
    ("serve_config", "message"),
    [
        (_serve_config(tcp={"443": {"HTTP": True}}), "conflicting protocol"),
        (_serve_config("http://localhost:9999"), "owned by another proxy"),
        (_serve_config(funnel=True), "Funnel is active"),
    ],
)
def test_enable_fails_closed_on_conflicting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serve_config: dict[str, object],
    message: str,
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    _mock_json(monkeypatch, _STATUS, serve_config)
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    with pytest.raises(UiExposeError, match=message):
        ui_exposure.enable_tailscale_ui(60887, config_path=path)

    mutation.assert_not_called()
    assert load_bootstrap(str(path)).ui_expose is None


def test_enable_verification_failure_does_not_remove_foreign_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config(tcp={}),
        _STATUS,
        _serve_config("http://localhost:9999"),
        _STATUS,
        _serve_config("http://localhost:9999"),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    with pytest.raises(UiExposeError, match="expected HTTPS root handler"):
        ui_exposure.enable_tailscale_ui(60887, config_path=path)

    assert mutation.call_count == 1
    assert load_bootstrap(str(path)).ui_expose is None


def test_enable_inconclusive_verification_removes_confirmed_new_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config(tcp={}),
        _STATUS,
        {"TCP": {"443": {"HTTPS": True}}, "Web": []},
        _STATUS,
        _serve_config("http://localhost:60887"),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    with pytest.raises(UiExposeError, match="Web state is malformed"):
        ui_exposure.enable_tailscale_ui(60887, config_path=path)

    assert mutation.call_args_list[1].args[0] == [
        "serve",
        "--yes",
        "--https=443",
        "--set-path=/",
        "off",
    ]
    assert load_bootstrap(str(path)).ui_expose is None


def test_enable_persistence_failure_rolls_back_new_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config(tcp={}),
        _STATUS,
        _serve_config("http://localhost:60887"),
        _STATUS,
        _serve_config("http://localhost:60887"),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)
    monkeypatch.setattr(
        ui_exposure,
        "update_bootstrap_yaml",
        Mock(side_effect=OSError("disk full")),
    )

    with pytest.raises(UiExposeError, match="failed to persist.*disk full"):
        ui_exposure.enable_tailscale_ui(60887, config_path=path)

    assert mutation.call_count == 2
    assert mutation.call_args_list[1].args[0][-1] == "off"


def test_disable_removes_root_then_clears_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path, exposed=True)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config("http://localhost:60887", sibling=True),
        _STATUS,
        _serve_config(sibling=True),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    result = ui_exposure.disable_tailscale_ui(60887, config_path=path)

    assert result.mode is None
    mutation.assert_called_once_with(["serve", "--yes", "--https=443", "--set-path=/", "off"])
    assert load_bootstrap(str(path)).ui_expose is None


def test_disable_persistence_failure_restores_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path, exposed=True)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config("http://localhost:60887"),
        _STATUS,
        _serve_config(tcp={}),
        _STATUS,
        _serve_config(tcp={}),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)
    monkeypatch.setattr(
        ui_exposure,
        "update_bootstrap_yaml",
        Mock(side_effect=OSError("read-only")),
    )

    with pytest.raises(UiExposeError, match="failed to clear.*read-only"):
        ui_exposure.disable_tailscale_ui(60887, config_path=path)

    assert mutation.call_count == 2
    assert mutation.call_args_list[1].args[0] == [
        "serve",
        "--yes",
        "--bg",
        "--https=443",
        "--set-path=/",
        "http://localhost:60887",
    ]


def test_disable_persistence_failure_does_not_overwrite_foreign_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path, exposed=True)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config("http://localhost:60887"),
        _STATUS,
        _serve_config(tcp={}),
        _STATUS,
        _serve_config("http://localhost:9999"),
    )
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)
    monkeypatch.setattr(
        ui_exposure,
        "update_bootstrap_yaml",
        Mock(side_effect=OSError("read-only")),
    )

    with pytest.raises(UiExposeError, match="failed to clear.*read-only"):
        ui_exposure.disable_tailscale_ui(60887, config_path=path)

    assert mutation.call_count == 1


def test_forget_clears_intent_without_tailscale_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path, exposed=True)

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        pytest.fail("forget must not call Tailscale")

    monkeypatch.setattr(ui_exposure, "_run_json", unexpected_call)
    monkeypatch.setattr(ui_exposure, "_run_mutation", unexpected_call)

    ui_exposure.disable_tailscale_ui(60887, config_path=path, forget=True)

    assert load_bootstrap(str(path)).ui_expose is None


def test_status_and_reconcile_skip_tailscale_without_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path)
    probe = Mock()
    mutation = Mock()
    monkeypatch.setattr(ui_exposure, "_run_json", probe)
    monkeypatch.setattr(ui_exposure, "_run_mutation", mutation)

    status = ui_exposure.get_ui_exposure_status(60887, config_path=path)
    reconciled = ui_exposure.reconcile_ui_exposure(60887, config_path=path)

    assert status.mode is None
    assert reconciled is None
    probe.assert_not_called()
    mutation.assert_not_called()


def test_status_reports_healthy_and_degraded_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path, exposed=True)
    _mock_json(
        monkeypatch,
        _STATUS,
        _serve_config("http://localhost:60887"),
        _STATUS,
        _serve_config(tcp={}),
    )

    healthy = ui_exposure.get_ui_exposure_status(60887, config_path=path)
    degraded = ui_exposure.get_ui_exposure_status(60887, config_path=path)

    assert healthy.url == f"https://{_DNS_NAME}/"
    assert healthy.degraded_reason is None
    assert degraded.url is None
    assert degraded.degraded_reason == "HTTPS root handler is missing"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ({**_STATUS, "BackendState": "Stopped"}, "backend is not running"),
        (
            {**_STATUS, "CurrentTailnet": {"MagicDNSEnabled": False}},
            "MagicDNS is disabled",
        ),
        ({**_STATUS, "Self": {"DNSName": ""}}, "missing this node's MagicDNS name"),
    ],
)
def test_node_probe_fails_closed(
    monkeypatch: pytest.MonkeyPatch, status: dict[str, object], message: str
) -> None:
    _mock_json(monkeypatch, status)

    with pytest.raises(UiExposeError, match=message):
        ui_exposure._read_node_info()


def test_json_probe_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.ui_exposure.shutil.which", Mock(return_value=None))

    with pytest.raises(UiExposeError, match="executable was not found"):
        ui_exposure._run_json(["status", "--json"])


@pytest.mark.parametrize(
    ("side_effect", "completed", "message"),
    [
        (subprocess.TimeoutExpired("tailscale", 5), None, "timed out"),
        (None, subprocess.CompletedProcess([], 1, "", "backend error"), "backend error"),
        (None, subprocess.CompletedProcess([], 0, "not-json", ""), "malformed JSON"),
    ],
)
def test_json_probe_reports_command_failures(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: BaseException | None,
    completed: subprocess.CompletedProcess[str] | None,
    message: str,
) -> None:
    monkeypatch.setattr("gobby.ui_exposure.shutil.which", lambda _name: "/bin/tailscale")

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if side_effect is not None:
            raise side_effect
        assert completed is not None
        return completed

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(UiExposeError, match=message):
        ui_exposure._run_json(["status", "--json"])


def test_mutation_is_blocked_by_test_protect(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    with pytest.raises(UiExposeError, match="GOBBY_TEST_PROTECT"):
        ui_exposure._run_mutation(["serve", "off"])

    run.assert_not_called()


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("http://localhost:60887", True),
        ("http://localhost:60887/", True),
        ("http://127.0.0.1:60887", True),
        ("https://localhost:60887", False),
        ("http://localhost:60888", False),
        ("http://example.com:60887", False),
        ("http://localhost:60887/admin", False),
    ],
)
def test_proxy_target_comparison_is_semantic(target: str, expected: bool) -> None:
    assert ui_exposure._is_gobby_proxy(target, 60887) is expected


def test_installer_consent_is_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    confirm = Mock(return_value=True)
    probe = _mock_json(monkeypatch, _STATUS)

    assert (
        ui_exposure.resolve_installer_ui_exposure(
            None,
            full_install=True,
            no_interactive=False,
            confirm=confirm,
        )
        is True
    )
    assert probe.call_count == 1
    confirm.assert_called_once_with()

    probe.reset_mock()
    confirm.reset_mock()
    assert (
        ui_exposure.resolve_installer_ui_exposure(
            None,
            full_install=False,
            no_interactive=False,
            confirm=confirm,
        )
        is False
    )
    probe.assert_not_called()
    confirm.assert_not_called()
    assert (
        ui_exposure.resolve_installer_ui_exposure(
            None,
            full_install=True,
            no_interactive=True,
            confirm=confirm,
        )
        is False
    )
    probe.assert_not_called()
    confirm.assert_not_called()
    assert (
        ui_exposure.resolve_installer_ui_exposure(
            True,
            full_install=False,
            no_interactive=True,
            confirm=confirm,
        )
        is True
    )
    probe.assert_not_called()
    confirm.assert_not_called()
    assert (
        ui_exposure.resolve_installer_ui_exposure(
            False,
            full_install=True,
            no_interactive=False,
            confirm=confirm,
        )
        is False
    )
    probe.assert_not_called()
    confirm.assert_not_called()
