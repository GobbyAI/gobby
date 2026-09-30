"""Commands for exposing hub-managed datastores to trusted clients."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
import shutil
import subprocess  # nosec B404 - fixed tailscale and Docker commands
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import click
import psycopg
from psycopg import sql
from psycopg_pool import PoolTimeout

from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.bootstrap_io import (
    read_bootstrap_yaml,
    update_bootstrap_yaml,
    write_bootstrap_yaml,
)
from gobby.config.postgres_bootstrap import (
    read_pending_credential_rotation,
    write_postgres_defaults,
)
from gobby.storage.hub.async_ops import (
    CommittedCleanupError,
    IndeterminateCommitError,
    run_bounded_db,
)

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
    }
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
    parts = urlsplit(database_url)
    if parts.scheme not in {"postgres", "postgresql"}:
        raise click.ClickException("phase=validate database_url must use postgresql://")
    aliases = sorted(
        key
        for key, _value in (pair.partition("=")[::2] for pair in parts.query.split("&"))
        if key and key.lower() in _INDIRECTING_QUERY_KEYS
    )
    if aliases:
        raise click.ClickException(
            "phase=validate database_url uses indirection "
            f"({', '.join(aliases)}); address the role, database and endpoint directly"
        )
    userinfo = parts.netloc.rpartition("@")[0]
    role = userinfo.partition(":")[0]
    database = parts.path.removeprefix("/")
    if not role or not database:
        raise click.ClickException(
            "phase=validate database_url must name the role and database directly"
        )
    if not parts.hostname or parts.port is None:
        raise click.ClickException(
            "phase=validate database_url must name the endpoint host and port directly"
        )
    return _RotationTarget(
        role=unquote(role), database=unquote(database), host=parts.hostname, port=parts.port
    )


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


_SCRAM_VERIFIER_PATTERN = re.compile(r"SCRAM-SHA-256\$[^\s'\"]+")


def _redact_secrets(text: str, redaction_secrets: tuple[str, ...]) -> str:
    """Remove every known secret and any SCRAM verifier from operator-facing text."""
    for secret in redaction_secrets:
        if secret:
            text = text.replace(secret, "****")
    return _SCRAM_VERIFIER_PATTERN.sub("****", text)


def _rotation_failure_detail(exc: BaseException) -> str:
    """Report only the exception class: phase codes, classes and guidance, never text."""
    return type(exc).__name__


def _observe_hub_alter(
    database_url: str,
    role: str,
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
        # ALTER ROLE is a utility statement, so the verifier is a composed literal,
        # never a bound parameter. Generate SCRAM explicitly rather than relying on
        # the server's default password_encryption.
        verifier = connection.pgconn.encrypt_password(
            intended_password.encode(), role.encode(), b"scram-sha-256"
        )
        statement = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(verifier.decode("ascii"))
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
        except CommittedCleanupError:
            # COMMIT was observed; only termination/reap failed, so the ALTER is durable
            # and final publication must still proceed.
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
    gobby_home: Path,
    bootstrap_file: Path,
    role: str,
    new_url: str,
    redaction_secrets: tuple[str, ...],
) -> None:
    """Publish the new DSN and drop the pending pair as one compare-and-set."""
    try:
        write_postgres_defaults(
            gobby_home=gobby_home,
            database_url=new_url,
            clear_credential_rotation=True,
        )
    except (BootstrapConfigError, OSError) as exc:
        click.echo(
            f"phase=finalize PostgreSQL role {role!r} now uses the new password but "
            f"{bootstrap_file} was not updated: "
            f"{_rotation_failure_detail(exc)}",
            err=True,
        )
        click.echo(
            f"Set database_url in {bootstrap_file} to: "
            f"{_redact_secrets(new_url, redaction_secrets)}",
            err=True,
        )
        click.echo(
            "Resume with `gobby datastores rotate-password postgres`; the pending "
            "credential pair is still on disk.",
            err=True,
        )
        raise click.ClickException("bootstrap.yaml update failed after the role changed") from exc


def _rotate_postgres_password(gobby_home: Path, bootstrap: dict[str, Any]) -> None:
    bootstrap_file = gobby_home / "bootstrap.yaml"
    current_url = bootstrap.get("database_url")
    if not isinstance(current_url, str) or not current_url:
        raise click.ClickException(f"{bootstrap_file} has no database_url; run `gobby install`")
    target = _parse_rotation_target(current_url)

    pending = read_pending_credential_rotation(gobby_home)
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
        redaction_secrets = (intended_password, pending.previous_password)
    else:
        new_password = secrets.token_urlsafe(32)
        role = target.role
        previous_password = unquote(urlsplit(current_url).password or "")
        intended_password = new_password
        redaction_secrets = (new_password, previous_password)

        def _stage(data: dict[str, Any]) -> None:
            data["credential_rotation"] = {
                "role": role,
                "pending_password": intended_password,
                "previous_password": previous_password,
            }

        # Durable pending publication precedes ALTER: a crash between here and the
        # COMMIT leaves recovery state on disk rather than a role nobody can open.
        try:
            update_bootstrap_yaml(bootstrap_file, _stage)
        except (BootstrapConfigError, OSError) as exc:
            raise click.ClickException(
                f"phase=prepare could not publish the pending credential pair: "
                f"{_rotation_failure_detail(exc)}"
            ) from exc

    new_url = _dsn_with_password(current_url, intended_password)[1]
    # A resumed rotation may already have committed the intended password while the
    # bootstrap DSN still carries the previous one (post-COMMIT ambiguity). Only then
    # is the pending DSN a valid fallback for the ALTER.
    resume_conninfo = new_url if pending is not None else None
    try:
        _observe_hub_alter(current_url, role, intended_password, resume_conninfo=resume_conninfo)
    except IndeterminateCommitError as exc:
        raise click.ClickException(
            f"phase=alter COMMIT outcome unobserved; the pending pair is preserved. "
            f"Re-run `gobby datastores rotate-password postgres` to resume. "
            f"({_rotation_failure_detail(exc)})"
        ) from exc
    except Exception as exc:
        raise click.ClickException(
            f"PostgreSQL password rotation failed: {_rotation_failure_detail(exc)}"
        ) from exc

    # The role has already changed: a failed write below must hand the operator
    # the new DSN for manual repair before the command exits.
    _finalize_postgres_rotation(
        gobby_home=gobby_home,
        bootstrap_file=bootstrap_file,
        role=role,
        new_url=new_url,
        redaction_secrets=redaction_secrets,
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
        _rotate_postgres_password(gobby_home, bootstrap)
    else:
        _rotate_falkordb_password(gobby_home)
    click.echo(f"Run `gobby restart` to apply the new {service} password.")
