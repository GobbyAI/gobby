"""Validate and probe the isolated runtime used for cohort execution."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import yaml
from ask_cohort_records import (
    _DATABASE_SCHEMA,
    _PASSTHROUGH_ENVIRONMENT_KEYS,
    EXPECTED_TOOL_IDENTITIES,
    REQUIRED_EXECUTION_GATES,
    CommandRunner,
    PreparationError,
    RuntimeServiceProbe,
    _canonical_json,
    _digest,
    _mapping,
    _normalize_daemon_url,
    _require_success,
    _sha256_bytes,
    _sha256_file,
    _string,
    _write_json_new,
    _write_new,
)
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver


class _RejectRedirects(urllib_request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib_request.Request,
        response: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _redirect_url: str,
    ) -> urllib_request.Request | None:
        response.close()
        raise PreparationError(
            f"daemon-backed runtime identity probe refused redirect for {request.selector}"
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    seen: set[object] = set()
    construct_object = cast(Callable[[yaml.Node, bool], object], loader.construct_object)
    for key_node, _value_node in node.value:
        key = construct_object(key_node, deep)
        try:
            duplicate = key in seen
            seen.add(key)
        except TypeError as error:
            raise ConstructorError(
                None,
                None,
                "bootstrap mapping keys must be scalar",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                None,
                None,
                f"duplicate bootstrap key {key!r}",
                key_node.start_mark,
            )
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _safe_load_unique(value: str) -> object:
    loader = _UniqueKeyLoader(value)
    try:
        return loader.get_single_data()
    finally:
        dispose = cast(Callable[[], None], loader.dispose)
        dispose()


def _database_identity_from_conninfo(value: str, *, name: str) -> dict[str, object]:
    try:
        connection = conninfo_to_dict(value)
    except (ProgrammingError, ValueError) as error:
        raise PreparationError(f"{name} is invalid") from error
    if connection.get("hostaddr") or connection.get("service"):
        raise PreparationError(f"{name} contains a destination override")
    raw_port = connection.get("port")
    try:
        port = int(raw_port) if raw_port is not None else None
    except ValueError as error:
        raise PreparationError(f"{name} port is invalid") from error
    options = connection.get("options")
    match = (
        re.fullmatch(r"-csearch_path=(gobby_test_[A-Za-z0-9_]+)", options)
        if isinstance(options, str)
        else None
    )
    if match is None:
        raise PreparationError(f"{name} must contain exactly one canonical search_path setting")
    return {
        "host": connection.get("host"),
        "port": port,
        "name": connection.get("dbname"),
        "schema": match.group(1),
    }


def _bootstrap_database_identity(value: str) -> dict[str, object]:
    try:
        endpoint = urlsplit(value)
        query = parse_qsl(endpoint.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise PreparationError("bootstrap database_url is invalid") from error
    if len(query) != 1 or query[0][0] != "options":
        raise PreparationError("bootstrap database_url must use exactly one canonical options key")
    options = query[0][1]
    if re.fullmatch(r"-csearch_path=gobby_test_[A-Za-z0-9_]+", options) is None:
        raise PreparationError(
            "bootstrap database_url must contain exactly one canonical search_path setting"
        )
    if endpoint.query != urlencode([("options", options)]):
        raise PreparationError("bootstrap database_url must use canonical options encoding")
    if endpoint.scheme not in {"postgres", "postgresql"} or endpoint.fragment:
        raise PreparationError("bootstrap database_url is invalid")
    return _database_identity_from_conninfo(value, name="bootstrap database_url")


def _private_owned_path(value: object, *, name: str, directory: bool) -> Path:
    raw = Path(_string(value, name=name))
    if not raw.is_absolute():
        raise PreparationError(f"{name} must be absolute")
    try:
        metadata = raw.lstat()
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise PreparationError(f"{name} is unavailable: {error}") from error
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if raw != resolved or stat.S_ISLNK(metadata.st_mode) or not expected_type(metadata.st_mode):
        raise PreparationError(
            f"{name} must be an owned non-symlink {'directory' if directory else 'file'}"
        )
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise PreparationError(f"{name} must be owner-only")
    return resolved


def _receipt_payload(value: object, *, name: str) -> tuple[dict[str, str], dict[str, Any]]:
    receipt = _mapping(value, name=f"{name} receipt")
    path = _private_owned_path(receipt.get("path"), name=f"{name} receipt path", directory=False)
    expected = _digest(receipt.get("sha256"), name=f"{name} receipt")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PreparationError(f"{name} receipt is unavailable: {error}") from error
    if _sha256_bytes(payload) != expected:
        raise PreparationError(f"{name} receipt hash changed")
    try:
        body = _mapping(json.loads(payload), name=f"{name} receipt body")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationError(f"{name} receipt must be JSON") from error
    return {"path": str(path), "sha256": expected}, body


def _runtime_isolation(value: object) -> dict[str, Any]:
    isolation = _mapping(value, name="runtime isolation")
    if isolation.get("mode") != "contained":
        raise PreparationError("runtime isolation mode must be contained")
    raw_daemon_url = _string(isolation.get("daemon_url"), name="isolated daemon_url")
    daemon_url = _normalize_daemon_url(raw_daemon_url)
    try:
        endpoint = urlsplit(daemon_url)
        port = endpoint.port
    except ValueError as error:
        raise PreparationError("isolated daemon_url is invalid") from error
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise PreparationError(
            "isolated daemon_url must be an uncredentialed loopback HTTP endpoint"
        )

    gobby_home = _private_owned_path(
        isolation.get("gobby_home"), name="isolated GOBBY_HOME", directory=True
    )
    if gobby_home == (Path.home() / ".gobby").resolve():
        raise PreparationError("isolated GOBBY_HOME must not be the user's default Gobby home")
    bootstrap = _mapping(isolation.get("bootstrap"), name="bootstrap identity")
    bootstrap_path = _private_owned_path(
        bootstrap.get("path"), name="bootstrap path", directory=False
    )
    if bootstrap_path != gobby_home / "bootstrap.yaml":
        raise PreparationError("bootstrap path must be the contained GOBBY_HOME bootstrap")
    bootstrap_hash = _digest(bootstrap.get("sha256"), name="bootstrap")
    try:
        observed_bootstrap_hash = _sha256_file(bootstrap_path)
    except OSError as error:
        raise PreparationError(f"bootstrap is unavailable: {error}") from error
    if observed_bootstrap_hash != bootstrap_hash:
        raise PreparationError("bootstrap hash changed")
    try:
        bootstrap_settings = _mapping(
            _safe_load_unique(bootstrap_path.read_text(encoding="utf-8")),
            name="bootstrap settings",
        )
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise PreparationError(f"bootstrap settings are unavailable: {error}") from error
    bootstrap_host = _string(bootstrap_settings.get("bind_host"), name="bootstrap bind_host")
    bootstrap_port = bootstrap_settings.get("daemon_port")
    if (
        bootstrap_host != endpoint.hostname
        or not isinstance(bootstrap_port, int)
        or isinstance(bootstrap_port, bool)
        or bootstrap_port != port
    ):
        raise PreparationError("bootstrap daemon endpoint does not match isolated daemon_url")

    database = _mapping(isolation.get("database"), name="database identity")
    database_public = {
        "host": _string(database.get("host"), name="database host"),
        "port": database.get("port"),
        "name": _string(database.get("name"), name="database name"),
        "schema": _string(database.get("schema"), name="database schema"),
    }
    if (
        database_public["host"] not in {"127.0.0.1", "::1"}
        or not isinstance(database_public["port"], int)
        or isinstance(database_public["port"], bool)
        or not 1 <= database_public["port"] <= 65535
        or not _DATABASE_SCHEMA.fullmatch(cast(str, database_public["schema"]))
    ):
        raise PreparationError("database identity must name a private loopback test schema")
    database_receipt, database_body = _receipt_payload(database.get("receipt"), name="database")
    if database_body != database_public:
        raise PreparationError("database receipt does not match its public identity")
    database_url = _string(bootstrap_settings.get("database_url"), name="bootstrap database_url")
    if _bootstrap_database_identity(database_url) != database_public:
        raise PreparationError("bootstrap database identity does not match database receipt")

    service = _mapping(isolation.get("service"), name="service identity")
    service_public = {
        "identity": _string(service.get("identity"), name="service identity"),
        "daemon_url": _normalize_daemon_url(
            _string(service.get("daemon_url"), name="service daemon_url")
        ),
    }
    if service_public["daemon_url"] != daemon_url:
        raise PreparationError("service receipt endpoint does not match isolated daemon_url")
    service_receipt, service_body = _receipt_payload(service.get("receipt"), name="service")
    normalized_service_body = dict(service_body)
    normalized_service_body["daemon_url"] = _normalize_daemon_url(
        _string(service_body.get("daemon_url"), name="service receipt daemon_url")
    )
    if normalized_service_body != service_public:
        raise PreparationError("service receipt does not match its public identity")
    if not re.fullmatch(r"[0-9a-f]{16}", service_public["identity"]):
        raise PreparationError("service identity must be the 16-character deployment token")
    return {
        "mode": "contained",
        "daemon_url": daemon_url,
        "gobby_home": str(gobby_home),
        "bootstrap": {"path": str(bootstrap_path), "sha256": bootstrap_hash},
        "database": {**database_public, "receipt": database_receipt},
        "service": {
            "identity": service_public["identity"],
            "daemon_url": daemon_url,
            "receipt": service_receipt,
        },
    }


def _sealed_environment(isolation: Mapping[str, Any]) -> dict[str, str]:
    return {
        "GOBBY_DAEMON_URL": cast(str, isolation["daemon_url"]),
        "GOBBY_HOME": cast(str, isolation["gobby_home"]),
        "GOBBY_TEST_PROTECT": "1",
    }


def _command_environment(isolation: Mapping[str, Any]) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in _PASSTHROUGH_ENVIRONMENT_KEYS if key in os.environ
    }
    environment.update(_sealed_environment(isolation))
    return environment


def _profile(value: object, *, role: str, identifier: str) -> dict[str, Any]:
    profile = _mapping(value, name=f"{role} profile")
    if profile.get("identifier") != identifier:
        raise PreparationError(f"{role} profile must identify {identifier}")
    effective = _mapping(profile.get("effective"), name=f"{role} effective profile")
    for key in ("provider", "model", "reasoning_effort"):
        _string(effective.get(key), name=f"{role} {key}")
    return {
        "identifier": identifier,
        "definition_id": _string(profile.get("definition_id"), name=f"{role} definition_id"),
        "definition_updated_at": _string(
            profile.get("definition_updated_at"), name=f"{role} definition_updated_at"
        ),
        "effective": effective,
        "content_hash": _digest(profile.get("content_hash"), name=f"{role} content_hash"),
    }


def validate_runtime_identity(value: object) -> dict[str, Any]:
    """Validate and normalize the database-derived pre-execution identity receipt."""
    identity = _mapping(value, name="runtime identity")
    if identity.get("schema_version") != 1:
        raise PreparationError("runtime identity schema_version must be 1")
    gcode = _mapping(identity.get("gcode"), name="gcode identity")
    if gcode.get("contract_version") != 10:
        raise PreparationError("installed gcode contract_version must be 10")
    profiles = _mapping(identity.get("profiles"), name="profile identities")
    if identity.get("tool_identities") != list(EXPECTED_TOOL_IDENTITIES):
        raise PreparationError("runtime Ask tool identities do not match the accepted contract")
    isolation = _runtime_isolation(identity.get("isolation"))
    gates = _mapping(identity.get("execution_gates"), name="execution gates")
    normalized_gates: dict[str, dict[str, str]] = {}
    for gate_name in REQUIRED_EXECUTION_GATES:
        gate = _mapping(gates.get(gate_name), name=f"execution gate {gate_name}")
        if gate.get("status") != "accepted":
            raise PreparationError(f"execution gate {gate_name} must be accepted")
        normalized_gates[gate_name] = {
            "status": "accepted",
            "evidence_sha256": _digest(
                gate.get("evidence_sha256"), name=f"execution gate {gate_name} evidence"
            ),
        }
    return {
        "schema_version": 1,
        "project_id": _string(identity.get("project_id"), name="project_id"),
        "gcode": {
            "version": _string(gcode.get("version"), name="gcode version"),
            "contract_version": 10,
            "executable_sha256": _digest(gcode.get("executable_sha256"), name="gcode executable"),
        },
        "profiles": {
            "investigator": _profile(
                profiles.get("investigator"),
                role="investigator",
                identifier="ask-investigator",
            ),
            "reviewer": _profile(
                profiles.get("reviewer"), role="reviewer", identifier="ask-reviewer"
            ),
        },
        "tool_identities": list(EXPECTED_TOOL_IDENTITIES),
        "isolation": isolation,
        "execution_gates": normalized_gates,
    }


def _verified_execution_environment(manifest: Mapping[str, Any]) -> dict[str, str]:
    runtime = _mapping(manifest.get("runtime_identity"), name="runtime identity")
    isolation = _runtime_isolation(runtime.get("isolation"))
    execution = _mapping(manifest.get("execution"), name="execution contract")
    sealed = _sealed_environment(isolation)
    if execution.get("environment") != sealed:
        raise PreparationError("sealed execution environment changed")
    expected = _digest(execution.get("environment_sha256"), name="sealed execution environment")
    if _sha256_bytes(_canonical_json(sealed)) != expected:
        raise PreparationError("sealed execution environment hash changed")
    return _command_environment(isolation)


def _cached_runtime_grant(
    manifest: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, Any]:
    identity = _mapping(manifest.get("runtime_identity"), name="runtime identity")
    isolation = _mapping(identity.get("isolation"), name="runtime isolation")
    service = _mapping(isolation.get("service"), name="service identity")
    deployment_token = _string(service.get("identity"), name="service deployment identity")
    project_id = _string(identity.get("project_id"), name="project_id")
    home = _private_owned_path(
        environment.get("GOBBY_HOME"), name="isolated GOBBY_HOME", directory=True
    )
    grant_root = _private_owned_path(
        str(home / "grants" / deployment_token),
        name="runtime grant directory",
        directory=True,
    )
    matches: list[dict[str, Any]] = []
    for candidate in sorted(grant_root.glob("*.json")):
        # This probe runs status at the primary root. Overlay grants have the
        # same authorized project but distinct project--overlay cache names.
        if candidate.name != f"{project_id}.json":
            continue
        path = _private_owned_path(str(candidate), name="runtime grant", directory=False)
        try:
            payload = _mapping(json.loads(path.read_bytes()), name="runtime grant cache")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PreparationError(f"runtime grant cache is invalid: {error}") from error
        grant = _mapping(payload.get("grant", payload), name="runtime grant")
        principal = _mapping(grant.get("principal"), name="runtime grant principal")
        deployment = _mapping(grant.get("deployment"), name="runtime grant deployment")
        if (
            principal.get("project_id") == project_id
            and deployment.get("token") == deployment_token
        ):
            matches.append(grant)
    if len(matches) != 1:
        raise PreparationError("exactly one accepted runtime grant must bind the project")
    return matches[0]


def _daemon_json(
    daemon_url: str,
    path: str,
    headers: Mapping[str, str],
    *,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = urllib_request.Request(
        f"{_normalize_daemon_url(daemon_url)}{path}",
        headers={**headers, **({"Content-Type": "application/json"} if body is not None else {})},
        method="POST" if body is not None else "GET",
        data=_canonical_json(body) if body is not None else None,
    )
    opener = urllib_request.build_opener(
        urllib_request.ProxyHandler({}),
        _RejectRedirects(),
    )
    try:
        with opener.open(request, timeout=10) as response:
            return _mapping(json.load(response), name="daemon-backed runtime identity response")
    except urllib_error.HTTPError as error:
        raise PreparationError(
            f"daemon-backed runtime identity probe rejected {path}: HTTP {error.code}"
        ) from error
    except (urllib_error.URLError, TimeoutError, OSError) as error:
        raise PreparationError(
            f"daemon-backed runtime identity probe failed for {path}: {error}"
        ) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PreparationError(
            f"daemon-backed runtime identity probe returned invalid JSON for {path}"
        ) from error


def create_caller_worktree(
    identity: Mapping[str, Any], environment: Mapping[str, str], commit: str, branch: str
) -> dict[str, Any]:
    """Use ordinary daemon worktree creation so checkout registration is durable."""
    token_path = _private_owned_path(
        str(Path(environment["GOBBY_HOME"]) / "local_cli_token"),
        name="contained local CLI token",
        directory=False,
    )
    bearer = token_path.read_text(encoding="utf-8").strip()
    return _daemon_json(
        environment["GOBBY_DAEMON_URL"],
        "/api/source-control/worktrees",
        {"Authorization": f"Bearer {bearer}"},
        body={
            "project_id": identity["project_id"],
            "branch_name": branch,
            "base_branch": commit,
            "workspace_role": "client",
        },
    )


def prepare_caller_worktrees(
    identity: Mapping[str, Any],
    environment: Mapping[str, str],
    commits: dict[str, dict[str, Any]],
    destination: Path,
    binary: Path,
    runner: CommandRunner,
    creator: Callable[[Mapping[str, Any], Mapping[str, str], str, str], dict[str, Any]],
) -> None:
    """Create each pinned caller once and index it before any primary invocation."""
    receipt_root = destination / "worktree-preparation"
    receipt_root.mkdir(parents=True, exist_ok=False)
    for commit, details in commits.items():
        branch = f"ask-cohort-{destination.name}-{commit[:12]}"
        _write_json_new(
            receipt_root / f"{commit}.request.json", {"commit": commit, "branch": branch}
        )
        created = creator(identity, environment, commit, branch)
        _write_json_new(receipt_root / f"{commit}.worktree.json", created)
        root = Path(_string(created.get("worktree_path"), name="created worktree path")).resolve(
            strict=True
        )
        if created.get("project_id") != identity["project_id"]:
            raise PreparationError("created caller worktree belongs to another project")
        head = (
            _require_success(
                runner(("git", "-C", str(root), "rev-parse", "HEAD"), 30, environment),
                operation="caller worktree HEAD verification",
            )
            .decode()
            .strip()
        )
        if head != commit:
            raise PreparationError("caller worktree HEAD differs from frozen source commit")
        argv = (str(binary), "--project", str(root), "index", "--format", "json")
        _write_json_new(receipt_root / f"{commit}.index-invocation.json", {"argv": argv})
        indexed = runner(argv, 600, environment)
        _write_new(receipt_root / f"{commit}.index.stdout", indexed.stdout)
        _write_new(receipt_root / f"{commit}.index.stderr", indexed.stderr)
        _write_json_new(
            receipt_root / f"{commit}.index-result.json",
            {
                "exit_code": indexed.exit_code,
                "interruption": indexed.interruption,
                "wall_seconds": indexed.wall_seconds,
            },
        )
        _require_success(indexed, operation="ordinary caller worktree indexing")
        details["project_root"] = str(root)
        details["worktree_id"] = _string(created.get("id"), name="created worktree id")


def _probe_live_runtime_service(
    manifest: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, Any]:
    """Read daemon-backed deployment, database, project, and checkout identities."""
    identity = _mapping(manifest.get("runtime_identity"), name="runtime identity")
    isolation = _mapping(identity.get("isolation"), name="runtime isolation")
    daemon_url = _string(isolation.get("daemon_url"), name="isolated daemon_url")
    project_id = _string(identity.get("project_id"), name="project_id")
    grant = _cached_runtime_grant(manifest, environment)
    token_path = _private_owned_path(
        str(Path(environment["GOBBY_HOME"]) / "local_cli_token"),
        name="contained local CLI token",
        directory=False,
    )
    try:
        bearer = _string(token_path.read_text(encoding="utf-8").strip(), name="local CLI token")
    except (OSError, UnicodeError) as error:
        raise PreparationError(f"contained local CLI token is unavailable: {error}") from error
    headers = {
        "Authorization": f"Bearer {bearer}",
        "X-Gobby-Caller-Project-Id": project_id,
        "X-Gobby-Project-Id": project_id,
    }
    grant_header = base64.urlsafe_b64encode(_canonical_json(grant)).decode().rstrip("=")
    runtime = _daemon_json(
        daemon_url,
        "/api/runtime/config",
        {**headers, "X-Gobby-Runtime-Grant": grant_header},
    )
    project = _daemon_json(daemon_url, f"/api/projects/{quote(project_id, safe='')}", headers)
    checkout = _mapping(project.get("checkout"), name="live runtime service checkout")
    capabilities = _mapping(grant.get("capabilities"), name="runtime grant capabilities")
    postgres = _mapping(capabilities.get("postgres"), name="runtime grant postgres capability")
    if postgres.get("mode") != "direct":
        raise PreparationError("runtime grant must provide direct PostgreSQL identity")
    database = _database_identity_from_conninfo(
        _string(postgres.get("dsn"), name="runtime grant PostgreSQL DSN"),
        name="runtime grant PostgreSQL DSN",
    )
    deployment = _mapping(grant.get("deployment"), name="runtime grant deployment")
    return {
        "daemon_url": daemon_url,
        "deployment_token": deployment.get("token"),
        "database": database,
        "config_revision": runtime.get("config_revision"),
        "grant_config_revision": grant.get("config_revision"),
        "project_id": project.get("id"),
        "project_root": checkout.get("root_path"),
    }


def _verify_live_runtime_service(
    manifest: Mapping[str, Any],
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    runtime_service_probe: RuntimeServiceProbe,
) -> None:
    """Bind the sealed endpoint/home to the daemon used by native Ask."""
    status_argv = (
        str(manifest["gcode"]["path"]),
        "--project",
        str(manifest["source"]["project_root"]),
        "--format",
        "json",
        "status",
    )
    _require_success(
        command_runner(status_argv, 30, environment),
        operation="contained runtime grant materialization",
    )
    observed = runtime_service_probe(manifest, environment)
    identity = _mapping(manifest.get("runtime_identity"), name="runtime identity")
    isolation = _mapping(identity.get("isolation"), name="runtime isolation")
    service = _mapping(isolation.get("service"), name="service identity")
    expected_database = _mapping(isolation.get("database"), name="database identity")
    expected_database_public = {
        name: expected_database[name] for name in ("host", "port", "name", "schema")
    }
    if observed.get("daemon_url") != isolation.get("daemon_url"):
        raise PreparationError("live runtime service endpoint does not match receipt")
    if observed.get("deployment_token") != service.get("identity"):
        raise PreparationError("live runtime service deployment does not match receipt")
    if observed.get("config_revision") != observed.get("grant_config_revision"):
        raise PreparationError("live runtime service config revision does not match its grant")
    if observed.get("database") != expected_database_public:
        raise PreparationError("live runtime service database identity does not match receipt")
    expected_project = identity["project_id"]
    if observed.get("project_id") != expected_project:
        raise PreparationError("live runtime service project identity does not match receipt")
    observed_root = observed.get("project_root")
    if (
        not isinstance(observed_root, str)
        or Path(observed_root).resolve() != Path(manifest["source"]["project_root"]).resolve()
    ):
        raise PreparationError("live runtime service project root does not match prepared source")
