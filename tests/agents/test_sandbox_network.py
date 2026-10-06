"""A definition's ``network`` sets its spawn's SRT egress (placed-agent-launch 4.3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.external_write_grants import apply_write_grant
from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.sandbox_domains import GIT_DOMAINS, PACKAGE_REGISTRY_DOMAINS, trusted_domains
from gobby.agents.sandbox_network import apply_network_override
from gobby.agents.sandbox_policy import allowed_domains
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.mcp_proxy.tools.spawn_agent._sandbox_gate import resolve_spawn_sandbox
from gobby.workflows.agent_models import AgentDefinitionBody

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]

_IMPL = "gobby.mcp_proxy.tools.spawn_agent._implementation"
_SEED_FILES = "gobby.agents.sandbox_domains.files"


def _body(network: Literal["none", "trusted"]) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "net-agent",
            "provider": "claude",
            "prompts": {"agent": "Run the assigned task."},
            "workflows": {"rule_selectors": {"include": []}},
            "network": network,
        }
    )


async def test_trusted_definition_adds_seed_to_srt_allowlist(tmp_path: Path) -> None:
    daemon_config = DaemonConfig()
    grant = {"canonical_roots": [str(tmp_path)]}
    base = apply_write_grant(agent_sandbox_config(daemon_config), grant)

    trusted = await resolve_spawn_sandbox(daemon_config, grant, _body("trusted"))

    assert isinstance(trusted, SandboxConfig)
    assert (trusted.enabled, trusted.backend, trusted.allow_network) == (True, "srt", False)
    assert trusted.extra_write_paths == base.extra_write_paths
    domains = set(allowed_domains(trusted, None, None))
    expected = {*trusted_domains(), *GIT_DOMAINS, *PACKAGE_REGISTRY_DOMAINS, "localhost"}
    assert {domain.lower() for domain in expected} <= domains
    for body in (_body("none"), None):
        assert await resolve_spawn_sandbox(daemon_config, grant, body) == base


@pytest.mark.parametrize(
    ("definition", "override", "effective"),
    [
        pytest.param("none", None, "none", id="inherits-none"),
        pytest.param("trusted", None, "trusted", id="inherits-trusted"),
        pytest.param("none", "trusted", "trusted", id="widens-to-trusted"),
        pytest.param("trusted", "none", "none", id="narrows-to-none"),
    ],
)
async def test_override_sets_the_profile_for_one_launch(
    tmp_path: Path,
    definition: Literal["none", "trusted"],
    override: Literal["none", "trusted"] | None,
    effective: Literal["none", "trusted"],
) -> None:
    daemon_config = DaemonConfig()
    grant = {"canonical_roots": [str(tmp_path)]}
    loaded = _body(definition)

    launch = apply_network_override(loaded, override)

    assert launch is not None
    assert launch.network == effective
    assert await resolve_spawn_sandbox(daemon_config, grant, launch) == (
        await resolve_spawn_sandbox(daemon_config, grant, _body(effective))
    )
    # The loaded definition keeps its profile, so a later launch with no override inherits it.
    assert loaded.network == definition
    later = apply_network_override(loaded, None)
    assert later is not None
    assert later.network == definition


@pytest.mark.parametrize(
    ("body", "override"),
    [
        pytest.param(_body("none"), "open", id="unknown-profile"),
        pytest.param(_body("trusted"), "", id="empty-profile"),
        pytest.param(None, "trusted", id="no-definition-trusted"),
        pytest.param(None, "none", id="no-definition-none"),
    ],
)
def test_override_refuses_unknown_profile_or_missing_definition(
    body: AgentDefinitionBody | None, override: str
) -> None:
    with pytest.raises(ValueError, match="network"):
        apply_network_override(body, override)


def test_no_override_without_definition_stays_unresolved() -> None:
    assert apply_network_override(None, None) is None


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    return runner


@pytest.fixture
def seed_read() -> Iterator[MagicMock]:
    """The real ``trusted_domains`` loader over a controlled seed file read."""
    trusted_domains.cache_clear()
    with patch(_SEED_FILES) as package_files:
        yield package_files.return_value.joinpath.return_value.read_text
    trusted_domains.cache_clear()


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(FileNotFoundError("trusted_domains.json"), id="missing"),
        pytest.param("{", id="malformed-json"),
        pytest.param("[]", id="top-level-array"),
        pytest.param("{}", id="no-categories"),
        pytest.param('{"categories": []}', id="categories-not-object"),
        pytest.param('{"categories": {"git": "github.com"}}', id="domains-not-list"),
        pytest.param('{"categories": {"git": [7]}}', id="domain-not-string"),
    ],
)
async def test_unreadable_seed_refuses_trusted_spawn_only(
    seed: str | Exception, seed_read: MagicMock
) -> None:
    if isinstance(seed, Exception):
        seed_read.side_effect = seed
    else:
        seed_read.return_value = seed
    runner = _runner()
    with (
        patch(f"{_IMPL}.authorize_write_grant", return_value=None),
        patch(f"{_IMPL}.get_project_context", return_value={"id": "p", "project_path": "/repo"}),
        patch(f"{_IMPL}.get_machine_id", return_value="21000000-0000-4000-8000-000000000001"),
        patch(f"{_IMPL}.get_isolation_handler") as isolation,
        patch(f"{_IMPL}.prepare_terminal_spawn") as prepare,
        patch(f"{_IMPL}.execute_spawn") as execute,
    ):
        result: dict[str, Any] = await spawn_agent_impl(
            terminal_backend="native",
            prompt="Do the thing",
            runner=runner,
            provider="claude",
            agent_body=_body("trusted"),
            parent_session_id="parent-session-xyz",
            daemon_config=DaemonConfig(),
        )
        assert result["success"] is False
        assert result["error_code"] == "sandbox_required"
        runner.can_spawn.assert_not_called()
        isolation.assert_not_called()
        prepare.assert_not_called()
        execute.assert_not_called()

        seed_read.reset_mock()
        none = await resolve_spawn_sandbox(DaemonConfig(), None, _body("none"))
        assert isinstance(none, SandboxConfig)
        seed_read.assert_not_called()
