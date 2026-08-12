"""Acceptance tests for the typed runtime configuration registry."""

from collections.abc import Iterator, Mapping
from dataclasses import fields
from types import UnionType
from typing import Union, cast, get_args, get_origin

import pytest
from pydantic import BaseModel

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.registry import (
    BOOTSTRAP_RUNTIME_PATHS,
    CONFIG_REGISTRY,
    DYNAMIC_SEGMENT_CODEC_VECTORS,
    INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS,
    INVALID_DYNAMIC_SEGMENTS,
    ActivationPolicy,
    ConfigPatternSpec,
    ConfigSecrecy,
    ConfigVisibility,
    UnknownConfigKeyError,
    config_structured_identity_field,
    decode_dynamic_segment,
    encode_dynamic_segment,
)

pytestmark = pytest.mark.unit


def _model_type(annotation: object) -> type[BaseModel] | None:
    """Return a nested Pydantic model type from a field annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in {Union, UnionType}:
        for argument in get_args(annotation):
            if isinstance(argument, type) and issubclass(argument, BaseModel):
                return argument
    return None


def _is_mapping(annotation: object) -> bool:
    return get_origin(annotation) in {dict, Mapping}


def _walk_model(
    model: type[BaseModel], prefix: str = "", ancestors: tuple[type[BaseModel], ...] = ()
) -> Iterator[tuple[str, bool]]:
    """Yield each daemon leaf and whether it is a mapping boundary."""
    if model in ancestors:
        return
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        if _is_mapping(field.annotation):
            yield path, True
            continue
        nested_model = _model_type(field.annotation)
        if nested_model is None:
            yield path, False
            continue
        yield from _walk_model(nested_model, path, (*ancestors, model))


def _flatten_mapping(value: Mapping[str, object], prefix: str = "") -> set[str]:
    leaves: set[str] = set()
    for name, child in value.items():
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(child, Mapping):
            leaves.update(_flatten_mapping(child, path))
        else:
            leaves.add(path)
    return leaves


def _canonical_key(source_path: str) -> str:
    if source_path == "embeddings":
        return "ai.embeddings"
    if source_path.startswith("embeddings."):
        return f"ai.{source_path}"
    return source_path


def test_every_daemon_leaf_has_one_spec() -> None:
    bootstrap_paths = _flatten_mapping(BootstrapConfig().to_config_dict())
    daemon_leaves = {
        path
        for path, is_mapping in _walk_model(DaemonConfig)
        if not is_mapping
        and path not in bootstrap_paths
        and path not in {"hub_backend", "postgres_pool"}
    }

    registered_by_source: dict[str, list[str]] = {}
    for spec in CONFIG_REGISTRY.key_specs:
        if spec.source_path is not None:
            registered_by_source.setdefault(spec.source_path, []).append(spec.key)

    assert set(registered_by_source) == daemon_leaves
    for source_path in daemon_leaves:
        assert registered_by_source[source_path] == [_canonical_key(source_path)]
        assert CONFIG_REGISTRY.resolve(_canonical_key(source_path)).source_path == source_path


def test_mapping_patterns_are_complete() -> None:
    daemon_mappings = {path for path, is_mapping in _walk_model(DaemonConfig) if is_mapping}
    registered_mappings = {
        pattern.source_path
        for pattern in CONFIG_REGISTRY.pattern_specs
        if pattern.source_path is not None
    }

    assert registered_mappings == daemon_mappings
    assert {
        "ai.generation.endpoints.{endpoint}.{field}",
        "ai.generation.profile_defaults.{profile}",
        "mcp_client_proxy.tool_timeouts.{tool}",
        "gobby_tasks.expansion.pattern_criteria.patterns.{pattern}",
        "gobby_tasks.expansion.pattern_criteria.detection_keywords.{pattern}",
        "verification_defaults.custom.{command}",
        "skills.hubs.{hub}.{field}",
        "context_window_overrides.{model_match}",
        "wiki.codewiki_project_scopes_by_name.{project_name}",
        "launch_defaults.{project_id}",
    }.issubset({pattern.pattern for pattern in CONFIG_REGISTRY.pattern_specs})


def test_visibility_partitions_are_disjoint() -> None:
    partitions = {
        visibility: set(CONFIG_REGISTRY.for_visibility(visibility))
        for visibility in ConfigVisibility
    }

    for visibility, specs in partitions.items():
        assert specs
        assert all(spec.visibility is visibility for spec in specs)
        assert specs.isdisjoint(
            set().union(*(other for key, other in partitions.items() if key is not visibility))
        )

    assert set().union(*partitions.values()) == set(CONFIG_REGISTRY.specs)
    assert set(ActivationPolicy) == {spec.activation for spec in CONFIG_REGISTRY.specs}

    public_schema = CONFIG_REGISTRY.json_schema(ConfigVisibility.PUBLIC)
    machine_schema = CONFIG_REGISTRY.json_schema(ConfigVisibility.MACHINE)
    restricted_schema = CONFIG_REGISTRY.json_schema(ConfigVisibility.RESTRICTED)
    assert public_schema["properties"]
    assert machine_schema["properties"]
    assert restricted_schema["properties"]
    for schema in (public_schema, machine_schema, restricted_schema):
        properties = cast(Mapping[str, Mapping[str, object]], schema["properties"])
        for metadata in properties.values():
            assert {
                "namespace",
                "secrecy",
                "visibility",
                "activation",
                "machineExport",
            }.issubset(metadata)


def test_structured_voice_api_key_secrecy_is_registry_owned() -> None:
    spec = CONFIG_REGISTRY.resolve("voice.openai_compatible_audio")

    assert config_structured_identity_field(spec).name == "provider"
    assert {field.name: field.secrecy for field in spec.field_specs}["api_key"] is (
        ConfigSecrecy.REFERENCE
    )
    properties = cast(
        Mapping[str, object],
        CONFIG_REGISTRY.json_schema(ConfigVisibility.PUBLIC)["properties"],
    )
    metadata = cast(Mapping[str, object], properties["voice.openai_compatible_audio"])
    fields = cast(Mapping[str, object], metadata["fields"])
    api_key = cast(Mapping[str, object], fields["api_key"])
    assert api_key["secrecy"] == "reference"
    provider = cast(Mapping[str, object], fields["provider"])
    assert provider["identity"] is True


def test_postgres_pool_is_bootstrap_only() -> None:
    with pytest.raises(UnknownConfigKeyError):
        CONFIG_REGISTRY.resolve("postgres_pool")


def test_bootstrap_field_names_never_enter_runtime_registry() -> None:
    bootstrap_field_names = {field.name for field in fields(BootstrapConfig)}
    runtime_keys = {spec.key for spec in CONFIG_REGISTRY.key_specs}

    assert bootstrap_field_names.isdisjoint(runtime_keys)


def _pattern_segments(pattern: ConfigPatternSpec) -> dict[str, str]:
    return dict(pattern.example_segments)


def test_dynamic_segment_codec_round_trip() -> None:
    for decoded, encoded in DYNAMIC_SEGMENT_CODEC_VECTORS:
        assert encode_dynamic_segment(decoded) == encoded
        assert decode_dynamic_segment(encoded) == decoded

    for malformed in INVALID_DYNAMIC_SEGMENTS:
        with pytest.raises(ValueError):
            decode_dynamic_segment(malformed)

    for pattern in CONFIG_REGISTRY.pattern_specs:
        for decoded, _encoded in DYNAMIC_SEGMENT_CODEC_VECTORS:
            segments = _pattern_segments(pattern)
            segments[pattern.dynamic_placeholders[0]] = decoded
            key = pattern.format(**segments)

            assert "." not in key.split(".")[pattern.dynamic_segment_indexes[0]]
            assert pattern.match(key) == segments
            assert CONFIG_REGISTRY.resolve(key) is pattern


def test_bootstrap_runtime_paths_match_projection_and_registry_exclusion() -> None:
    expected = frozenset(_flatten_mapping(BootstrapConfig().to_config_dict()))
    registry_source_paths = {
        spec.source_path for spec in CONFIG_REGISTRY.specs if spec.source_path is not None
    }

    assert BOOTSTRAP_RUNTIME_PATHS == expected
    assert BOOTSTRAP_RUNTIME_PATHS.isdisjoint(registry_source_paths)


@pytest.mark.parametrize("value", ["", *INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS])
def test_dynamic_segment_codec_rejects_unencodable_input(value: str) -> None:
    with pytest.raises(ValueError):
        encode_dynamic_segment(value)


def test_machine_exported_secret_specs_are_rejected_at_registry_load() -> None:
    """The registry fails closed if a secret-bearing key enters the machine export."""
    from gobby.config.registry import ConfigFieldSpec, ConfigKeySpec, ConfigRegistry, RegistryError

    exported_secret = ConfigKeySpec(
        key="databases.falkordb.password",
        annotation=str | None,
        default=None,
        secrecy=ConfigSecrecy.REFERENCE,
        machine_export=True,
    )
    with pytest.raises(RegistryError, match="cannot be machine-exported"):
        ConfigRegistry((exported_secret,), ())

    machine_visible_secret = ConfigKeySpec(
        key="databases.falkordb.password",
        annotation=str | None,
        default=None,
        secrecy=ConfigSecrecy.REFERENCE,
        visibility=ConfigVisibility.MACHINE,
    )
    with pytest.raises(RegistryError, match="cannot be machine-exported"):
        ConfigRegistry((machine_visible_secret,), ())

    exported_secret_field = ConfigPatternSpec(
        pattern="ai.generation.endpoints.{endpoint}.{field}",
        annotation=str | None,
        default=None,
        machine_export=True,
        field_specs=(
            ConfigFieldSpec(
                name="api_key",
                annotation=str | None,
                default=None,
                secrecy=ConfigSecrecy.REFERENCE,
            ),
        ),
    )
    with pytest.raises(RegistryError, match="cannot be machine-exported"):
        ConfigRegistry((), (exported_secret_field,))


def test_no_machine_exported_spec_carries_secrets() -> None:
    """Live-registry regression guard for the plaintext-egress policy."""
    for spec in CONFIG_REGISTRY.specs:
        if spec.machine_export or spec.visibility is ConfigVisibility.MACHINE:
            assert spec.secrecy is ConfigSecrecy.NONE, spec.key
            assert all(field.secrecy is ConfigSecrecy.NONE for field in spec.field_specs), spec.key
