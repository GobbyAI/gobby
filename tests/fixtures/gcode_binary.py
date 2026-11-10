"""Run the cargo-built gcode binary against an isolated test hub with a cached grant."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from gobby.runtime_grants.launch import write_grant_file
from gobby.runtime_grants.schema import (
    AIUnavailableCapability,
    GrantBundle,
    GrantCapabilities,
    GrantDeployment,
    GrantPrincipal,
    PostgresDirect,
    SchemaIdentity,
    UnavailableCapability,
)
from gobby.runtime_grants.signing import sign_grant
from gobby.storage.schema_contract import expected_schema_identity

ROOT = Path(__file__).resolve().parents[2]


def built_gcode_bin() -> str:
    """The gcode that `cargo build -p gobby-code --bin gcode` writes for this checkout.

    Skips when no debug build exists. An installed binary never stands in: a build whose
    version differs from this checkout's gobby-code crate fails the test.
    """
    meta = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    binary = Path(meta["target_directory"]) / "debug" / "gcode"
    if not binary.is_file():
        pytest.skip("build the binary first: cargo build -p gobby-code --bin gcode")
    expected = next(p["version"] for p in meta["packages"] if p["name"] == "gobby-code")
    reported = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True
    ).stdout.split()[-1]
    assert reported == expected, (
        f"built gcode reports {reported}, checkout is {expected}: rebuild before testing"
    )
    return str(binary)


def interactive_grant(
    *, project_id: str, machine_id: str, dsn: str, code_overlay_project_id: str | None = None
) -> GrantBundle:
    # This fixture owns an ordinary per-test hub, not a managed agent DB role.
    now = int(time.time())
    unsigned = GrantBundle(
        config_revision=1,
        deployment=GrantDeployment(token="cafebabedeadbeef", fencing_epoch=1),
        schema_identity=SchemaIdentity.model_validate(expected_schema_identity()),
        principal=GrantPrincipal(
            kind="interactive",
            machine_id=machine_id,
            project_id=project_id,
            execution_id=None,
            session_id=None,
            code_overlay_project_id=code_overlay_project_id,
        ),
        capabilities=GrantCapabilities(
            postgres=PostgresDirect(
                dsn=dsn,
                role_name="gobby_grant_fixture",
                credential_generation=1,
                valid_until=now + 86_400,
            ),
            falkordb=UnavailableCapability(),
            qdrant=UnavailableCapability(),
            embed=AIUnavailableCapability(),
            text_generate=AIUnavailableCapability(),
            tool_chat=AIUnavailableCapability(),
            vision_extract=AIUnavailableCapability(),
            audio_transcribe=AIUnavailableCapability(),
            broker_operations=(),
        ),
        issued_at=now,
        expires_at=now + 86_400,
    )
    return sign_grant(unsigned, "secret")


def cache_interactive_grant(home: Path, daemon_url: str, grant: GrantBundle) -> None:
    principal = grant.principal
    suffix = f"--{principal.code_overlay_project_id}" if principal.code_overlay_project_id else ""
    write_grant_file(
        home / "grants" / grant.deployment.token / f"{principal.project_id}{suffix}.json",
        grant,
    )
    endpoint = daemon_url.strip().rstrip("/")
    digest = hashlib.sha256(endpoint.encode()).hexdigest()[:16]
    binding = home / "grants" / "bindings" / f"{digest}.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps({"endpoint": endpoint, "deployment_token": grant.deployment.token})
    )
    binding.chmod(0o600)
