"""Tests for MCP server template loading and expansion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.templates import (
    MCPServerTemplate,
    RequireWhen,
    TemplateParam,
    expand_template,
    get_bundled_templates_path,
    load_template_file,
)

pytestmark = pytest.mark.unit

_PROJECT_ID = "11111111-1111-1111-1111-111111111111"
_BUNDLED_TEMPLATE_NAMES = frozenset(
    {
        "openapi",
        "github",
        "linear",
        "brave-search",
        "context7",
        "playwright",
        "chrome-devtools",
    }
)


def _param(**overrides: Any) -> TemplateParam:
    values: dict[str, Any] = {"name": "token"}
    values.update(overrides)
    return TemplateParam(**values)


def _template(**overrides: Any) -> MCPServerTemplate:
    values: dict[str, Any] = {
        "name": "demo",
        "description": "Demo template",
        "version": 1,
        "transport": "stdio",
        "command": "npx",
        "args": ("-y", "demo"),
        "url": None,
        "env": {},
        "headers": {},
        "connect_timeout": 30.0,
        "params": (),
        "require_one_of": (),
        "require_when": (),
        "runtime_hook": None,
        "override": False,
        "enabled": True,
    }
    values.update(overrides)
    return MCPServerTemplate(**values)


def _expand(
    template: MCPServerTemplate,
    values: Mapping[str, str],
    *,
    name: str = "demo",
    secret_exists: Callable[[str], bool] | None = None,
) -> Any:
    return expand_template(
        template,
        name=name,
        project_id=_PROJECT_ID,
        values=values,
        description=None,
        secret_exists=secret_exists or (lambda _secret: False),
    )


def _load_bundled() -> dict[str, MCPServerTemplate]:
    bundled = get_bundled_templates_path()
    loaded: dict[str, MCPServerTemplate] = {}
    for path in sorted(bundled.glob("*.yaml")):
        template = load_template_file(path)
        loaded[template.name] = template
    return loaded


def test_seven_bundled_templates_load_and_openapi_contract() -> None:
    templates = _load_bundled()
    assert set(templates) == _BUNDLED_TEMPLATE_NAMES

    openapi = templates["openapi"]
    assert openapi.command == "uvx"
    assert openapi.args == ("awslabs.openapi-mcp-server@1.1.5", "--log-level", "ERROR")
    assert openapi.connect_timeout == 120.0
    assert [param.name for param in openapi.params] == [
        "api_name",
        "api_base_url",
        "spec_url",
        "spec_path",
        "auth_type",
        "auth_token",
        "auth_api_key",
        "auth_api_key_name",
        "auth_api_key_in",
        "auth_username",
        "auth_password",
        "include_tags",
        "exclude_tags",
        "allow_insecure_http",
        "allow_private_networks",
    ]
    assert openapi.require_one_of == (("spec_url", "spec_path"),)
    by_name = {param.name: param for param in openapi.params}
    assert by_name["api_name"].required is True
    assert by_name["api_base_url"].env == "API_BASE_URL"
    assert by_name["auth_type"].choices == ("none", "bearer", "api_key", "basic")
    assert by_name["auth_type"].default == "none"
    assert by_name["auth_token"].secret is True
    assert by_name["auth_api_key"].secret is True
    assert by_name["auth_password"].secret is True
    assert by_name["auth_api_key_in"].choices == ("header", "query", "cookie")
    assert by_name["allow_insecure_http"].choices == ("true", "false")
    equals_to_requires = {
        rule.equals: rule.requires for rule in openapi.require_when if rule.param == "auth_type"
    }
    assert equals_to_requires["bearer"] == ("auth_token",)
    assert equals_to_requires["api_key"] == ("auth_api_key",)
    assert equals_to_requires["basic"] == ("auth_username", "auth_password")


def test_expand_template_materialises_env_and_args() -> None:
    template = _template(
        params=(
            _param(name="token", env="API_TOKEN", required=True, secret=True),
            _param(name="region", arg_flag="--region"),
        )
    )
    expanded = _expand(
        template,
        {"token": "$secret:api_token", "region": "us-east-1"},
        secret_exists=lambda name: name == "api_token",
    )

    assert expanded.config.command == "npx"
    assert expanded.config.args == ["-y", "demo", "--region", "us-east-1"]
    assert expanded.config.env == {"API_TOKEN": "$secret:api_token"}
    assert expanded.config.project_id == _PROJECT_ID
    assert expanded.config.name == "demo"
    assert expanded.missing_secrets == []
    assert expanded.optional_missing_secrets == []


def test_expand_template_rejects_unknown_params() -> None:
    template = _template(params=(_param(name="token", env="API_TOKEN", secret=True),))

    with pytest.raises(ValueError, match="Unknown") as excinfo:
        _expand(template, {"token": "$secret:api_token", "extra": "nope"})

    message = str(excinfo.value).lower()
    assert "extra" in message
    assert "token" in message
    assert "nope" not in message


def test_expand_template_aggregates_required_conditional_and_choice_errors() -> None:
    template = _template(
        params=(
            _param(name="api_name", env="API_NAME", required=True),
            _param(name="spec_url", env="API_SPEC_URL"),
            _param(name="spec_path", env="API_SPEC_PATH"),
            _param(
                name="auth_type",
                env="AUTH_TYPE",
                default="none",
                choices=("none", "bearer"),
            ),
            _param(name="auth_token", env="AUTH_TOKEN", secret=True),
            _param(name="mode", env="MODE", choices=("a", "b")),
        ),
        require_one_of=(("spec_url", "spec_path"),),
        require_when=(RequireWhen(param="auth_type", equals="bearer", requires=("auth_token",)),),
    )

    with pytest.raises(ValueError) as excinfo:
        _expand(template, {"auth_type": "bearer", "mode": "nope"})

    message = str(excinfo.value)
    assert "api_name" in message
    assert "spec_url" in message
    assert "auth_token" in message
    assert "mode" in message


def test_expand_template_reports_missing_secrets_by_name() -> None:
    template = _template(
        params=(_param(name="token", env="API_TOKEN", required=True, secret=True),)
    )
    expanded = _expand(template, {"token": "$secret:api_token"})

    assert expanded.missing_secrets == ["api_token"]
    assert expanded.config.env == {"API_TOKEN": "$secret:api_token"}


def test_expand_template_normalizes_secret_references() -> None:
    template = _template(
        params=(_param(name="token", env="API_TOKEN", required=True, secret=True),)
    )
    expanded = _expand(
        template,
        {"token": "api_token"},
        secret_exists=lambda name: name == "api_token",
    )

    assert expanded.template_values == {"token": "$secret:api_token"}
    assert expanded.config.env == {"API_TOKEN": "$secret:api_token"}
    assert expanded.config.template_values == {"token": "$secret:api_token"}
    assert "s3cret" not in str(expanded.template_values)
    assert expanded.missing_secrets == []


def test_expand_template_required_and_optional_missing_secrets() -> None:
    template = _template(
        params=(
            _param(
                name="token",
                env="API_TOKEN",
                required=True,
                secret=True,
                default_secret="api_token",
            ),
            _param(
                name="api_key",
                arg_flag="--api-key",
                secret=True,
                default_secret="optional_api_key",
            ),
        )
    )
    expanded = _expand(template, {})

    assert expanded.template_values["token"] == "$secret:api_token"
    assert expanded.template_values["api_key"] == "$secret:optional_api_key"
    assert expanded.config.env == {"API_TOKEN": "$secret:api_token"}
    assert expanded.config.args == ["-y", "demo"]
    assert expanded.missing_secrets == ["api_token"]
    assert expanded.optional_missing_secrets == ["optional_api_key"]


def test_bundled_template_definitions_match_legacy_contracts() -> None:
    templates = _load_bundled()

    github = _expand(templates["github"], {})
    assert github.config.command == "npx"
    assert github.config.args == ["-y", "@modelcontextprotocol/server-github"]
    assert github.config.env == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "$secret:github_personal_access_token"
    }
    assert github.missing_secrets == ["github_personal_access_token"]
    assert github.config.runtime_hook is None

    linear = _expand(templates["linear"], {})
    assert linear.config.command == "npx"
    assert linear.config.args == ["-y", "mcp-linear"]
    assert linear.config.env == {"LINEAR_API_KEY": "$secret:linear_api_key"}
    assert linear.missing_secrets == ["linear_api_key"]

    brave = _expand(templates["brave-search"], {})
    assert brave.config.command == "npx"
    assert brave.config.args == ["-y", "@brave/brave-search-mcp-server"]
    assert brave.config.env == {"BRAVE_API_KEY": "$secret:brave_api_key"}
    assert brave.missing_secrets == ["brave_api_key"]

    context7 = _expand(templates["context7"], {})
    assert context7.config.command == "npx"
    assert context7.config.args == ["-y", "@upstash/context7-mcp"]
    assert context7.config.env in (None, {})
    assert "--api-key" not in (context7.config.args or [])
    assert context7.optional_missing_secrets == ["context7_api_key"]
    assert context7.missing_secrets == []
    assert context7.template_values["api_key"] == "$secret:context7_api_key"

    playwright = _expand(templates["playwright"], {})
    assert playwright.config.command == "npx"
    assert playwright.config.args == ["-y", "@playwright/mcp@latest"]
    assert playwright.config.runtime_hook is None
    assert playwright.template_values == {}

    chrome = _expand(templates["chrome-devtools"], {})
    assert chrome.config.command == "npx"
    assert chrome.config.args == [
        "-y",
        "chrome-devtools-mcp@0.21.0",
        "--no-usage-statistics",
    ]
    assert chrome.config.runtime_hook == "chrome_executable_path"
    assert chrome.template_values == {}


def test_template_enabled_defaults_true_and_round_trips() -> None:
    defaulted = MCPServerTemplate.from_definition(
        {
            "name": "demo",
            "description": "Demo",
            "version": 1,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "demo"],
        }
    )
    assert defaulted.enabled is True

    disabled = MCPServerTemplate.from_definition(
        {
            **defaulted.to_definition(),
            "enabled": False,
        }
    )
    assert disabled.enabled is False
    assert disabled.to_definition()["enabled"] is False
    assert MCPServerTemplate.from_definition(disabled.to_definition()) == disabled


def test_secret_params_require_reference_or_existing_name() -> None:
    template = _template(
        params=(_param(name="token", env="API_TOKEN", required=True, secret=True),)
    )

    with pytest.raises(ValueError, match="token") as excinfo:
        _expand(template, {"token": "ghp_abc123"})
    assert "ghp_abc123" not in str(excinfo.value)
    assert "$secret:" in str(excinfo.value)

    existing = _expand(
        template,
        {"token": "api_token"},
        secret_exists=lambda name: name == "api_token",
    )
    assert existing.template_values["token"] == "$secret:api_token"
    assert existing.missing_secrets == []

    forward = _expand(template, {"token": "$secret:not_yet_set"})
    assert forward.template_values["token"] == "$secret:not_yet_set"
    assert forward.missing_secrets == ["not_yet_set"]


def test_expand_template_lowercases_instance_name() -> None:
    template = _template()
    expanded = _expand(template, {}, name="GitHub")
    assert expanded.config.name == "github"


def test_bundled_module_exposes_only_runtime_hooks() -> None:
    import gobby.mcp_proxy.bundled as bundled

    assert not hasattr(bundled, "DEFAULT_EXTERNAL_MCP_SERVERS")
    assert not hasattr(bundled, "BUNDLED_EXTERNAL_MCP_SERVER_NAMES")
    assert not hasattr(bundled, "is_bundled_external_mcp_server")
    assert not hasattr(bundled, "canonical_project_id_for_server")
    assert not hasattr(bundled, "normalize_persisted_args")
    assert not hasattr(bundled, "normalize_bundled_managed_args")
    assert not hasattr(bundled, "normalize_bundled_server_config")
    assert hasattr(bundled, "resolve_runtime_stdio_args")
    assert hasattr(bundled, "resolve_chrome_devtools_executable_path")
    assert hasattr(bundled, "prefers_offline_npx")


def test_shared_agents_documents_mcp_override_roots() -> None:
    agents = (
        Path(__file__).resolve().parents[2] / "src" / "gobby" / "install" / "shared" / "AGENTS.md"
    )
    text = agents.read_text(encoding="utf-8")
    assert ".gobby/mcp/templates/" in text
    assert ".gobby/mcp/servers/" in text
    assert "override: true" in text


def test_choices_error_for_secret_param_does_not_echo_value() -> None:
    template = _template(
        params=(
            _param(name="mode", env="MODE", choices=("a", "b")),
            _param(name="token", env="TOKEN", secret=True, choices=("a", "b")),
        ),
    )

    with pytest.raises(ValueError) as excinfo:
        _expand(template, {"mode": "nope", "token": "$secret:super_secret"})

    message = str(excinfo.value)
    assert "'mode' must be one of" in message
    assert "nope" in message
    assert "'token' must be one of" in message
    assert "super_secret" not in message
