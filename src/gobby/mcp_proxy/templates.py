"""MCP server template model, YAML loader, and instance expansion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.storage.secret_names import SECRET_NAME_PATTERN, normalize_secret_name

_SECRET_REF_PREFIX = "$secret:"
_DEFAULT_CONNECT_TIMEOUT = 30.0


@dataclass(frozen=True)
class TemplateParam:
    """One substitutable parameter on an MCP server template."""

    name: str
    env: str | None = None
    arg_flag: str | None = None
    required: bool = False
    secret: bool = False
    default: str | None = None
    default_secret: str | None = None
    choices: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class RequireWhen:
    """Conditional required-params rule keyed off another parameter's value."""

    param: str
    equals: str
    requires: tuple[str, ...]


@dataclass(frozen=True)
class MCPServerTemplate:
    """Validated MCP server template definition."""

    name: str
    description: str
    version: int
    transport: str
    command: str | None
    args: tuple[str, ...]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    connect_timeout: float
    params: tuple[TemplateParam, ...]
    require_one_of: tuple[tuple[str, ...], ...]
    require_when: tuple[RequireWhen, ...]
    runtime_hook: str | None
    override: bool
    enabled: bool = True

    def to_definition(self) -> dict[str, Any]:
        """Serialize this template to a YAML-round-trippable mapping."""
        definition: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict(self.env),
            "headers": dict(self.headers),
            "connect_timeout": self.connect_timeout,
            "params": [_param_to_definition(param) for param in self.params],
            "require_one_of": [list(group) for group in self.require_one_of],
            "require_when": [
                {"param": rule.param, "equals": rule.equals, "requires": list(rule.requires)}
                for rule in self.require_when
            ],
            "runtime_hook": self.runtime_hook,
            "override": self.override,
        }
        return definition

    @classmethod
    def from_definition(cls, data: dict[str, Any]) -> MCPServerTemplate:
        """Parse and validate a template mapping."""
        if not isinstance(data, dict):
            raise ValueError("Template definition must be a mapping")
        name = _require_str(data.get("name"), field="name")
        if not name:
            raise ValueError("Template name must be a non-empty string")
        description = _optional_str(data.get("description"), field="description") or ""
        version = _require_int(data.get("version"), field="version")
        transport = _require_str(data.get("transport"), field="transport")
        command = _optional_str(data.get("command"), field="command")
        url = _optional_str(data.get("url"), field="url")
        args = tuple(_require_str_list(data.get("args", []), field="args"))
        env = _require_str_dict(data.get("env", {}), field="env")
        headers = _require_str_dict(data.get("headers", {}), field="headers")
        connect_timeout = _optional_float(
            data.get("connect_timeout"),
            field="connect_timeout",
            default=_DEFAULT_CONNECT_TIMEOUT,
        )
        raw_params = _require_list(data.get("params", []), field="params")
        params = tuple(_parse_param(item, index) for index, item in enumerate(raw_params))
        names = [param.name for param in params]
        if len(names) != len(set(names)):
            raise ValueError("Template params must have unique names")
        require_one_of = tuple(
            tuple(_require_str_list(group, field="require_one_of"))
            for group in _require_list(data.get("require_one_of", []), field="require_one_of")
        )
        require_when = tuple(
            _parse_require_when(item, index)
            for index, item in enumerate(
                _require_list(data.get("require_when", []), field="require_when")
            )
        )
        runtime_hook = _optional_str(data.get("runtime_hook"), field="runtime_hook")
        override = _optional_bool(data.get("override"), field="override", default=False)
        enabled = _optional_bool(data.get("enabled"), field="enabled", default=True)
        return cls(
            name=name,
            description=description,
            version=version,
            transport=transport,
            command=command,
            args=args,
            url=url,
            env=env,
            headers=headers,
            connect_timeout=connect_timeout,
            params=params,
            require_one_of=require_one_of,
            require_when=require_when,
            runtime_hook=runtime_hook,
            override=override,
            enabled=enabled,
        )


@dataclass(frozen=True)
class ExpandedInstance:
    """Result of expanding a template into a concrete server config."""

    config: MCPServerConfig
    template_values: dict[str, str]
    missing_secrets: list[str]
    optional_missing_secrets: list[str]


def get_bundled_templates_path() -> Path:
    """Return ``src/gobby/install/shared/mcp/templates``."""
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "mcp" / "templates"


def load_template_file(path: Path) -> MCPServerTemplate:
    """Load one template YAML file with ``yaml.safe_load``."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Failed to read template {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid template YAML {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Template {path} must be a mapping")
    return MCPServerTemplate.from_definition(raw)


def expand_template(
    template: MCPServerTemplate,
    *,
    name: str,
    project_id: str,
    values: Mapping[str, str],
    description: str | None,
    secret_exists: Callable[[str], bool],
) -> ExpandedInstance:
    """Materialise a template into an instance config and secret report."""
    known = {param.name: param for param in template.params}
    unknown = [key for key in values if key not in known]
    if unknown:
        known_names = ", ".join(known) if known else "(none)"
        raise ValueError(
            f"Unknown parameter(s): {', '.join(unknown)}. Known parameters: {known_names}"
        )

    resolved: dict[str, str] = {}
    for param in template.params:
        if param.name in values:
            raw = values[param.name]
            if not isinstance(raw, str):
                raw = str(raw)
            if param.secret:
                resolved[param.name] = _normalize_secret_value(
                    param.name, raw, secret_exists=secret_exists
                )
            else:
                resolved[param.name] = raw
        elif param.secret and param.default_secret:
            secret_name = normalize_secret_name(param.default_secret)
            resolved[param.name] = f"{_SECRET_REF_PREFIX}{secret_name}"
        elif param.default is not None:
            resolved[param.name] = param.default

    errors: list[str] = []
    for param in template.params:
        if param.required and param.name not in resolved:
            errors.append(f"Missing required parameter '{param.name}'")
        if param.choices and param.name in resolved and resolved[param.name] not in param.choices:
            message = f"Parameter '{param.name}' must be one of: {', '.join(param.choices)}"
            if not param.secret:
                message += f" (got {resolved[param.name]!r})"
            errors.append(message)

    for group in template.require_one_of:
        if not any(item in resolved for item in group):
            errors.append(f"One of {', '.join(group)} is required")

    required_by_when: set[str] = set()
    for rule in template.require_when:
        if resolved.get(rule.param) == rule.equals:
            required_by_when.update(rule.requires)
            for required_name in rule.requires:
                if required_name not in resolved:
                    errors.append(
                        f"Parameter '{required_name}' is required when {rule.param} is "
                        f"{rule.equals!r}"
                    )

    if errors:
        raise ValueError("; ".join(errors))

    env = dict(template.env)
    args = list(template.args)
    template_values: dict[str, str] = {}
    missing_secrets: list[str] = []
    optional_missing_secrets: list[str] = []

    for param in template.params:
        if param.name not in resolved:
            continue
        value = resolved[param.name]
        template_values[param.name] = value
        if param.secret:
            secret_name = _secret_name_from_reference(value)
            exists = secret_exists(secret_name)
            is_required = param.required or param.name in required_by_when
            if not exists:
                if is_required:
                    missing_secrets.append(secret_name)
                else:
                    optional_missing_secrets.append(secret_name)
                    continue
        if param.env:
            env[param.env] = value
        if param.arg_flag:
            args.extend([param.arg_flag, value])

    config = MCPServerConfig(
        name=name.lower(),
        project_id=project_id,
        transport=template.transport,
        command=template.command,
        args=args,
        url=template.url,
        env=env,
        headers=dict(template.headers),
        connect_timeout=template.connect_timeout,
        description=template.description if description is None else description,
        template=template.name,
        runtime_hook=template.runtime_hook,
        template_values=dict(template_values),
    )
    config.validate()
    return ExpandedInstance(
        config=config,
        template_values=template_values,
        missing_secrets=missing_secrets,
        optional_missing_secrets=optional_missing_secrets,
    )


def expand_server_instance(
    definition: Mapping[str, Any],
    *,
    name: str,
    project_id: str,
    template_values: Mapping[str, Any] | None,
    description: str | None,
    secret_exists: Callable[[str], bool],
) -> dict[str, Any]:
    """Expand a stored template row into template-owned connection fields."""
    template = MCPServerTemplate.from_definition(dict(definition))
    values = {
        str(key): value if isinstance(value, str) else str(value)
        for key, value in (template_values or {}).items()
    }
    expanded = expand_template(
        template,
        name=name,
        project_id=project_id,
        values=values,
        description=description,
        secret_exists=secret_exists,
    )
    config = expanded.config
    return {
        "transport": config.transport,
        "url": config.url,
        "command": config.command,
        "args": config.args,
        "env": config.env,
        "headers": config.headers,
        "connect_timeout": config.connect_timeout,
        "runtime_hook": config.runtime_hook,
    }


def _normalize_secret_value(
    param_name: str,
    raw: str,
    *,
    secret_exists: Callable[[str], bool],
) -> str:
    value = raw.strip()
    if value.startswith(_SECRET_REF_PREFIX):
        secret_name = value[len(_SECRET_REF_PREFIX) :]
        if SECRET_NAME_PATTERN.fullmatch(secret_name) is None:
            raise ValueError(_secret_param_error(param_name))
        return f"{_SECRET_REF_PREFIX}{normalize_secret_name(secret_name)}"
    if SECRET_NAME_PATTERN.fullmatch(value) is not None and secret_exists(
        normalize_secret_name(value)
    ):
        return f"{_SECRET_REF_PREFIX}{normalize_secret_name(value)}"
    raise ValueError(_secret_param_error(param_name))


def _secret_param_error(param_name: str) -> str:
    return (
        f"Parameter '{param_name}' must be a $secret:<name> reference or the name of an "
        "existing secret; write $secret:<name> for a secret that will be set later."
    )


def _secret_name_from_reference(value: str) -> str:
    if value.startswith(_SECRET_REF_PREFIX):
        return normalize_secret_name(value[len(_SECRET_REF_PREFIX) :])
    return normalize_secret_name(value)


def _param_to_definition(param: TemplateParam) -> dict[str, Any]:
    definition: dict[str, Any] = {"name": param.name}
    if param.env is not None:
        definition["env"] = param.env
    if param.arg_flag is not None:
        definition["arg_flag"] = param.arg_flag
    if param.required:
        definition["required"] = True
    if param.secret:
        definition["secret"] = True
    if param.default is not None:
        definition["default"] = param.default
    if param.default_secret is not None:
        definition["default_secret"] = param.default_secret
    if param.choices:
        definition["choices"] = list(param.choices)
    if param.description:
        definition["description"] = param.description
    return definition


def _parse_param(raw: object, index: int) -> TemplateParam:
    field = f"params[{index}]"
    data = _require_mapping(raw, field=field)
    name = _require_str(data.get("name"), field=f"{field}.name")
    choices_raw = data.get("choices", [])
    return TemplateParam(
        name=name,
        env=_optional_str(data.get("env"), field=f"{field}.env"),
        arg_flag=_optional_str(data.get("arg_flag"), field=f"{field}.arg_flag"),
        required=_optional_bool(data.get("required"), field=f"{field}.required", default=False),
        secret=_optional_bool(data.get("secret"), field=f"{field}.secret", default=False),
        default=_optional_str(data.get("default"), field=f"{field}.default"),
        default_secret=_optional_str(data.get("default_secret"), field=f"{field}.default_secret"),
        choices=tuple(_require_str_list(choices_raw, field=f"{field}.choices")),
        description=_optional_str(data.get("description"), field=f"{field}.description") or "",
    )


def _parse_require_when(raw: object, index: int) -> RequireWhen:
    field = f"require_when[{index}]"
    data = _require_mapping(raw, field=field)
    return RequireWhen(
        param=_require_str(data.get("param"), field=f"{field}.param"),
        equals=_require_str(data.get("equals"), field=f"{field}.equals"),
        requires=tuple(_require_str_list(data.get("requires", []), field=f"{field}.requires")),
    )


def _require_mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def _require_list(value: object, *, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_str(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _optional_float(value: object, *, field: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    return float(value)


def _optional_bool(value: object, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _require_str_list(value: object, *, field: str) -> list[str]:
    items = _require_list(value, field=field)
    result: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError(f"{field}[{index}] must be a string")
        result.append(item)
    return result


def _require_str_dict(value: object, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    data = _require_mapping(value, field=field)
    result: dict[str, str] = {}
    for key, item in data.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{field} keys and values must be strings")
        result[key] = item
    return result
