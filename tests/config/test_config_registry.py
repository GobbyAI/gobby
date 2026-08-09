"""Acceptance tests for the typed runtime configuration registry."""

from collections.abc import Iterator, Mapping
from types import UnionType
from typing import Union, cast, get_args, get_origin

import pytest
from pydantic import BaseModel

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.registry import (
    CONFIG_REGISTRY,
    DYNAMIC_SEGMENT_CODEC_VECTORS,
    INVALID_DYNAMIC_SEGMENTS,
    ActivationPolicy,
    ConfigPatternSpec,
    ConfigVisibility,
    decode_dynamic_segment,
    encode_dynamic_segment,
)


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
        if not is_mapping and path not in bootstrap_paths and path != "hub_backend"
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
