"""`gobby auth login` and `gobby auth key`: enroll this node with its hub."""

from __future__ import annotations

import hashlib
import os
import platform
import socket
import ssl
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx

from gobby.config.bootstrap import BootstrapConfigError, _parse_hub_daemon_url, is_loopback_host
from gobby.config.bootstrap_io import (
    bootstrap_path,
    publish_bootstrap_yaml_locked,
    read_bootstrap_yaml,
)
from gobby.utils import api_key_format
from gobby.utils.durable_file import durable_replace_text, exclusive_file_lock
from gobby.utils.machine_id import require_machine_id

NETWORK_TIMEOUT_SECONDS = 10.0
CLIENT_SETUP = "docs/guides/shared-stack.md (Client setup)"
SESSION_COOKIE = "gobby_session"
KEY_CLEANUP = "`gobby auth key list` or the hub UI"


@dataclass(frozen=True)
class LoginRequest:
    hub: str | None
    email: str | None
    fingerprint: str | None
    label: str
    insecure: bool


@dataclass(frozen=True)
class LoginPrompts:
    email: Callable[[], str]
    confirm: Callable[[str], bool]
    password: Callable[[], str]


@dataclass(frozen=True)
class Enrollment:
    key_id: str


@dataclass(frozen=True)
class _Hub:
    origin: str
    host: str
    port: int
    tls: bool


@dataclass(frozen=True)
class _Minted:
    key: str
    key_id: str


class _Publication(Enum):
    PUBLISHED = "published"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled back"
    UNRESOLVED = "unresolved"


def enroll(request: LoginRequest, prompts: LoginPrompts) -> Enrollment:
    """Mint a key on the hub, then publish it and the pinned certificate locally."""
    path = bootstrap_path()
    pem_path = path.parent / "tls" / "hub.pem"
    try:
        prior = read_bootstrap_yaml(path)
        machine_id = require_machine_id()
    except (BootstrapConfigError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    hub = _enrolled_hub(prior, request)
    email = request.email or prompts.email()
    staged: Path | None = None
    revoke_error: str | None = None
    try:
        pem: str | None = None
        verify: ssl.SSLContext | bool = True
        if hub.tls:
            pem = _approved_certificate(hub, request.fingerprint, prompts.confirm)
            staged = _stage(pem_path, pem)
            verify = ssl.create_default_context(cafile=str(staged))
        password = prompts.password()
        with httpx.Client(
            base_url=hub.origin, verify=verify, timeout=httpx.Timeout(NETWORK_TIMEOUT_SECONDS)
        ) as client:
            body = {
                "email": email,
                "password": password,
                "machine_id": machine_id,
                "hostname": socket.gethostname(),
                "os": platform.system(),
                "label": request.label,
            }
            try:
                response = client.post("/api/auth/keys/bootstrap", json=body)
            except httpx.HTTPError as exc:
                raise _network_error(hub, exc) from exc
            minted = _verified(response, machine_id, prior.get("api_key_id"))
            outcome, error = _publish(path, pem_path, pem, minted)
            if outcome is _Publication.ROLLED_BACK:
                revoke_error = _revoke(client, email, password, minted.key_id)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)

    if outcome is _Publication.PUBLISHED:
        click.echo(
            f"Enrolled with {hub.origin}: API key {api_key_format.hint(minted.key)} "
            f"(id {minted.key_id})."
        )
        return Enrollment(key_id=minted.key_id)
    if outcome is _Publication.COMMITTED:
        raise click.ClickException(
            f"enrolled key id {minted.key_id}, but its publication could not be verified "
            f"durable: {error}. Check bootstrap.yaml with `gobby auth key --show`."
        )
    if outcome is _Publication.UNRESOLVED:
        raise click.ClickException(
            f"enrollment publication failed and could not be settled: {error}. The hub "
            f"minted key id {minted.key_id}, which was not revoked; these files may have "
            f"changed: {path}, {pem_path}. Check them, and revoke the key with {KEY_CLEANUP} "
            "if bootstrap.yaml does not name it."
        )
    if revoke_error is not None:
        raise click.ClickException(
            f"enrollment publication failed: {error}. The prior enrollment is intact, but "
            f"revoking new key id {minted.key_id} failed: {revoke_error}. Revoke it with "
            f"{KEY_CLEANUP}."
        )
    raise click.ClickException(
        f"enrollment publication failed: {error}. The prior enrollment is intact and "
        "the new key was revoked."
    )


def _enrolled_hub(data: dict[str, object], request: LoginRequest) -> _Hub:
    mode = data.get("datastore_mode", "local")
    if mode != "remote":
        raise click.ClickException(
            f"`gobby auth login` enrolls a node, but this bootstrap is datastore_mode: {mode}. "
            f"Set the node up first; see {CLIENT_SETUP}."
        )
    try:
        origin = _parse_hub_daemon_url(data.get("hub_daemon_url"))
        requested = None if request.hub is None else _parse_hub_daemon_url(request.hub)
    except BootstrapConfigError as exc:
        raise click.ClickException(f"{exc}; see {CLIENT_SETUP}.") from exc
    if requested is not None and requested != origin:
        raise click.ClickException(
            f"--hub {requested} differs from this node's hub_daemon_url {origin}; login "
            f"never re-homes a node. See {CLIENT_SETUP}."
        )
    parsed = urlparse(origin)
    tls = parsed.scheme == "https"
    host = parsed.hostname or ""
    if not tls and request.fingerprint is not None:
        raise click.ClickException(
            f"--fingerprint pins a TLS certificate, but {origin} is plain http://."
        )
    if not tls and not is_loopback_host(host) and not request.insecure:
        raise click.ClickException(
            f"{origin} is plain http:// to a non-loopback host, so the password would "
            "cross the network unencrypted. Use an https:// hub, or pass --insecure."
        )
    return _Hub(origin=origin, host=host, port=parsed.port or (443 if tls else 80), tls=tls)


def _approved_certificate(hub: _Hub, expected: str | None, confirm: Callable[[str], bool]) -> str:
    try:
        pem = ssl.get_server_certificate((hub.host, hub.port), timeout=NETWORK_TIMEOUT_SECONDS)
    except OSError as exc:
        raise click.ClickException(f"could not reach the hub at {hub.origin}: {exc}") from exc
    fingerprint = f"sha256:{hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()}"
    click.echo(f"Hub certificate fingerprint: {fingerprint}")
    if expected is not None:
        if expected.strip().lower() != fingerprint:
            raise click.ClickException(
                f"the hub's certificate {fingerprint} does not match --fingerprint {expected}; "
                "the password was not sent."
            )
    elif not confirm(f"Trust this certificate for {hub.origin}?"):
        raise click.ClickException("certificate not trusted; the password was not sent.")
    return pem


def _stage(pem_path: Path, pem: str) -> Path:
    pem_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{pem_path.name}.", suffix=".staged", dir=pem_path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(pem)
    return Path(name)


def _network_error(hub: _Hub, exc: httpx.HTTPError) -> click.ClickException:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return click.ClickException(
                f"the TLS handshake with {hub.origin} failed: {cause.verify_message}. The "
                f"hub's certificate does not cover {hub.host}; add it to front_door.tls.sans "
                "on the hub, or dial a name the certificate lists. The password was not sent."
            )
        cause = cause.__cause__ or cause.__context__
    return click.ClickException(f"could not reach the hub at {hub.origin}: {exc}")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _same_uuid(left: object, right: object) -> bool:
    return _is_uuid(left) and _is_uuid(right) and uuid.UUID(str(left)) == uuid.UUID(str(right))


def _verified(response: httpx.Response, machine_id: str, prior_key_id: object) -> _Minted:
    if response.status_code == 401:
        raise click.ClickException("the hub rejected the email or password.")
    if not response.is_success:
        raise click.ClickException(f"the hub refused enrollment (HTTP {response.status_code}).")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    key, key_id = body.get("key"), body.get("key_id")
    if (
        isinstance(key, str)
        and api_key_format.parse(key) is not None
        and isinstance(key_id, str)
        and all(_is_uuid(body.get(name)) for name in ("key_id", "user_id", "machine_id"))
        and _same_uuid(body.get("machine_id"), machine_id)
        and body.get("hint") == api_key_format.hint(key)
        and not _same_uuid(key_id, prior_key_id)
    ):
        return _Minted(key=key, key_id=key_id)
    reported = f": key id {key_id} (unverified)" if _is_uuid(key_id) else ""
    raise click.ClickException(
        "the hub's enrollment response failed verification, so nothing was saved and "
        f"nothing was revoked. An unverified key may have been minted{reported}. "
        f"Review it with {KEY_CLEANUP}."
    )


def _publish(
    path: Path, pem_path: Path, pem: str | None, minted: _Minted
) -> tuple[_Publication, Exception | None]:
    """Publish under the bootstrap lock; on failure, settle by the publication point."""
    with exclusive_file_lock(path):
        try:
            prior_pem = pem_path.read_bytes().decode("utf-8") if pem and pem_path.exists() else None
        except (OSError, UnicodeDecodeError) as exc:
            return _Publication.ROLLED_BACK, exc
        try:
            if pem is not None:
                durable_replace_text(pem_path, pem)
            data = read_bootstrap_yaml(path)
            data["api_key"] = minted.key
            data["api_key_id"] = minted.key_id
            if pem is not None:
                data["hub_cert"] = str(pem_path)
            else:
                data.pop("hub_cert", None)
            publish_bootstrap_yaml_locked(path, data)
        # Any failure must be settled here, or a minted key and a swapped pin are orphaned.
        except Exception as exc:
            return _settle(path, pem_path, pem is not None, prior_pem, minted.key_id, exc)
    return _Publication.PUBLISHED, None


def _settle(
    path: Path,
    pem_path: Path,
    pinned: bool,
    prior_pem: str | None,
    key_id: str,
    error: Exception,
) -> tuple[_Publication, Exception]:
    """Decide by what bootstrap names now; restore the prior pin when it is not ours."""
    try:
        if read_bootstrap_yaml(path).get("api_key_id") == key_id:
            return _Publication.COMMITTED, error
        if pinned and prior_pem is None:
            pem_path.unlink(missing_ok=True)
        elif pinned and prior_pem is not None:
            durable_replace_text(pem_path, prior_pem)
    except (OSError, BootstrapConfigError) as settle_error:
        return _Publication.UNRESOLVED, settle_error
    return _Publication.ROLLED_BACK, error


def _revoke(client: httpx.Client, email: str, password: str, key_id: str) -> str | None:
    """Revoke ``key_id`` through a password session; return the failure, if any."""
    try:
        login_response = client.post("/api/auth/login", json={"email": email, "password": password})
        if not login_response.is_success:
            return f"hub login returned HTTP {login_response.status_code}"
        cookies: SimpleCookie = SimpleCookie()
        for header in login_response.headers.get_list("set-cookie"):
            cookies.load(header)
        session = cookies.get(SESSION_COOKIE)
        if session is None:
            return "hub login set no session cookie"
        client.cookies.clear()
        response = client.delete(
            f"/api/auth/keys/{key_id}", headers={"Cookie": f"{SESSION_COOKIE}={session.value}"}
        )
    except httpx.HTTPError as exc:
        return str(exc)
    return None if response.is_success else f"hub revoke returned HTTP {response.status_code}"


@click.command("login")
@click.option("--hub", default=None, help="Hub origin; defaults to hub_daemon_url.")
@click.option("--email", default=None, help="Hub account email; prompted when omitted.")
@click.option("--fingerprint", default=None, help="Expected sha256: certificate fingerprint.")
@click.option("--label", default=None, help="Key label; defaults to this hostname.")
@click.option("--insecure", is_flag=True, help="Allow plain HTTP to a non-loopback hub.")
def login(
    hub: str | None,
    email: str | None,
    fingerprint: str | None,
    label: str | None,
    insecure: bool,
) -> None:
    """Enroll this node with its hub and store the minted API key."""
    request = LoginRequest(
        hub=hub,
        email=email,
        fingerprint=fingerprint,
        label=label or socket.gethostname(),
        insecure=insecure,
    )
    prompts = LoginPrompts(
        email=lambda: str(click.prompt("Email")),
        confirm=lambda message: click.confirm(message, default=False),
        password=lambda: str(click.prompt("Password", hide_input=True)),
    )
    enroll(request, prompts)


@click.command("key")
@click.option("--show", is_flag=True, help="Print the enrolled key's hint and id.")
def key(show: bool) -> None:
    """Inspect this node's enrolled API key (never the key itself)."""
    if not show:
        raise click.UsageError("pass --show to print the enrolled key's hint and id")
    try:
        data = read_bootstrap_yaml(bootstrap_path())
    except BootstrapConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    api_key, key_id = data.get("api_key"), data.get("api_key_id")
    if not isinstance(api_key, str) or not isinstance(key_id, str):
        raise click.ClickException("no API key is enrolled; run `gobby auth login` first.")
    click.echo(f"API key {api_key_format.hint(api_key)} (id {key_id})")
