"""Typed, immutable registry of runtime configuration keys."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from types import MappingProxyType, UnionType
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, to_jsonable_python

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.embedding_keys import (
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
    AI_EMBEDDINGS_CONFIG_PREFIX,
    EMBEDDING_API_BASE_FIELD,
    EMBEDDING_CATALOG_KEY_FIELD,
    EMBEDDING_DIM_FIELD,
    EMBEDDING_MODEL_FIELD,
    EMBEDDING_QUERY_PREFIX_FIELD,
    EMBEDDING_SWITCH_COMPLETED_KEY,
    EMBEDDING_SWITCH_JOURNAL_KEY,
    MCP_SCOPED_PAYLOAD_VERSION_KEY,
    RUNTIME_EMBEDDINGS_CONFIG_PREFIX,
    runtime_embedding_key,
)


class RegistryError(ValueError):
    """Raised when registry declarations are incomplete or ambiguous."""


class UnknownConfigKeyError(KeyError):
    """Raised when a key is absent from the public registry contract."""


class ActivationPolicy(StrEnum):
    """How a stored change becomes active."""

    LIVE = "live"
    RESTART_REQUIRED = "restart_required"
    MANAGED = "managed"


class ConfigVisibility(StrEnum):
    """The single exposure partition that owns a key."""

    PUBLIC = "public"
    MACHINE = "machine"
    RESTRICTED = "restricted"


class ConfigSecrecy(StrEnum):
    """How a value may be represented outside its owning store."""

    NONE = "none"
    REFERENCE = "reference"
    PAYLOAD = "payload"


_MISSING = object()
_SAFE_SEGMENT_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~"
)
_UPPER_HEX = frozenset("0123456789ABCDEF")

DYNAMIC_SEGMENT_CODEC_VECTORS: tuple[tuple[str, str], ...] = (
    ("plain", "plain"),
    ("AZaz09-_~", "AZaz09-_~"),
    ("dot.segment", "dot%2Esegment"),
    ("percent%sign", "percent%25sign"),
    ("already%2Eencoded", "already%252Eencoded"),
    ("slash/value", "slash%2Fvalue"),
    ("space value", "space%20value"),
    ("child.looking", "child%2Elooking"),
    ("café", "caf%C3%A9"),
    ("配置", "%E9%85%8D%E7%BD%AE"),
    ("🙂", "%F0%9F%99%82"),
    ("é", "%C3%A9"),
    ("e\u0301", "e%CC%81"),
)

INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS: tuple[str, ...] = ("\ud800",)

INVALID_DYNAMIC_SEGMENTS: tuple[str, ...] = (
    "",
    "%",
    "%2",
    "%GG",
    "%2e",
    "%41",
    "%7E",
    "raw.dot",
    "raw space",
    "bad+plus",
    "bad=equals",
    "%C0%AF",
    "%E0%80%AF",
    "%ED%A0%80",
    "é",
    "%FF",
)


def encode_dynamic_segment(value: str) -> str:
    """Encode one logical dynamic segment into its canonical UTF-8 form."""
    if not value:
        raise ValueError("Dynamic config segment must not be empty")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Dynamic config segment is not encodable UTF-8") from error
    encoded: list[str] = []
    for byte in raw:
        if byte in _SAFE_SEGMENT_BYTES:
            encoded.append(chr(byte))
        else:
            encoded.append(f"%{byte:02X}")
    return "".join(encoded)


def decode_dynamic_segment(value: str) -> str:
    """Decode a canonical segment and reject alternate or malformed spellings."""
    if not value:
        raise ValueError("Dynamic config segment must not be empty")
    decoded_bytes = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if index + 2 >= len(value):
                raise ValueError("Truncated percent escape in dynamic config segment")
            digits = value[index + 1 : index + 3]
            if any(digit not in _UPPER_HEX for digit in digits):
                raise ValueError("Percent escapes must use uppercase hexadecimal digits")
            decoded_bytes.append(int(digits, 16))
            index += 3
            continue
        byte = ord(character)
        if byte not in _SAFE_SEGMENT_BYTES:
            raise ValueError("Dynamic config segment is not canonically encoded")
        decoded_bytes.append(byte)
        index += 1
    try:
        decoded = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Dynamic config segment is not valid UTF-8") from error
    if encode_dynamic_segment(decoded) != value:
        raise ValueError("Dynamic config segment uses a noncanonical escape")
    return decoded


def _namespace(key: str) -> str:
    parts = key.split(".")
    if parts[0] == "ai" and len(parts) > 1:
        return ".".join(parts[:2])
    return parts[0]


@dataclass(frozen=True, slots=True)
class ConfigFieldSpec:
    """Field-specific metadata for model-valued configuration entries."""

    name: str
    annotation: object
    default: object = _MISSING
    secrecy: ConfigSecrecy = ConfigSecrecy.NONE
    identity: bool = False

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING


@dataclass(frozen=True, slots=True, eq=False)
class ConfigKeySpec:
    """Complete contract for one exact runtime configuration key."""

    key: str
    annotation: object
    default: object = _MISSING
    source_path: str | None = None
    activation: ActivationPolicy = ActivationPolicy.LIVE
    visibility: ConfigVisibility = ConfigVisibility.PUBLIC
    secrecy: ConfigSecrecy = ConfigSecrecy.NONE
    machine_export: bool = False
    description: str | None = None
    field_specs: tuple[ConfigFieldSpec, ...] = ()

    @property
    def namespace(self) -> str:
        return _namespace(self.key)

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING


@dataclass(frozen=True, slots=True, eq=False)
class ConfigPatternSpec:
    """Contract for one family with canonically encoded dynamic segments."""

    pattern: str
    annotation: object
    default: object = _MISSING
    source_path: str | None = None
    activation: ActivationPolicy = ActivationPolicy.LIVE
    visibility: ConfigVisibility = ConfigVisibility.PUBLIC
    secrecy: ConfigSecrecy = ConfigSecrecy.NONE
    machine_export: bool = False
    description: str | None = None
    field_specs: tuple[ConfigFieldSpec, ...] = ()

    @property
    def key(self) -> str:
        return self.pattern

    @property
    def namespace(self) -> str:
        return _namespace(self.pattern)

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING

    @property
    def placeholders(self) -> tuple[str, ...]:
        return tuple(
            segment[1:-1]
            for segment in self.pattern.split(".")
            if segment.startswith("{") and segment.endswith("}")
        )

    @property
    def dynamic_placeholders(self) -> tuple[str, ...]:
        return self.placeholders

    @property
    def dynamic_segment_indexes(self) -> tuple[int, ...]:
        return tuple(
            index
            for index, segment in enumerate(self.pattern.split("."))
            if segment.startswith("{") and segment.endswith("}")
        )

    @property
    def example_segments(self) -> Mapping[str, str]:
        examples = dict.fromkeys(self.placeholders, "example")
        if self.field_specs and "field" in examples:
            examples["field"] = self.field_specs[0].name
        return MappingProxyType(examples)

    def format(self, **segments: str) -> str:
        """Render a canonical storage key from logical placeholder values."""
        expected = set(self.placeholders)
        if set(segments) != expected:
            raise ValueError(f"Expected dynamic segments {sorted(expected)}")
        if self.field_specs and segments.get("field") not in {
            field.name for field in self.field_specs
        }:
            raise ValueError(f"Unsupported field for pattern {self.pattern}")
        rendered: list[str] = []
        for segment in self.pattern.split("."):
            if segment.startswith("{") and segment.endswith("}"):
                rendered.append(encode_dynamic_segment(segments[segment[1:-1]]))
            else:
                rendered.append(segment)
        return ".".join(rendered)

    def match(self, key: str) -> dict[str, str] | None:
        """Return decoded placeholders when a canonical storage key matches."""
        pattern_parts = self.pattern.split(".")
        key_parts = key.split(".")
        if len(pattern_parts) != len(key_parts):
            return None
        matched: dict[str, str] = {}
        try:
            for expected, actual in zip(pattern_parts, key_parts, strict=True):
                if expected.startswith("{") and expected.endswith("}"):
                    matched[expected[1:-1]] = decode_dynamic_segment(actual)
                elif expected != actual:
                    return None
        except ValueError:
            return None
        if self.field_specs and matched.get("field") not in {
            field.name for field in self.field_specs
        }:
            return None
        return matched


RegistrySpec = ConfigKeySpec | ConfigPatternSpec


class ConfigRegistry:
    """Validated immutable lookup over exact and patterned configuration specs."""

    def __init__(
        self,
        key_specs: tuple[ConfigKeySpec, ...],
        pattern_specs: tuple[ConfigPatternSpec, ...],
        *,
        bootstrap_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.key_specs = key_specs
        self.pattern_specs = pattern_specs
        self.specs: tuple[RegistrySpec, ...] = (*key_specs, *pattern_specs)
        self._by_key = MappingProxyType({spec.key: spec for spec in key_specs})
        self._validate(bootstrap_paths)

    def _validate(self, bootstrap_paths: frozenset[str]) -> None:
        if len(self._by_key) != len(self.key_specs):
            raise RegistryError("Exact configuration keys overlap")
        for spec in self.specs:
            if spec.visibility is ConfigVisibility.RESTRICTED and spec.machine_export:
                raise RegistryError(f"Restricted key cannot be machine-exported: {spec.key}")
            if (
                spec.machine_export or spec.visibility is ConfigVisibility.MACHINE
            ) and _spec_carries_secrets(spec):
                raise RegistryError(f"Secret-bearing key cannot be machine-exported: {spec.key}")
            if spec.secrecy is ConfigSecrecy.PAYLOAD and (
                spec.visibility is not ConfigVisibility.RESTRICTED
            ):
                raise RegistryError(f"Secret payload must be restricted: {spec.key}")
            if config_structured_reference_fields(spec):
                config_structured_identity_field(spec)
        for key in self._by_key:
            matches = [pattern for pattern in self.pattern_specs if pattern.match(key) is not None]
            if matches:
                raise RegistryError(f"Exact key overlaps a pattern: {key}")
        for index, left in enumerate(self.pattern_specs):
            for right in self.pattern_specs[index + 1 :]:
                if _patterns_overlap(left, right):
                    raise RegistryError(f"Configuration patterns overlap: {left.key}, {right.key}")
        conflicts = [path for path in bootstrap_paths if self._matches(path)]
        if conflicts:
            raise RegistryError(f"Bootstrap-owned keys entered runtime registry: {conflicts}")

    def _matches(self, key: str) -> bool:
        return key in self._by_key or any(
            pattern.match(key) is not None for pattern in self.pattern_specs
        )

    def resolve(self, key: str) -> RegistrySpec:
        """Resolve one exact or canonical patterned key."""
        exact = self._by_key.get(key)
        if exact is not None:
            return exact
        matches = [pattern for pattern in self.pattern_specs if pattern.match(key) is not None]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise RegistryError(f"Ambiguous configuration key: {key}")
        raise UnknownConfigKeyError(key)

    def dynamic_segment_follows(self, prefix: tuple[str, ...]) -> bool:
        """Return True when any pattern puts a dynamic segment right after prefix."""
        for pattern in self.pattern_specs:
            parts = pattern.pattern.split(".")
            if len(parts) <= len(prefix):
                continue
            if not _pattern_prefix_matches(parts, prefix):
                continue
            next_part = parts[len(prefix)]
            if next_part.startswith("{") and next_part.endswith("}"):
                return True
        return False

    def for_visibility(self, visibility: ConfigVisibility) -> tuple[RegistrySpec, ...]:
        return tuple(spec for spec in self.specs if spec.visibility is visibility)

    def json_schema(self, visibility: ConfigVisibility) -> dict[str, object]:
        """Return deterministic JSON Schema metadata for one exposure surface."""
        if visibility is ConfigVisibility.MACHINE:
            selected = tuple(
                spec
                for spec in self.specs
                if spec.visibility is ConfigVisibility.MACHINE or spec.machine_export
            )
        else:
            selected = self.for_visibility(visibility)
        properties = {
            spec.key: _schema_metadata(spec)
            for spec in sorted(selected, key=lambda candidate: candidate.key)
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }


def _pattern_prefix_matches(pattern_parts: list[str], prefix: tuple[str, ...]) -> bool:
    for index, segment in enumerate(prefix):
        part = pattern_parts[index]
        if part.startswith("{") and part.endswith("}"):
            try:
                decode_dynamic_segment(segment)
            except ValueError:
                return False
        elif part != segment:
            return False
    return True


def config_key_secrecy(spec: RegistrySpec, key: str) -> ConfigSecrecy:
    """Return effective secrecy for an exact key or one patterned field."""
    if not isinstance(spec, ConfigPatternSpec):
        return spec.secrecy
    if spec.secrecy is not ConfigSecrecy.NONE:
        return spec.secrecy
    matched = spec.match(key)
    field_name = matched.get("field") if matched else None
    field = next((item for item in spec.field_specs if item.name == field_name), None)
    return ConfigSecrecy.NONE if field is None else field.secrecy


def _spec_carries_secrets(spec: RegistrySpec) -> bool:
    """True when any value the spec can address has non-NONE secrecy."""
    if spec.secrecy is not ConfigSecrecy.NONE:
        return True
    return any(field.secrecy is not ConfigSecrecy.NONE for field in spec.field_specs)


def config_reference_fields(spec: RegistrySpec) -> tuple[ConfigFieldSpec, ...]:
    """Return structured fields that must contain secret references."""
    return tuple(field for field in spec.field_specs if field.secrecy is ConfigSecrecy.REFERENCE)


def config_structured_reference_fields(spec: RegistrySpec) -> tuple[ConfigFieldSpec, ...]:
    """Return secret-reference fields only for exact list-valued keys."""
    if not isinstance(spec, ConfigKeySpec) or get_origin(spec.annotation) is not list:
        return ()
    return config_reference_fields(spec)


def config_structured_identity_field(spec: RegistrySpec) -> ConfigFieldSpec:
    """Return the sole registry-declared identity for a structured secret list."""
    identities = tuple(field for field in spec.field_specs if field.identity)
    if len(identities) != 1:
        raise RegistryError(
            f"Structured secret-bearing key requires exactly one identity field: {spec.key}"
        )
    return identities[0]


def _patterns_overlap(left: ConfigPatternSpec, right: ConfigPatternSpec) -> bool:
    left_parts = left.pattern.split(".")
    right_parts = right.pattern.split(".")
    if len(left_parts) != len(right_parts):
        return False
    for left_part, right_part in zip(left_parts, right_parts, strict=True):
        left_dynamic = left_part.startswith("{") and left_part.endswith("}")
        right_dynamic = right_part.startswith("{") and right_part.endswith("}")
        if not left_dynamic and not right_dynamic and left_part != right_part:
            return False
        if (
            left_dynamic
            and not right_dynamic
            and not _placeholder_accepts(left, left_part, right_part)
        ):
            return False
        if (
            right_dynamic
            and not left_dynamic
            and not _placeholder_accepts(right, right_part, left_part)
        ):
            return False
    return True


def _placeholder_accepts(spec: ConfigPatternSpec, placeholder: str, value: str) -> bool:
    if placeholder != "{field}" or not spec.field_specs:
        return True
    return value in {field.name for field in spec.field_specs}


def _schema_metadata(spec: RegistrySpec) -> dict[str, object]:
    metadata: dict[str, object] = {
        "type": _json_type(spec.annotation),
        "namespace": spec.namespace,
        "secrecy": spec.secrecy.value,
        "visibility": spec.visibility.value,
        "activation": spec.activation.value,
        "machineExport": spec.machine_export,
    }
    if spec.has_default:
        metadata["default"] = _json_default(spec.default)
    if spec.description:
        metadata["description"] = spec.description
    if isinstance(spec, ConfigPatternSpec):
        metadata["pattern"] = spec.pattern
    if spec.field_specs:
        metadata["fields"] = {
            field.name: {
                "type": _json_type(field.annotation),
                "secrecy": field.secrecy.value,
                "identity": field.identity,
                **({"default": _json_default(field.default)} if field.has_default else {}),
            }
            for field in spec.field_specs
        }
    return metadata


def _json_type(annotation: object) -> str | list[str]:
    origin = get_origin(annotation)
    if origin is Literal:
        values = get_args(annotation)
        return _json_type(type(values[0])) if values else "string"
    if origin is not None and origin not in {dict, list, tuple, set, frozenset, Mapping}:
        choices = [_json_type(choice) for choice in get_args(annotation)]
        flattened = [
            item for choice in choices for item in ([choice] if isinstance(choice, str) else choice)
        ]
        return list(dict.fromkeys(flattened))
    if annotation is type(None):
        return "null"
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is str or (isinstance(annotation, type) and issubclass(annotation, Enum)):
        return "string"
    if origin in {list, tuple, set, frozenset}:
        return "array"
    if origin in {dict, Mapping} or _model_type(annotation) is not None:
        return "object"
    return "string"


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return {str(key): _json_default(child) for key, child in value}
        return [_json_default(child) for child in value]
    if isinstance(value, Mapping):
        return {str(key): _json_default(child) for key, child in value.items()}
    if isinstance(value, frozenset):
        return sorted((_json_default(child) for child in value), key=repr)
    return to_jsonable_python(value)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(child)) for key, child in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(child) for child in value)
    return value


def _model_type(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in {Union, UnionType}:
        for argument in get_args(annotation):
            if isinstance(argument, type) and issubclass(argument, BaseModel):
                return argument
    return None


def _field_default(field: FieldInfo, instance_value: object = _MISSING) -> object:
    if instance_value is not _MISSING:
        return _freeze(instance_value)
    default = field.get_default(call_default_factory=True)
    return _MISSING if default is PydanticUndefined else _freeze(default)


def _field_specs(annotation: object) -> tuple[ConfigFieldSpec, ...]:
    model = _model_type(annotation)
    if model is None:
        return ()
    return tuple(
        ConfigFieldSpec(
            name=name,
            annotation=field.annotation,
            default=_field_default(field),
            secrecy=_secrecy(name),
            identity=(
                isinstance(field.json_schema_extra, dict)
                and field.json_schema_extra.get("x-config-identity") is True
            ),
        )
        for name, field in model.model_fields.items()
    )


def _list_field_specs(annotation: object) -> tuple[ConfigFieldSpec, ...]:
    if get_origin(annotation) is not list:
        return ()
    arguments = get_args(annotation)
    return _field_specs(arguments[0]) if arguments else ()


@dataclass(frozen=True, slots=True)
class _Leaf:
    source_path: str
    annotation: object
    default: object
    description: str | None


def _walk_daemon_model() -> tuple[tuple[_Leaf, ...], Mapping[str, _Leaf]]:
    leaves: list[_Leaf] = []
    mappings: dict[str, _Leaf] = {}
    root = DaemonConfig()

    def walk(
        model: type[BaseModel],
        instance: BaseModel | None,
        prefix: str,
        ancestors: tuple[type[BaseModel], ...],
    ) -> None:
        if model in ancestors:
            return
        for name, field in model.model_fields.items():
            source_path = f"{prefix}.{name}" if prefix else name
            value = getattr(instance, name, _MISSING) if instance is not None else _MISSING
            if get_origin(field.annotation) in {dict, Mapping}:
                mappings[source_path] = _Leaf(
                    source_path,
                    field.annotation,
                    _field_default(field, value),
                    field.description,
                )
                continue
            nested = _model_type(field.annotation)
            if nested is not None:
                nested_instance = value if isinstance(value, BaseModel) else None
                walk(nested, nested_instance, source_path, (*ancestors, model))
                continue
            leaves.append(
                _Leaf(
                    source_path,
                    field.annotation,
                    _field_default(field, value),
                    field.description,
                )
            )

    walk(DaemonConfig, root, "", ())
    return tuple(leaves), MappingProxyType(mappings)


def _flatten_mapping(value: Mapping[str, object], prefix: str = "") -> Iterator[str]:
    for name, child in value.items():
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(child, Mapping):
            yield from _flatten_mapping(child, path)
        else:
            yield path


BOOTSTRAP_RUNTIME_PATHS = frozenset(_flatten_mapping(BootstrapConfig().to_config_dict()))
_REMOVED_RUNTIME_PATHS = frozenset({"hub_backend", "postgres_pool"})

_RESTART_PATHS = frozenset({"cors_origins", "test_mode", "memory.backend"})
_RESTART_PREFIXES = (
    "database_concurrency.",
    "databases.",
    "telemetry.",
)
_RESTART_NESTED_PATHS = frozenset({"websocket.enabled", "ui.enabled", "ui.mode", "ui.web_dir"})
_MANAGED_EMBEDDING_PATHS = frozenset(
    runtime_embedding_key(field)
    for field in (
        EMBEDDING_MODEL_FIELD,
        EMBEDDING_API_BASE_FIELD,
        EMBEDDING_DIM_FIELD,
        EMBEDDING_QUERY_PREFIX_FIELD,
        EMBEDDING_CATALOG_KEY_FIELD,
    )
)
_SECRET_REFERENCE_PATHS = frozenset({"databases.falkordb.password"})
_MACHINE_EXPORT_KEYS = frozenset(
    {
        AI_EMBEDDING_MODEL_KEY,
        AI_EMBEDDING_DIM_KEY,
        AI_EMBEDDING_QUERY_PREFIX_KEY,
        "databases.falkordb.host",
        "databases.falkordb.port",
        "databases.qdrant.url",
        "indexing.respect_gitignore",
    }
)

_MAPPING_PATTERNS = MappingProxyType(
    {
        "telemetry.exporter.otlp_headers": "telemetry.exporter.otlp_headers.{header}",
        "mcp_client_proxy.tool_timeouts": "mcp_client_proxy.tool_timeouts.{tool}",
        "gobby_tasks.expansion.pattern_criteria.patterns": (
            "gobby_tasks.expansion.pattern_criteria.patterns.{pattern}"
        ),
        "gobby_tasks.expansion.pattern_criteria.detection_keywords": (
            "gobby_tasks.expansion.pattern_criteria.detection_keywords.{pattern}"
        ),
        "ai.generation.endpoints": "ai.generation.endpoints.{endpoint}.{field}",
        "ai.generation.profile_defaults": "ai.generation.profile_defaults.{profile}",
        "skills.hubs": "skills.hubs.{hub}.{field}",
        "verification_defaults.custom": "verification_defaults.custom.{command}",
        "context_window_overrides": "context_window_overrides.{model_match}",
        "hooks.additional_context_limits": "hooks.additional_context_limits.{provider}",
        "wiki.codewiki_project_scopes_by_name": (
            "wiki.codewiki_project_scopes_by_name.{project_name}"
        ),
    }
)


def _canonical_key(source_path: str) -> str:
    if source_path == RUNTIME_EMBEDDINGS_CONFIG_PREFIX:
        return AI_EMBEDDINGS_CONFIG_PREFIX
    if source_path.startswith(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}."):
        field = source_path.removeprefix(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.")
        return f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{field}"
    return source_path


def _activation(source_path: str) -> ActivationPolicy:
    if source_path in _MANAGED_EMBEDDING_PATHS:
        return ActivationPolicy.MANAGED
    if (
        source_path in _RESTART_PATHS
        or source_path in _RESTART_NESTED_PATHS
        or source_path.startswith(_RESTART_PREFIXES)
    ):
        return ActivationPolicy.RESTART_REQUIRED
    return ActivationPolicy.LIVE


def _secrecy(source_path: str) -> ConfigSecrecy:
    name = source_path.rsplit(".", 1)[-1]
    if source_path in _SECRET_REFERENCE_PATHS or "api_key" in name:
        return ConfigSecrecy.REFERENCE
    return ConfigSecrecy.NONE


def _pattern_from_mapping(leaf: _Leaf, pattern: str) -> ConfigPatternSpec:
    arguments = get_args(leaf.annotation)
    value_annotation = arguments[1] if len(arguments) == 2 else object
    return ConfigPatternSpec(
        pattern=pattern,
        annotation=value_annotation,
        default=leaf.default,
        source_path=leaf.source_path,
        description=leaf.description,
        field_specs=_field_specs(value_annotation),
    )


def _supplemental_key_specs() -> tuple[ConfigKeySpec, ...]:
    ui_fields: tuple[tuple[str, object], ...] = (
        ("fontSize", int | None),
        ("model", str | None),
        ("theme", str | None),
        ("defaultChatMode", str | None),
        ("sttEnabled", bool | None),
        ("ttsEnabled", bool | None),
        ("voiceInputMode", str | None),
        ("planPendingVariant", str | None),
        ("selectedProjectId", str | None),
        ("selectedProvider", str | None),
    )
    public = tuple(
        ConfigKeySpec(f"ui_settings.{name}", annotation, None) for name, annotation in ui_fields
    ) + (
        ConfigKeySpec("rules.enforcement_enabled", bool, True),
        ConfigKeySpec("rules.aggregate_blocks", bool, True),
        ConfigKeySpec("tool_approvals.global_rules", list[str], ()),
    )
    machine = (
        ConfigKeySpec(
            "ai.embeddings.routing",
            str,
            "daemon",
            visibility=ConfigVisibility.MACHINE,
            machine_export=True,
        ),
        ConfigKeySpec(
            "ai.embeddings.timeout_seconds",
            float,
            visibility=ConfigVisibility.MACHINE,
            machine_export=True,
        ),
    )
    restricted = tuple(
        ConfigKeySpec(
            key,
            annotation,
            visibility=ConfigVisibility.RESTRICTED,
            secrecy=ConfigSecrecy.PAYLOAD,
        )
        for key, annotation in (
            ("auth.api_token_hash", str),
            (EMBEDDING_SWITCH_JOURNAL_KEY, dict[str, object]),
            (EMBEDDING_SWITCH_COMPLETED_KEY, dict[str, object]),
        )
    ) + (
        # Daemon-owned one-shot marker, not a secret payload; read at startup only.
        ConfigKeySpec(
            MCP_SCOPED_PAYLOAD_VERSION_KEY,
            int,
            activation=ActivationPolicy.RESTART_REQUIRED,
            visibility=ConfigVisibility.RESTRICTED,
        ),
    )
    return (*public, *machine, *restricted)


def _build_registry() -> ConfigRegistry:
    leaves, mappings = _walk_daemon_model()
    unclassified = set(mappings).symmetric_difference(_MAPPING_PATTERNS)
    if unclassified:
        raise RegistryError(f"Mapping leaves require explicit adapters: {sorted(unclassified)}")

    key_specs = tuple(
        ConfigKeySpec(
            key=_canonical_key(leaf.source_path),
            annotation=leaf.annotation,
            default=leaf.default,
            source_path=leaf.source_path,
            activation=_activation(leaf.source_path),
            secrecy=_secrecy(leaf.source_path),
            machine_export=_canonical_key(leaf.source_path) in _MACHINE_EXPORT_KEYS,
            description=leaf.description,
            field_specs=_list_field_specs(leaf.annotation),
        )
        for leaf in leaves
        if leaf.source_path not in BOOTSTRAP_RUNTIME_PATHS
        and leaf.source_path not in _REMOVED_RUNTIME_PATHS
    )
    pattern_specs = tuple(
        _pattern_from_mapping(mappings[source_path], pattern)
        for source_path, pattern in _MAPPING_PATTERNS.items()
    ) + (
        ConfigPatternSpec(
            "launch_defaults.{project_id}",
            dict[str, object],
            default=(),
            description="Per-project native launch defaults.",
        ),
    )
    return ConfigRegistry(
        (*key_specs, *_supplemental_key_specs()),
        pattern_specs,
        bootstrap_paths=BOOTSTRAP_RUNTIME_PATHS,
    )


CONFIG_REGISTRY = _build_registry()


__all__ = [
    "BOOTSTRAP_RUNTIME_PATHS",
    "CONFIG_REGISTRY",
    "DYNAMIC_SEGMENT_CODEC_VECTORS",
    "INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS",
    "INVALID_DYNAMIC_SEGMENTS",
    "ActivationPolicy",
    "ConfigKeySpec",
    "ConfigPatternSpec",
    "ConfigRegistry",
    "ConfigSecrecy",
    "ConfigVisibility",
    "ConfigFieldSpec",
    "RegistryError",
    "UnknownConfigKeyError",
    "config_reference_fields",
    "decode_dynamic_segment",
    "config_key_secrecy",
    "encode_dynamic_segment",
]
