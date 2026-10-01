"""Commands for exposing hub-managed datastores to trusted clients."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import secrets
import shutil
import subprocess  # nosec B404 - fixed tailscale and Docker commands
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import click
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import PoolTimeout

from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.bootstrap_io import (
    publish_bootstrap_yaml_locked,
    read_bootstrap_yaml,
    write_bootstrap_yaml,
)
from gobby.config.postgres_bootstrap import (
    read_pending_credential_rotation,
)
from gobby.storage.hub.async_ops import (
    CommittedCleanupError,
    IndeterminateCommitError,
    run_bounded_db,
)
from gobby.utils.durable_file import exclusive_file_lock

from .installers.falkor import rotate_falkordb_password
from .installers.managed_services_lock import ManagedServicesLockError, managed_services_lock
from .utils import get_gobby_home

_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def apply_hub_schema_contract(gobby_home: Path) -> None:
    """Bring the hub to the current schema contract before reading it.

    Lives in this CLI composition root because acquiring a PostgreSQL pool is
    reserved to composition roots (tests/storage/hub/test_pool_ownership_boundaries.py).
    """
    from gobby.storage.hub.runtime import runtime_hub_database

    with runtime_hub_database(str(gobby_home / "bootstrap.yaml"), apply_migrations=True):
        pass


# Bounded end-to-end budget for one hub ALTER: connect, execute, COMMIT, reap.
_HUB_ALTER_DEADLINE_SECONDS = 10.0
_ROTATABLE_SERVICES = ("postgres", "falkordb")

# Query keys that redirect identity or endpoint away from the parsed userinfo/netloc.
_INDIRECTING_QUERY_KEYS = frozenset(
    {
        "user",
        "dbname",
        "password",
        "host",
        "hostaddr",
        "port",
        "service",
        "servicefile",
        "passfile",
        "options",
    }
)
_INDIRECTING_ENVIRONMENT_KEYS = (
    "PGHOSTADDR",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGPASSFILE",
    "PGOPTIONS",
    "PGSYSCONFDIR",
)


@dataclass(frozen=True, slots=True)
class _RotationTarget:
    """The validated, directly-addressed role/database/endpoint of one rotation."""

    role: str
    database: str
    host: str
    port: int


def _parse_rotation_target(database_url: str) -> _RotationTarget:
    """Reject aliases/indirection and return the effective role, database and endpoint."""
    if any(os.environ.get(key) for key in _INDIRECTING_ENVIRONMENT_KEYS):
        raise click.ClickException(
            "phase=validate unset libpq environment indirection before rotating credentials"
        )
    if "#" in database_url or any(ord(char) < 32 for char in database_url):
        raise click.ClickException("phase=validate database_url contains ambiguous URI characters")
    try:
        parts = urlsplit(database_url)
        parameters = conninfo_to_dict(database_url)
        uri_identity = (
            unquote(parts.username or ""),
            unquote(parts.path.removeprefix("/")),
            unquote(parts.hostname or "").lower(),
            parts.port,
        )
    except (ValueError, psycopg.ProgrammingError):
        # libpq parser errors can contain secret URI material.
        raise click.ClickException("phase=validate invalid PostgreSQL connection URI") from None
    if parts.scheme not in {"postgres", "postgresql"}:
        raise click.ClickException("phase=validate database_url must use postgresql://")
    aliases = sorted(
        key.lower()
        for key, _value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() in _INDIRECTING_QUERY_KEYS
    )
    if aliases:
        raise click.ClickException(
            "phase=validate database_url uses indirection "
            f"({', '.join(aliases)}); address the role, database and endpoint directly"
        )
    role = parameters.get("user", "")
    database = parameters.get("dbname", "")
    password = parameters.get("password", "")
    if (
        not isinstance(role, str)
        or not role
        or not isinstance(database, str)
        or not database
        or not isinstance(password, str)
        or not password
    ):
        raise click.ClickException(
            "phase=validate database_url must name the role, password and database directly"
        )
    host = parameters.get("host", "")
    port = parameters.get("port", "")
    if (
        not isinstance(host, str)
        or not host
        or host.startswith(("/", "@"))
        or "," in host
        or not isinstance(port, str)
        or not port.isascii()
        or not port.isdecimal()
        or not 0 < int(port) <= 65535
    ):
        raise click.ClickException(
            "phase=validate database_url must name one endpoint host and port directly"
        )
    if (role, database, host.lower(), int(port)) != uri_identity or password != unquote(
        parts.password or ""
    ):
        raise click.ClickException(
            "phase=validate database_url is ambiguous between URI and libpq parsing"
        )
    return _RotationTarget(role=role, database=database, host=host, port=int(port))


class DatastoreExposureError(RuntimeError):
    """Raised when datastore exposure cannot be applied safely."""


@dataclass(frozen=True)
class DatastoreExposureResult:
    bind_address: str
    published_host: str


def validate_bind_address(
    value: str,
    *,
    tailscale_ipv4: set[str] | None = None,
) -> str:
    """Accept loopback IPv4 or a concrete IPv4 assigned by Tailscale locally."""
    candidate = value.strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise DatastoreExposureError("--bind must be an IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise DatastoreExposureError("--bind accepts IPv4 only")
    if address.is_unspecified:
        raise DatastoreExposureError("--bind cannot use wildcard address 0.0.0.0")
    if address.is_loopback:
        return str(address)
    local_tailscale = tailscale_ipv4 if tailscale_ipv4 is not None else _tailscale_ipv4_addresses()
    if str(address) not in local_tailscale:
        raise DatastoreExposureError(
            "--bind must be loopback or a concrete IPv4 address assigned to local Tailscale"
        )
    return str(address)


def validate_published_host(value: str) -> str:
    """Validate a DNS dial host and reject wildcard or IP-literal values."""
    candidate = value.strip().rstrip(".")
    if not candidate or "*" in candidate:
        raise DatastoreExposureError("--host must be a concrete DNS name")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise DatastoreExposureError("--host must be a DNS name, not an IP address")
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DatastoreExposureError("--host is not a valid DNS name") from exc
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise DatastoreExposureError("--host is not a valid DNS name")
    return ascii_host.lower()


def expose_datastores(
    gobby_home: Path,
    *,
    bind_address: str,
    published_host: str,
) -> DatastoreExposureResult:
    """Stage a bind change, verify services, then atomically publish dial endpoints."""
    bind = validate_bind_address(bind_address)
    host = validate_published_host(published_host)
    bootstrap_path = gobby_home / "bootstrap.yaml"

    try:
        with managed_services_lock(gobby_home, operation="datastores expose"):
            previous = read_bootstrap_yaml(bootstrap_path)
            if previous.get("datastore_mode", "local") != "local":
                raise DatastoreExposureError("datastores expose runs only on the local hub")
            was_running = _snapshot_compose_running(gobby_home)
            candidate = dict(previous)
            candidate["services_bind_address"] = bind
            candidate["hub"] = True
            write_bootstrap_yaml(bootstrap_path, candidate)

            ready, detail = _start_managed_services(gobby_home)
            if not ready:
                rollback = _restore_compose_state(gobby_home, previous, was_running)
                raise DatastoreExposureError(f"Exposure staging failed: {detail}; {rollback}")

            try:
                _commit_shared_endpoints(gobby_home, host)
            except Exception as exc:
                rollback = _restore_compose_state(gobby_home, previous, was_running)
                raise DatastoreExposureError(
                    f"Endpoint publication failed: {exc}; {rollback}"
                ) from exc
    except ManagedServicesLockError as exc:
        raise DatastoreExposureError(str(exc)) from exc

    return DatastoreExposureResult(bind_address=bind, published_host=host)


def _tailscale_ipv4_addresses() -> set[str]:
    executable = shutil.which("tailscale")
    if executable is None:
        return set()
    try:
        result = subprocess.run(  # nosec B603
            [executable, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    return {
        str(address)
        for line in result.stdout.splitlines()
        if (address := _parse_ipv4(line.strip())) is not None
    }


def _parse_ipv4(value: str) -> ipaddress.IPv4Address | None:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, ipaddress.IPv4Address) else None


def _snapshot_compose_running(gobby_home: Path) -> bool:
    compose_file = gobby_home / "services" / "docker-compose.yml"
    if not compose_file.is_file() or shutil.which("docker") is None:
        return False
    from .installers.compose_env import ComposeEnvironmentError, resolve_compose_runtime
    from .installers.docker_guard import ensure_docker_allowed

    try:
        runtime = resolve_compose_runtime(gobby_home, profiles=("postgres",))
        ensure_docker_allowed("datastores compose ps snapshot", runner=subprocess.run)
        result = subprocess.run(  # nosec B603 B607
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "ps",
                "--status",
                "running",
                "--services",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=runtime.environment,
            cwd=str(compose_file.parent),
            check=False,
        )
    except (ComposeEnvironmentError, OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _start_managed_services(gobby_home: Path) -> tuple[bool, str]:
    from .daemon import _services_start

    result = _services_start(gobby_home)
    return result.outcome == "success", result.detail


def _stop_managed_services(gobby_home: Path) -> bool:
    from .daemon import _services_stop

    return _services_stop(gobby_home)


def _restore_compose_state(
    gobby_home: Path,
    previous_bootstrap: dict[str, object],
    was_running: bool,
) -> str:
    write_bootstrap_yaml(gobby_home / "bootstrap.yaml", previous_bootstrap)
    if was_running:
        restored, detail = _start_managed_services(gobby_home)
        if not restored:
            return f"rollback failed to restart prior compose state: {detail}"
        return "prior bind and running compose state restored"
    if not _stop_managed_services(gobby_home):
        return "prior bind restored; compose was already stopped"
    return "prior bind and stopped compose state restored"


def _commit_shared_endpoints(gobby_home: Path, published_host: str) -> None:
    from gobby.cli.config_writes import apply_cas_config_patch
    from gobby.storage.config_mutations import ConfigPatch
    from gobby.storage.config_repository import ConfigReadSnapshot
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.runtime import runtime_hub_database

    def build_patch(snapshot: ConfigReadSnapshot) -> ConfigPatch:
        qdrant_port = _config_port(snapshot.values.get("databases.qdrant.port"), 6333)
        return ConfigPatch(
            values={
                "databases.published_host": published_host,
                "databases.qdrant.url": f"http://{published_host}:{qdrant_port}",
                "databases.falkordb.host": published_host,
            }
        )

    with runtime_hub_database(
        str(gobby_home / "bootstrap.yaml"),
        apply_migrations=False,
    ) as database:
        store = ConfigStore(database)
        apply_cas_config_patch(
            read_snapshot=store.read_snapshot,
            build_patch=build_patch,
            patch=store.patch,
        )


def _config_port(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise DatastoreExposureError("stored datastore port is invalid")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise DatastoreExposureError("stored datastore port is invalid") from exc
    if not 1 <= port <= 65535:
        raise DatastoreExposureError("stored datastore port is invalid")
    return port


@click.group()
def datastores() -> None:
    """Manage hub-side shared datastores."""


@datastores.command("expose")
@click.option("bind_address", "--bind", required=True, metavar="IPV4")
@click.option("published_host", "--host", required=True, metavar="DNS_NAME")
def expose(bind_address: str, published_host: str) -> None:
    """Expose managed datastores on a local Tailscale IPv4 address."""
    try:
        result = expose_datastores(
            get_gobby_home(),
            bind_address=bind_address,
            published_host=published_host,
        )
    except DatastoreExposureError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Datastores exposed on {result.bind_address}; clients use {result.published_host}")


def _dsn_with_password(database_url: str, password: str) -> tuple[str, str]:
    """Return ``(role, dsn)``: the DSN user and the DSN with only its password replaced."""
    parts = urlsplit(database_url)
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user = userinfo.partition(":")[0]
    if not user:
        raise click.ClickException("database_url names no user; cannot rotate its password")
    netloc = f"{user}:{quote(password, safe='')}@{hostport}"
    return unquote(user), urlunsplit(parts._replace(netloc=netloc))


def _rotation_failure_detail(exc: BaseException) -> str:
    """Report only the exception class: phase codes, classes and guidance, never text."""
    return type(exc).__name__


def _observe_hub_alter(
    database_url: str,
    target: _RotationTarget,
    intended_password: str,
    *,
    resume_conninfo: str | None = None,
) -> None:
    """Run the transactional ALTER on the bounded helper and observe its COMMIT.

    ``resume_conninfo`` is the pending intended DSN. On the resumed pending-pair
    path the current credential may already have been replaced by a COMMIT whose
    outcome was never observed, so a connection failure falls back to that exact
    intended DSN at most once. The failure proves neither physical outcome, so a
    failed or ambiguous fallback preserves the pending pair. A fresh rotation has
    no fallback: it connects once and propagates any failure unchanged.
    """

    async def _work(connection: Any, _remaining: float) -> None:
        info = connection.info
        if (info.user, info.dbname, info.host, info.port) != (
            target.role,
            target.database,
            target.host,
            target.port,
        ):
            raise click.ClickException(
                "phase=validate connected PostgreSQL target differs from the validated URI; "
                "the pending pair is preserved. Re-run "
                "`gobby datastores rotate-password postgres` after resolving the target"
            )
        # ALTER ROLE is a utility statement, so the verifier is a composed literal,
        # never a bound parameter. Generate SCRAM explicitly rather than relying on
        # the server's default password_encryption.
        verifier = connection.pgconn.encrypt_password(
            intended_password.encode(), target.role.encode(), b"scram-sha-256"
        )
        statement = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(target.role), sql.Literal(verifier.decode("ascii"))
        )
        await connection.execute(statement)

    resume = resume_conninfo is not None
    conninfos = (database_url,) if resume_conninfo is None else (database_url, resume_conninfo)
    for index, conninfo in enumerate(conninfos):
        try:
            asyncio.run(
                run_bounded_db(
                    _work,
                    conninfo=conninfo,
                    deadline_seconds=_HUB_ALTER_DEADLINE_SECONDS,
                    statement_timeout_remaining=False,
                    lock_timeout=False,
                )
            )
            return
        except psycopg.OperationalError:
            # A psycopg connect refusal is the base OperationalError, not its SQLSTATE
            # subclass, so it cannot be told from a generic connection failure. Only on
            # resume does that ambiguity matter: try the intended DSN once. The error
            # proves neither outcome, so do not infer a committed ALTER from it, and a
            # fresh rotation (``resume`` False) propagates unchanged with no retry.
            if not resume or index + 1 == len(conninfos):
                raise


def _finalize_postgres_rotation(
    *,
    bootstrap_file: Path,
    new_url: str,
    prepared: dict[str, Any],
) -> None:
    """Publish the new DSN and drop the pending pair as one compare-and-set."""
    try:
        completed = dict(prepared)
        completed["database_url"] = new_url
        completed.pop("credential_rotation", None)
        publish_bootstrap_yaml_locked(bootstrap_file, completed, expected_credentials=prepared)
    except (BootstrapConfigError, OSError) as exc:
        # A rename may have taken effect before fsync/readback failed. A reread
        # selects the state to repair; only another durable publication proves it.
        try:
            visible = read_bootstrap_yaml(bootstrap_file)
            actual_credentials = (visible.get("database_url"), visible.get("credential_rotation"))
            if actual_credentials not in (
                (prepared.get("database_url"), prepared.get("credential_rotation")),
                (new_url, None),
            ):
                raise BootstrapConfigError("credential state changed during publication")
            restored = dict(visible)
            restored["database_url"] = prepared["database_url"]
            restored["credential_rotation"] = prepared["credential_rotation"]
            publish_bootstrap_yaml_locked(bootstrap_file, restored, expected_credentials=visible)
        except (BootstrapConfigError, OSError) as restore_error:
            raise click.ClickException(
                "phase=finalize indeterminate-publication; durable pending restoration "
                "could not be proved. Re-run `gobby datastores rotate-password postgres` "
                "to resume if pending is present; otherwise reconcile the canonical "
                "bootstrap before retrying. "
                f"(publication={_rotation_failure_detail(exc)}, "
                f"restoration={_rotation_failure_detail(restore_error)})"
            ) from None
        raise click.ClickException(
            "phase=finalize pending-restored; COMMIT was observed and the pending pair "
            "was durably restored. Re-run `gobby datastores rotate-password postgres` "
            f"to resume. ({_rotation_failure_detail(exc)})"
        ) from None


def _rotate_postgres_password(gobby_home: Path) -> None:
    # The canonical owner is read only after
    # the same services→sidecar lock order used by managed service transitions.
    with managed_services_lock(gobby_home, operation="rotate PostgreSQL credentials"):
        with exclusive_file_lock(gobby_home / "bootstrap.yaml"):
            _rotate_postgres_password_locked(gobby_home)


def _rotate_postgres_password_locked(gobby_home: Path) -> None:
    bootstrap_file = gobby_home / "bootstrap.yaml"
    bootstrap = read_bootstrap_yaml(bootstrap_file)
    if bootstrap.get("datastore_mode", "local") != "local":
        raise click.ClickException("phase=validate credential rotation requires local mode")
    current_url = bootstrap.get("database_url")
    if not isinstance(current_url, str) or not current_url:
        raise click.ClickException(f"{bootstrap_file} has no database_url; run `gobby install`")
    target = _parse_rotation_target(current_url)

    pending = read_pending_credential_rotation(gobby_home)
    prepared = dict(bootstrap)
    if pending is not None:
        # Resume: reapply the SAME intended password so a crash before COMMIT cannot
        # strand the role on a password nobody recorded.
        if pending.role != target.role:
            raise click.ClickException(
                "phase=validate bootstrap credential_rotation.role no longer matches "
                "database_url; resolve the pending pair before rotating"
            )
        role = target.role
        intended_password = pending.pending_password
    else:
        new_password = secrets.token_urlsafe(32)
        role = target.role
        previous_password = unquote(urlsplit(current_url).password or "")
        intended_password = new_password

        prepared["credential_rotation"] = {
            "role": role,
            "pending_password": intended_password,
            "previous_password": previous_password,
        }

    # Resume also republishes: visible pending after a failed rename/fsync is not
    # durability evidence. ALTER starts only after this durable boundary succeeds.
    try:
        publish_bootstrap_yaml_locked(bootstrap_file, prepared, expected_credentials=bootstrap)
    except (BootstrapConfigError, OSError) as exc:
        raise click.ClickException(
            f"phase=prepare could not publish the pending credential pair: "
            f"{_rotation_failure_detail(exc)}. Re-run "
            "`gobby datastores rotate-password postgres` to resume if pending is "
            "present; otherwise repair bootstrap storage before retrying."
        ) from None

    prepared = read_bootstrap_yaml(bootstrap_file)

    new_url = _dsn_with_password(current_url, intended_password)[1]
    # A resumed rotation may already have committed the intended password while the
    # bootstrap DSN still carries the previous one (post-COMMIT ambiguity). Only then
    # is the pending DSN a valid fallback for the ALTER.
    resume_conninfo = new_url if pending is not None else None
    try:
        _observe_hub_alter(current_url, target, intended_password, resume_conninfo=resume_conninfo)
    except click.ClickException:
        raise
    except CommittedCleanupError as exc:
        raise click.ClickException(
            "phase=cleanup COMMIT was observed, but cleanup failed; the pending pair "
            "is preserved. Re-run `gobby datastores rotate-password postgres` to resume. "
            f"({_rotation_failure_detail(exc)})"
        ) from exc
    except IndeterminateCommitError as exc:
        raise click.ClickException(
            f"phase=alter COMMIT outcome unobserved; the pending pair is preserved. "
            f"Re-run `gobby datastores rotate-password postgres` to resume. "
            f"({_rotation_failure_detail(exc)})"
        ) from exc
    except Exception as exc:
        raise click.ClickException(
            f"phase=alter PostgreSQL password rotation failed: {_rotation_failure_detail(exc)}. "
            "Re-run `gobby datastores rotate-password postgres` to resume."
        ) from None

    # Successful durable publication removes pending. A publication fault must
    # restore it durably or report that the publication state is indeterminate.
    _finalize_postgres_rotation(
        bootstrap_file=bootstrap_file,
        new_url=new_url,
        prepared=prepared,
    )


def _rotate_falkordb_password(gobby_home: Path) -> None:
    try:
        rotate_falkordb_password(gobby_home=gobby_home)
    except (BootstrapConfigError, RuntimeError, psycopg.OperationalError, PoolTimeout) as exc:
        raise click.ClickException(f"FalkorDB password rotation failed: {exc}") from exc


@datastores.command("rotate-password")
@click.argument("service", type=click.Choice(_ROTATABLE_SERVICES))
def rotate_password(service: str) -> None:
    """Rotate a managed datastore password; never restarts or touches Docker."""
    gobby_home = get_gobby_home()
    bootstrap_file = gobby_home / "bootstrap.yaml"
    if not bootstrap_file.exists():
        raise click.ClickException(f"{bootstrap_file} is missing; run `gobby install`")
    bootstrap = read_bootstrap_yaml(bootstrap_file)
    if bootstrap.get("datastore_mode", "local") != "local":
        raise click.UsageError(
            "rotate-password needs datastore_mode: local; "
            "remote clients hold no datastore credentials."
        )
    if service == "postgres":
        _rotate_postgres_password(gobby_home)
    else:
        _rotate_falkordb_password(gobby_home)
    click.echo(f"Run `gobby restart` to apply the new {service} password.")
