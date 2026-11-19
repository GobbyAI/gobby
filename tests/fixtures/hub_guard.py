"""DATABASE_URL guard for test code that launches worktree daemons or writes the test hub.

Every verdict and rule name here equals tests/fixtures/hub_guard_vectors.json, which the Rust
guard reads too. The rules, first failing rule refuses:

- R2: PGHOSTADDR, PGSERVICE or PGSERVICEFILE is set; libpq would redirect the connection.
- R3: the URL, parsed by psycopg's libpq conninfo parser, names exactly one loopback host
  and one port, carries no hostaddr, service or options, and has the test hub's user and
  database (gobby_test on port 60892). Identity query keys are refused even when equal.
- R4: rehearsal mode only, when a profile path is given (or GOBBY_HUB_REHEARSAL_PROFILE is
  set): the owner-private profile names the same user and database on its own port.
- ARG: the URL, or a per-run name, is missing or malformed.

R1 (GOBBY_TEST_PROTECT) is retired: no rule requires it.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from gobby.utils.dev import WORKTREE_DAEMON_OVERRIDE_ENV
from tests.fixtures.shared_hub import shared_hub_lock_waits

PROFILE_ENV = "GOBBY_HUB_REHEARSAL_PROFILE"
REDIRECT_ENV = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
TEST_HUB_PORT = 60892
TEST_HUB_USER = "gobby_test"
TEST_HUB_DBNAME = "gobby_test"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REHEARSAL_HOSTS = frozenset({"127.0.0.1", "::1"})
FORBIDDEN_QUERY_KEYS = frozenset(
    {"dbname", "user", "host", "port", "hostaddr", "service", "servicefile", "options"}
)
# Copy of gobby.cli.hub_backup.rehearsal._SHARED_PORTS; a test asserts equality with it.
SHARED_PORTS = frozenset(
    {5432, 6333, 6334, 6379, 16379, *range(60887, 60893), 60987, 60988, *range(60990, 61000)}
)
HARNESS_DATABASE_PATTERN = re.compile(r"gobby_harness_([1-9][0-9]{0,6})_[0-9a-f]{12}")
FIXTURE_DATABASE_PATTERN = re.compile(r"gobby_test_([1-9][0-9]{0,6})_[0-9a-f]{12}")
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{12}")
_SCHEMA_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_MAX_PID = 9_999_999
_MAX_PROFILE_BYTES = 65536


class HubGuardError(Exception):
    """A refused DATABASE_URL. `rule` is the first failing rule: R2, R3, R4 or ARG."""

    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


class GuardMode(StrEnum):
    TEST_HUB = "test_hub"
    REHEARSAL = "rehearsal"


@dataclass(frozen=True)
class HubGuard:
    """An accepted base URL. `system_identifier` is the rehearsal profile's cluster id."""

    base: str
    mode: GuardMode
    system_identifier: str | None = None


def operator_home() -> Path:
    """The operator's ~/.gobby, resolved through the account database as rehearsal.py does."""
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".gobby"


def _check_redirects(env: Mapping[str, str]) -> None:
    for name in REDIRECT_ENV:
        if env.get(name):
            raise HubGuardError("R2", f"{name} is set and would redirect libpq off the URL's host")


def _parse(url: str, *, allow_options: bool = False) -> tuple[dict[str, str], list[str]]:
    """Parse with libpq's conninfo parser and return the fields and the decoded query keys."""
    if not url.startswith(("postgresql://", "postgres://")):
        raise HubGuardError("R3", "DATABASE_URL must start with postgresql:// or postgres://")
    try:
        fields = {
            key: str(value) for key, value in conninfo_to_dict(url).items() if value is not None
        }
        query = urlsplit(url).query
    except (psycopg.Error, ValueError) as exc:
        raise HubGuardError("R3", "DATABASE_URL does not parse as libpq conninfo") from exc
    keys = [key for key, _value in parse_qsl(query, keep_blank_values=True)]
    for key in keys:
        lowered = key.lower()
        if lowered in FORBIDDEN_QUERY_KEYS and not (allow_options and lowered == "options"):
            raise HubGuardError("R3", f"query key {key!r} can redirect the connection")
    return fields, keys


def _check_endpoint(fields: Mapping[str, str]) -> None:
    if "hostaddr" in fields or "service" in fields:
        raise HubGuardError("R3", "hostaddr and service are refused in DATABASE_URL")
    host = fields.get("host", "")
    if not host or "," in host or host.lower() not in LOOPBACK_HOSTS:
        raise HubGuardError("R3", "DATABASE_URL must name exactly one loopback host")
    port = fields.get("port", "")
    if not port or "," in port:
        raise HubGuardError("R3", "DATABASE_URL must name exactly one port")


def _check_test_hub_identity(fields: Mapping[str, str], *, dbname: str = TEST_HUB_DBNAME) -> None:
    if fields.get("port") != str(TEST_HUB_PORT):
        raise HubGuardError("R3", f"DATABASE_URL must use port {TEST_HUB_PORT}")
    if fields.get("user") != TEST_HUB_USER:
        raise HubGuardError("R3", f"DATABASE_URL must use user {TEST_HUB_USER}")
    if fields.get("dbname") != dbname:
        raise HubGuardError("R3", f"DATABASE_URL must name database {dbname}")


def _read_profile(profile_path: str) -> dict[str, Any]:
    path = Path(profile_path)
    if not path.is_absolute():
        raise HubGuardError("R4", "the rehearsal profile path must be absolute")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise HubGuardError(
            "R4", "the rehearsal profile is missing, a symlink or unreadable"
        ) from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
            ):
                raise HubGuardError("R4", "the rehearsal profile must be a regular 0600 file")
            raw = handle.read(_MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        raise HubGuardError("R4", "the rehearsal profile cannot be read") from exc
    if len(raw) > _MAX_PROFILE_BYTES:
        raise HubGuardError("R4", "the rehearsal profile is too large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HubGuardError("R4", "the rehearsal profile is not JSON") from exc
    if not isinstance(data, dict):
        raise HubGuardError("R4", "the rehearsal profile must be a JSON object")
    return data


def _check_rehearsal(url: str, fields: Mapping[str, str], profile_path: str) -> HubGuard:
    profile = _read_profile(profile_path)
    home = profile.get("gobby_home")
    host = profile.get("postgres_host")
    identifier = profile.get("postgres_system_identifier")
    services = profile.get("services")
    postgres = services.get("postgres") if isinstance(services, dict) else None
    port = postgres.get("port") if isinstance(postgres, dict) else None
    if not isinstance(home, str) or not home:
        raise HubGuardError("R4", "the rehearsal profile names no gobby_home")
    if Path(home).resolve() == operator_home().resolve():
        raise HubGuardError("R4", "the rehearsal profile's gobby_home is the operator's home")
    if not isinstance(host, str) or host not in REHEARSAL_HOSTS or fields.get("host") != host:
        raise HubGuardError("R4", "the rehearsal host must equal the URL host exactly")
    if not isinstance(port, int) or isinstance(port, bool):
        raise HubGuardError("R4", "the rehearsal profile's postgres port must be an integer")
    if not 1024 <= port <= 65535 or port in SHARED_PORTS:
        raise HubGuardError("R4", "the rehearsal port is a shared or privileged port")
    if fields.get("port") != str(port):
        raise HubGuardError("R4", "the URL port must equal the rehearsal profile's port")
    if fields.get("user") != TEST_HUB_USER or fields.get("dbname") != TEST_HUB_DBNAME:
        raise HubGuardError("R4", "the rehearsal URL must use gobby_test for user and database")
    if not isinstance(identifier, str) or not identifier:
        raise HubGuardError("R4", "the rehearsal profile names no postgres_system_identifier")
    return HubGuard(base=url, mode=GuardMode.REHEARSAL, system_identifier=identifier)


def check_test_hub(
    url: str | None, env: Mapping[str, str], *, profile_path: str | None = None
) -> HubGuard:
    """Accept the base DATABASE_URL for a worktree daemon, or raise HubGuardError.

    Connection-free: it parses and reads the profile file, and never dials the hub.
    """
    if not url:
        raise HubGuardError("ARG", "DATABASE_URL is unset or empty")
    _check_redirects(env)
    fields, _keys = _parse(url)
    _check_endpoint(fields)
    if profile_path is None:
        profile_path = env.get(PROFILE_ENV) or None
    if profile_path is not None:
        return _check_rehearsal(url, fields, profile_path)
    _check_test_hub_identity(fields)
    return HubGuard(base=url, mode=GuardMode.TEST_HUB)


def guard_worktree_override(
    env: MutableMapping[str, str], url: str | None, profile_path: str | None
) -> bool:
    """Set the worktree-daemon override in env only when the guard accepts; never raises."""
    try:
        check_test_hub(url, env, profile_path=profile_path)
    except HubGuardError:
        return False
    env[WORKTREE_DAEMON_OVERRIDE_ENV] = "1"
    return True


def hub_database_url(env: Mapping[str, str] | None = None) -> str:
    """Return DATABASE_URL for a test that needs a database but launches no worktree daemon.

    Accepts a loopback gobby_test hub on any port and refuses the live hub by user and
    database. Launching a worktree daemon always goes through check_test_hub instead.
    """
    source = os.environ if env is None else env
    url = source.get("DATABASE_URL")
    if not url:
        raise HubGuardError("ARG", "DATABASE_URL must name the isolated test hub")
    _check_redirects(source)
    fields, _keys = _parse(url)
    _check_endpoint(fields)
    if fields.get("user") != TEST_HUB_USER or fields.get("dbname") != TEST_HUB_DBNAME:
        raise HubGuardError("R3", "DATABASE_URL must use the gobby_test user and database")
    return url


def harness_database_name(pid: int, token: str) -> str:
    """Name a per-run database: gobby_harness_<pid>_<12 lowercase hex>."""
    if not 1 <= pid <= _MAX_PID:
        raise HubGuardError("ARG", "the run pid must be a positive seven-digit number")
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise HubGuardError("ARG", "the run token must be 12 lowercase hex digits")
    return f"gobby_harness_{pid}_{token}"


def derive_run_url(guard: HubGuard, dbname: str, *, schema: str | None = None) -> str:
    """Return the per-run URL: the base with its path replaced, or the base in rehearsal mode."""
    if guard.mode is GuardMode.REHEARSAL:
        return guard.base
    if HARNESS_DATABASE_PATTERN.fullmatch(dbname) is None and (
        FIXTURE_DATABASE_PATTERN.fullmatch(dbname) is None
    ):
        raise HubGuardError("ARG", "a per-run database name must match a harness pattern")
    parts = urlsplit(guard.base)
    query = parts.query
    if schema is not None:
        if _SCHEMA_PATTERN.fullmatch(schema) is None:
            raise HubGuardError("ARG", "the search_path schema name must be a plain identifier")
        option = f"options=-csearch_path%3D{schema}"
        query = f"{query}&{option}" if query else option
    return urlunsplit(parts._replace(path=f"/{dbname}", query=query, fragment=""))


def check_derived_url(
    url: str,
    expected_dbname: str,
    expected_schema: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Re-check a per-run URL: same host, port and user, and exactly the expected database.

    Raises HubGuardError. `options` is allowed only as -csearch_path=<expected_schema>.
    """
    source = os.environ if env is None else env
    _check_redirects(source)
    fields, keys = _parse(url, allow_options=True)
    _check_endpoint(fields)
    _check_test_hub_identity(fields, dbname=expected_dbname)
    if keys.count("options") > 1:
        raise HubGuardError("R3", "the per-run URL repeats options")
    if expected_schema is None:
        if "options" in fields:
            raise HubGuardError("R3", "the per-run URL carries options without a schema")
    elif "options" in fields and fields["options"] != f"-csearch_path={expected_schema}":
        raise HubGuardError("R3", "the per-run URL names an unexpected search_path")


def maintenance_url(url: str) -> str:
    """The same hub on the postgres maintenance database, for CREATE and DROP DATABASE."""
    return urlunsplit(urlsplit(url)._replace(path="/postgres"))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def dead_database_names(names: Iterable[str], *, pattern: re.Pattern[str]) -> list[str]:
    """Return the names that fully match pattern whose pid (group 1) is gone."""
    dead: list[str] = []
    for name in names:
        match = pattern.fullmatch(name)
        if match is None:
            continue
        if not _process_alive(int(match[1])):
            dead.append(name)
    return dead


def sweep_dead_pid(admin_url: str, *, pattern: re.Pattern[str]) -> list[str]:
    """Drop every database whose pid is dead and return the names dropped.

    admin_url must be the maintenance URL (maintenance_url). Other sessions share the hub,
    so the names swept are whatever the pattern and liveness select, not a fixed count.
    """
    if urlsplit(admin_url).path != "/postgres":
        raise HubGuardError("ARG", "the sweep must connect to the postgres maintenance database")
    fields, _keys = _parse(admin_url)
    _check_endpoint(fields)
    if fields.get("user") != TEST_HUB_USER:
        raise HubGuardError("R3", "the sweep must connect as the gobby_test user")
    swept: list[str] = []
    with psycopg.connect(admin_url, autocommit=True) as connection:
        rows = connection.execute("SELECT datname FROM pg_database").fetchall()
        for name in dead_database_names([row[0] for row in rows], pattern=pattern):
            with shared_hub_lock_waits(connection, "DROP DATABASE of a dead run"):
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )
            swept.append(name)
    return swept
