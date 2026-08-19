"""Contract and end-to-end smoke tests for the Gobby log collector example."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

REPO_ROOT = Path(__file__).parents[2]
EXAMPLE_DIR = REPO_ROOT / "docs" / "examples" / "otel-collector"
CONFIG_PATH = EXAMPLE_DIR / "gobby-logs.yaml"
README_PATH = EXAMPLE_DIR / "README.md"
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.156.0"
COLLECTOR_VERSION = "0.156.0"

RECEIVER_FILES = {
    "daemon": "daemon.log",
    "errors": "errors.log",
    "runtime": "runtime.log",
    "hooks": "hooks.log",
    "mcp": "mcp.log",
    "automation": "automation.log",
    "ui": "ui.log",
    "parser": "*-parser-error.log",
}
FORMATTED_SURFACES = {"daemon", "errors", "hooks", "mcp", "automation"}


def _load_config() -> dict[str, object]:
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_collector_config_covers_the_bounded_log_taxonomy() -> None:
    config = _load_config()
    receivers = config["receivers"]
    assert isinstance(receivers, dict)

    assert set(receivers) == {f"file_log/{surface}" for surface in RECEIVER_FILES}
    for surface, filename in RECEIVER_FILES.items():
        receiver = receivers[f"file_log/{surface}"]
        assert isinstance(receiver, dict)
        assert receiver["include"] == [f"${{env:GOBBY_LOG_DIR}}/{filename}"]
        assert receiver["start_at"] == "end"
        assert receiver["storage"] == "file_storage"
        assert receiver["on_truncate"] == "read_whole_file"
        assert receiver["include_file_name"] is True
        assert receiver["include_file_path"] is True
        assert receiver["attributes"] == {"gobby.log.surface": surface}
        assert receiver["retry_on_failure"] == {
            "enabled": True,
            "max_elapsed_time": "0s",
        }


@pytest.mark.unit
def test_collector_config_preserves_each_log_format() -> None:
    receivers = _load_config()["receivers"]
    assert isinstance(receivers, dict)

    for surface in FORMATTED_SURFACES:
        receiver = receivers[f"file_log/{surface}"]
        assert isinstance(receiver, dict)
        assert receiver["multiline"] == {
            "line_start_pattern": r"^(?:\{|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - )"
        }
        assert receiver["operators"] == [
            {
                "type": "json_parser",
                "if": 'body matches "^[[:space:]]*[{]"',
                "parse_to": "body",
                "on_error": "send_quiet",
            }
        ]

    for surface in ("runtime", "ui"):
        receiver = receivers[f"file_log/{surface}"]
        assert isinstance(receiver, dict)
        assert "multiline" not in receiver
        assert "operators" not in receiver

    parser = receivers["file_log/parser"]
    assert isinstance(parser, dict)
    assert parser["multiline"] == {"line_start_pattern": r"^\[\d{4}-\d{2}-\d{2}T"}
    assert "operators" not in parser


@pytest.mark.unit
def test_collector_config_uses_durable_otlp_delivery() -> None:
    config = _load_config()
    assert config["extensions"] == {
        "file_storage": {
            "directory": "${env:GOBBY_OTEL_STORAGE_DIR}",
            "create_directory": True,
            "fsync": True,
        }
    }
    assert config["exporters"] == {
        "otlp_http/generic": {
            "endpoint": "${env:GOBBY_OTLP_ENDPOINT}",
            "sending_queue": {"enabled": True, "storage": "file_storage"},
            "retry_on_failure": {"enabled": True, "max_elapsed_time": "0s"},
        }
    }

    service = config["service"]
    assert isinstance(service, dict)
    assert service["extensions"] == ["file_storage"]
    assert service["pipelines"] == {
        "logs/gobby": {
            "receivers": [
                "file_log/daemon",
                "file_log/errors",
                "file_log/runtime",
                "file_log/hooks",
                "file_log/mcp",
                "file_log/automation",
                "file_log/ui",
                "file_log/parser",
            ],
            "processors": ["batch"],
            "exporters": ["otlp_http/generic"],
        }
    }


@pytest.mark.unit
def test_collector_readme_pins_runtime_and_documents_loki() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert COLLECTOR_IMAGE in readme
    assert "GOBBY_OTLP_ENDPOINT=http://loki:3100/otlp" in readme
    assert "http://loki:3100/otlp/v1/logs" in readme
    assert "start_at: end" in readme
    assert "at-least-once delivery" in readme
    assert "telemetry.exporter.otlp_endpoint" in readme


@dataclass(frozen=True)
class _CollectorLaunch:
    use_docker: bool
    binary: str | None

    def version_command(self) -> list[str]:
        if self.binary is not None:
            return [self.binary, "--version"]
        return ["docker", "run", "--rm", COLLECTOR_IMAGE, "--version"]

    def argv(
        self,
        *collector_args: str,
        logs_dir: Path,
        state_dir: Path,
        endpoint: str,
    ) -> tuple[list[str], dict[str, str]]:
        if self.binary is not None:
            return (
                [self.binary, *collector_args],
                _collector_env(logs_dir, state_dir, endpoint),
            )
        remapped = [
            "--config=/etc/otelcol-contrib/config.yaml" if arg.startswith("--config=") else arg
            for arg in collector_args
        ]
        cmd = ["docker", "run", "--rm"]
        if sys.platform.startswith("linux"):
            cmd.extend(["--network", "host"])
        if hasattr(os, "getuid"):
            cmd.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        docker_endpoint = endpoint
        if not sys.platform.startswith("linux"):
            docker_endpoint = endpoint.replace("127.0.0.1", "host.docker.internal")
            docker_endpoint = docker_endpoint.replace("localhost", "host.docker.internal")
        cmd.extend(
            [
                "-e",
                "GOBBY_LOG_DIR=/logs",
                "-e",
                "GOBBY_OTEL_STORAGE_DIR=/state",
                "-e",
                f"GOBBY_OTLP_ENDPOINT={docker_endpoint}",
                "-v",
                f"{logs_dir.resolve()}:/logs",
                "-v",
                f"{state_dir.resolve()}:/state",
                "-v",
                f"{CONFIG_PATH.resolve()}:/etc/otelcol-contrib/config.yaml:ro",
                COLLECTOR_IMAGE,
                *remapped,
            ]
        )
        return cmd, os.environ.copy()


def _require_collector_smoke() -> _CollectorLaunch:
    if os.environ.get("GOBBY_OTEL_COLLECTOR_SMOKE") != "1":
        pytest.skip("set GOBBY_OTEL_COLLECTOR_SMOKE=1 to run the pinned collector")
    configured = os.environ.get("GOBBY_OTELCOL_BIN") or None
    collector = configured or shutil.which("otelcol-contrib")
    if collector is not None:
        launch = _CollectorLaunch(use_docker=False, binary=collector)
    elif shutil.which("docker") is not None:
        launch = _CollectorLaunch(use_docker=True, binary=None)
    else:
        pytest.fail(
            "otelcol-contrib not found; set GOBBY_OTELCOL_BIN or install docker "
            f"to run {COLLECTOR_IMAGE}"
        )
    result = subprocess.run(
        launch.version_command(),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    version_output = result.stdout + result.stderr
    assert result.returncode == 0, version_output
    assert COLLECTOR_VERSION in version_output, version_output
    return launch


def _collector_env(logs_dir: Path, state_dir: Path, endpoint: str) -> dict[str, str]:
    return {
        **os.environ,
        "GOBBY_LOG_DIR": str(logs_dir),
        "GOBBY_OTEL_STORAGE_DIR": str(state_dir),
        "GOBBY_OTLP_ENDPOINT": endpoint,
    }


@pytest.mark.unit
def test_require_collector_smoke_falls_back_to_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_OTEL_COLLECTOR_SMOKE", "1")
    monkeypatch.delenv("GOBBY_OTELCOL_BIN", raising=False)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert COLLECTOR_IMAGE in cmd
        assert "--version" in cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"otelcol-contrib version {COLLECTOR_VERSION}\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    launch = _require_collector_smoke()
    assert launch.use_docker
    argv, _env = launch.argv(
        "validate",
        f"--config={CONFIG_PATH}",
        logs_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        endpoint="http://127.0.0.1:4318",
    )
    assert argv[0] == "docker"
    assert COLLECTOR_IMAGE in argv
    assert "validate" in argv
    assert "--config=/etc/otelcol-contrib/config.yaml" in argv


@pytest.mark.unit
def test_require_collector_smoke_fails_without_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_OTEL_COLLECTOR_SMOKE", "1")
    monkeypatch.delenv("GOBBY_OTELCOL_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.fail.Exception, match="docker"):
        _require_collector_smoke()


@pytest.mark.integration
def test_pinned_collector_accepts_reference_config(tmp_path: Path) -> None:
    launch = _require_collector_smoke()
    logs_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    logs_dir.mkdir()
    state_dir.mkdir()
    argv, env = launch.argv(
        "validate",
        f"--config={CONFIG_PATH}",
        logs_dir=logs_dir,
        state_dir=state_dir,
        endpoint="http://127.0.0.1:4318",
    )

    result = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True)
class _ExportedLog:
    body: object
    attributes: dict[str, object]


class _Capture:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._records: list[_ExportedLog] = []

    def extend(self, records: list[_ExportedLog]) -> None:
        with self._condition:
            self._records.extend(records)
            self._condition.notify_all()

    def wait_for_count(self, count: int, timeout: float) -> list[_ExportedLog]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._records) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return list(self._records)


def _any_value(value: AnyValue) -> object:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {item.key: _any_value(item.value) for item in value.kvlist_value.values}
    return getattr(value, kind)


def _extract_logs(request: ExportLogsServiceRequest) -> list[_ExportedLog]:
    exported: list[_ExportedLog] = []
    for resource_logs in request.resource_logs:
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                exported.append(
                    _ExportedLog(
                        body=_any_value(record.body),
                        attributes={item.key: _any_value(item.value) for item in record.attributes},
                    )
                )
    return exported


def _make_otlp_handler(capture: _Capture) -> type[BaseHTTPRequestHandler]:
    class _OTLPHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "GobbyOTLPSink/1.0"
        sys_version = ""
        response_body: ClassVar[bytes] = ExportLogsServiceResponse().SerializeToString()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length)
            if self.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)

            request = ExportLogsServiceRequest()
            request.ParseFromString(payload)
            capture.extend(_extract_logs(request))

            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(self.response_body)))
            self.end_headers()
            self.wfile.write(self.response_body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return _OTLPHandler


def _write_line(path: Path, content: str, *, append: bool = False) -> None:
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _seed_log_files(logs_dir: Path) -> dict[str, Path]:
    paths = {
        surface: logs_dir / ("codex-parser-error.log" if surface == "parser" else filename)
        for surface, filename in RECEIVER_FILES.items()
    }
    for surface, path in paths.items():
        if surface == "parser":
            historical = (
                "[2026-07-17T00:00:00+00:00] line:1 session:old "
                "— Unknown block type: historical\n{}\n"
            )
        elif surface in FORMATTED_SURFACES:
            historical = json.dumps({"message": f"historical-{surface}"}) + "\n"
        else:
            historical = f"historical-{surface}\n"
        _write_line(path, historical)
    return paths


def _append_smoke_records(paths: dict[str, Path]) -> None:
    _write_line(
        paths["daemon"],
        "2026-07-17 12:00:00 - ERROR    - smoke.test - daemon-text-sentinel\n"
        "Traceback (most recent call last):\n"
        '  File "smoke.py", line 1, in <module>\n'
        "ValueError: daemon-traceback-sentinel\n"
        "{malformed-json-sentinel\n",
        append=True,
    )
    for surface in ("errors", "hooks", "mcp", "automation"):
        _write_line(
            paths[surface],
            json.dumps(
                {
                    "time": "2026-07-17T12:00:00",
                    "level": "INFO",
                    "message": f"{surface}-json-sentinel",
                }
            )
            + "\n",
            append=True,
        )
    _write_line(paths["runtime"], "runtime-raw-sentinel\n", append=True)
    _write_line(paths["ui"], "ui-raw-sentinel\n", append=True)
    _write_line(
        paths["parser"],
        "[2026-07-17T12:00:00+00:00] line:2 session:new "
        "— Unknown block type: parser-sentinel\n"
        '{"payload":"parser-payload-sentinel"}\n',
        append=True,
    )


class _CollectorOutput:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._watched_files = 0
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            with self._lock:
                self._lines.append(line)
                if "Started watching file" in line:
                    self._watched_files += 1
                    if self._watched_files == len(RECEIVER_FILES):
                        self._ready.set()

    def wait_until_ready(self, timeout: float) -> None:
        if self._ready.wait(timeout):
            return
        pytest.fail(f"collector did not become ready\n{self.text()}")

    def finish(self) -> str:
        self._thread.join(timeout=5)
        return self.text()

    def text(self) -> str:
        with self._lock:
            return "".join(self._lines)


def _stop_collector(process: subprocess.Popen[str], output: _CollectorOutput) -> str:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return output.finish()


@pytest.mark.integration
def test_collector_exports_all_surfaces_from_fresh_offsets(tmp_path: Path) -> None:
    launch = _require_collector_smoke()
    logs_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    logs_dir.mkdir()
    state_dir.mkdir()
    paths = _seed_log_files(logs_dir)

    capture = _Capture()
    listen_host = "127.0.0.1"
    if launch.use_docker and not sys.platform.startswith("linux"):
        listen_host = "0.0.0.0"
    server = ThreadingHTTPServer((listen_host, 0), _make_otlp_handler(capture))
    port = int(server.server_address[1])
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    argv, env = launch.argv(
        f"--config={CONFIG_PATH}",
        logs_dir=logs_dir,
        state_dir=state_dir,
        endpoint=f"http://127.0.0.1:{port}",
    )

    process = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = _CollectorOutput(process)

    collector_output = ""
    ready_timeout = 30 if launch.use_docker else 10
    export_timeout = 40 if launch.use_docker else 20
    try:
        output.wait_until_ready(timeout=ready_timeout)
        _append_smoke_records(paths)
        records = capture.wait_for_count(9, timeout=export_timeout)
    finally:
        collector_output = _stop_collector(process, output)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert len(records) == 9, collector_output
    serialized_bodies = json.dumps([record.body for record in records], default=str)
    assert "historical-" not in serialized_bodies

    by_surface: dict[str, list[_ExportedLog]] = {}
    for record in records:
        surface = record.attributes["gobby.log.surface"]
        assert isinstance(surface, str)
        by_surface.setdefault(surface, []).append(record)
        assert record.attributes["log.file.name"]
        path = record.attributes["log.file.path"]
        assert isinstance(path, str)
        assert path == str(paths[surface])

    assert set(by_surface) == set(RECEIVER_FILES)
    for surface in ("errors", "hooks", "mcp", "automation"):
        [record] = by_surface[surface]
        assert isinstance(record.body, dict)
        assert record.body["message"] == f"{surface}-json-sentinel"

    daemon_bodies = [record.body for record in by_surface["daemon"]]
    assert len(daemon_bodies) == 2
    assert any(
        isinstance(body, str)
        and "daemon-text-sentinel" in body
        and "daemon-traceback-sentinel" in body
        for body in daemon_bodies
    )
    assert "{malformed-json-sentinel" in daemon_bodies

    assert [record.body for record in by_surface["runtime"]] == ["runtime-raw-sentinel"]
    assert [record.body for record in by_surface["ui"]] == ["ui-raw-sentinel"]
    [parser_record] = by_surface["parser"]
    assert isinstance(parser_record.body, str)
    assert "parser-sentinel" in parser_record.body
    assert "parser-payload-sentinel" in parser_record.body
    assert parser_record.attributes["log.file.name"] == "codex-parser-error.log"
