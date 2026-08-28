"""Prepared service adapters and live configuration consumer coverage."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_CONFIG_KEY_SET,
)
from gobby.config.registry import CONFIG_REGISTRY, ActivationPolicy, ConfigRegistry
from gobby.config.runtime_models import ConfigChange

_T = TypeVar("_T")


class RegistrySpecLike(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def activation(self) -> ActivationPolicy: ...


class UnmappedLiveConfigKeysError(ValueError):
    """Raised when a live registry key has no declared production consumer."""


class CanonicalConfigKeySet(frozenset[str]):
    """Subscriber keys that also match concrete members of registry patterns."""

    _registry: ConfigRegistry

    def __new__(
        cls,
        values: Iterable[str] = (),
        *,
        registry: ConfigRegistry = CONFIG_REGISTRY,
    ) -> CanonicalConfigKeySet:
        instance = super().__new__(cls, values)
        instance._registry = registry
        return instance

    def intersection(self, *others: Iterable[object]) -> frozenset[str]:
        candidates = super().intersection(*others)
        if candidates:
            return candidates
        matched: set[str] = set()
        for other in others:
            for concrete_key in other:
                if not isinstance(concrete_key, str):
                    continue
                try:
                    canonical_key = self._registry.resolve(concrete_key).key
                except (KeyError, ValueError):
                    canonical_key = concrete_key
                if canonical_key in self:
                    matched.add(concrete_key)
        return frozenset(matched)


@dataclass(slots=True)
class PreparedService:
    """A prepared value plus the cleanup owned by its runtime bundle handle."""

    value: object
    _dispose: Callable[[], None] = lambda: None
    _activate: Callable[[], None] = lambda: None

    def activate(self) -> None:
        self._activate()

    def dispose(self) -> None:
        self._dispose()


@dataclass(slots=True)
class ServiceSubscriber:
    """Focused prepared-service adapter consumed by ``ConfigRuntime``."""

    name: str
    keys: frozenset[str]
    builder: Callable[[ConfigChange], object]
    required: bool = False
    prepare_timeout: float = 5.0
    dispose_timeout: float = 5.0

    def __init__(
        self,
        *,
        name: str,
        keys: Iterable[str],
        builder: Callable[[ConfigChange], object],
        required: bool = False,
        prepare_timeout: float = 5.0,
        dispose_timeout: float = 5.0,
    ) -> None:
        if not name:
            raise ValueError("Configuration subscriber name cannot be empty")
        if prepare_timeout <= 0 or dispose_timeout <= 0:
            raise ValueError("Configuration subscriber deadlines must be positive")
        self.name = name
        self.keys = CanonicalConfigKeySet(keys)
        self.builder = builder
        self.required = required
        self.prepare_timeout = prepare_timeout
        self.dispose_timeout = dispose_timeout

    def prepare(self, change: ConfigChange) -> PreparedService:
        prepared = self.builder(change)
        if isinstance(prepared, PreparedService):
            return prepared
        return PreparedService(prepared)


@dataclass(frozen=True, slots=True)
class LiveConsumerMatrixEntry:
    """One registry key's production consumer and activation path."""

    registry_key: str
    consumers: tuple[str, ...]
    capture_points: tuple[str, ...]
    access_path: str
    subscribers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ConsumerRoute:
    consumers: tuple[str, ...]
    capture_points: tuple[str, ...]
    access_path: str
    subscribers: tuple[str, ...] = ()


def _route(
    consumer: str,
    capture_point: str,
    *,
    subscriber: str | None = None,
    subscribers: tuple[str, ...] = (),
) -> _ConsumerRoute:
    subscriber_names = subscribers or ((subscriber,) if subscriber else ())
    return _ConsumerRoute(
        consumers=(consumer,),
        capture_points=(capture_point,),
        access_path="subscriber" if subscriber_names else "per_operation",
        subscribers=subscriber_names,
    )


_STATEFUL_ROUTES: Mapping[str, _ConsumerRoute] = {
    "ai": _route(
        "daemon AI/model clients",
        "runner_init.services._build_ai_services",
        subscribers=("ai_services", "memory_services", "task_validator"),
    ),
    "chat": _route(
        "HTTP and WebSocket chat limits",
        "servers.http.HTTPServer.capture_runtime_bundle",
        subscriber="chat_config",
    ),
    "code_index": _route(
        "CodeIndexContext",
        "runner_init.services._build_code_index",
        subscriber="code_index",
    ),
    "knowledge_graph_queue": _route(
        "MemoryManager",
        "runner_init.services._build_memory_services",
        subscriber="memory_services",
    ),
    "mcp_client_proxy": _route(
        "HTTP MCP proxy operations",
        "servers.http.HTTPServer.capture_runtime_bundle",
        subscriber="mcp_proxy_config",
    ),
    "memory": _route(
        "MemoryManager",
        "runner_init.services._build_memory_services",
        subscriber="memory_services",
    ),
    "memory_backup": _route(
        "MemoryBackupManager",
        "runner_init.services._build_memory_services",
        subscriber="memory_services",
    ),
    "message_tracking": _route(
        "SessionMessageProcessor",
        "runner_init.services._build_message_processor",
        subscriber="message_processor",
    ),
}

_PREFIX_ROUTES: tuple[tuple[str, _ConsumerRoute], ...] = (
    (
        "gobby_tasks.validation.",
        _route(
            "TaskValidator",
            "runner_init.services._build_task_validator",
            subscriber="task_validator",
        ),
    ),
    (
        "logging.dir",
        _route(
            "MCPClientManager",
            "runner_init.services._build_mcp_manager",
            subscriber="mcp_manager",
        ),
    ),
)

_PER_OPERATION_ROUTES: Mapping[str, _ConsumerRoute] = {
    "agent_sandbox": _route("agent launch", "operation ConfigRuntime.capture"),
    "auth": _route("authentication routes", "request ConfigRuntime.capture"),
    "bin_freshness": _route("binary freshness loop", "iteration ConfigRuntime.capture"),
    "chat_history": _route("chat history", "request ConfigRuntime.capture"),
    "clones_dir": _route("clone operations", "operation ConfigRuntime.capture"),
    "communications": _route("communications tools", "operation ConfigRuntime.capture"),
    "compact_handoff": _route("session compaction", "operation ConfigRuntime.capture"),
    "context_window_overrides": _route("session routes", "request ConfigRuntime.capture"),
    "cron": _route("cron scheduler", "iteration ConfigRuntime.capture"),
    "daemon_health_check_interval": _route("health loop", "iteration ConfigRuntime.capture"),
    "digest": _route("memory digest", "operation ConfigRuntime.capture"),
    "gobby_tasks": _route("task tools and validation", "operation ConfigRuntime.capture"),
    "hook_extensions": _route("hook extension resolution", "operation ConfigRuntime.capture"),
    "hooks": _route("hook execution", "operation ConfigRuntime.capture"),
    "import_mcp_server": _route("MCP import", "request ConfigRuntime.capture"),
    "indexing": _route("memory indexing", "operation ConfigRuntime.capture"),
    "launch_defaults": _route("agent launch", "operation ConfigRuntime.capture"),
    "logging": _route("logging operations", "operation ConfigRuntime.capture"),
    "memory_usefulness": _route("memory usefulness", "operation ConfigRuntime.capture"),
    "merge_resolution": _route("merge resolution", "operation ConfigRuntime.capture"),
    "metrics": _route("metrics cleanup", "iteration ConfigRuntime.capture"),
    "pipelines": _route("pipeline execution", "operation ConfigRuntime.capture"),
    "postgres_pool": _route("database acquisition", "operation ConfigRuntime.capture"),
    "project_verification_synthesis": _route(
        "verification synthesis", "operation ConfigRuntime.capture"
    ),
    "recommend_tools": _route("tool recommendation", "request ConfigRuntime.capture"),
    "rules": _route("rule evaluation", "operation ConfigRuntime.capture"),
    "search": _route("memory search", "operation ConfigRuntime.capture"),
    "session_lifecycle": _route("session lifecycle", "operation ConfigRuntime.capture"),
    "session_summary": _route("session summary", "operation ConfigRuntime.capture"),
    "skill_description": _route("skill description", "operation ConfigRuntime.capture"),
    "skills": _route("skill operations", "operation ConfigRuntime.capture"),
    "system_loops": _route("system loops", "iteration ConfigRuntime.capture"),
    "telemetry": _route("telemetry operations", "operation ConfigRuntime.capture"),
    "terminal_host": _route("gterm host supervision", "operation ConfigRuntime.capture"),
    "terminals": _route("terminal runtime", "operation ConfigRuntime.capture"),
    "tmux": _route("terminal operations", "operation ConfigRuntime.capture"),
    "tool_approval": _route("tool approval", "operation ConfigRuntime.capture"),
    "tool_approvals": _route("tool approval", "operation ConfigRuntime.capture"),
    "tool_result_offload": _route("tool result offload", "operation ConfigRuntime.capture"),
    "tool_summarizer": _route("tool summarization", "operation ConfigRuntime.capture"),
    "ui": _route("UI routes", "request ConfigRuntime.capture"),
    "ui_settings": _route("UI settings", "request ConfigRuntime.capture"),
    "validation_detection": _route("validation detection", "operation ConfigRuntime.capture"),
    "verification_defaults": _route("verification", "operation ConfigRuntime.capture"),
    "voice": _route("voice routes", "request ConfigRuntime.capture"),
    "web_chat_sandbox": _route("web chat launch", "operation ConfigRuntime.capture"),
    "websocket": _route("WebSocket operations", "message ConfigRuntime.capture"),
    "wiki": _route("wiki operations", "operation ConfigRuntime.capture"),
    "workflow": _route("workflow execution", "operation ConfigRuntime.capture"),
    "worktrees_dir": _route("worktree operations", "operation ConfigRuntime.capture"),
}


def live_consumer_matrix(
    specs: Iterable[RegistrySpecLike] | None = None,
) -> tuple[LiveConsumerMatrixEntry, ...]:
    """Enumerate every live registry key and reject undeclared consumer families."""
    entries: list[LiveConsumerMatrixEntry] = []
    unmapped: list[str] = []
    source = CONFIG_REGISTRY.specs if specs is None else specs
    for spec in source:
        if spec.activation is not ActivationPolicy.LIVE:
            continue
        root = spec.key.partition(".")[0]
        route = next(
            (candidate for prefix, candidate in _PREFIX_ROUTES if spec.key.startswith(prefix)),
            None,
        )
        route = route or _STATEFUL_ROUTES.get(root) or _PER_OPERATION_ROUTES.get(root)
        if route is None:
            unmapped.append(spec.key)
            continue
        entries.append(
            LiveConsumerMatrixEntry(
                registry_key=spec.key,
                consumers=route.consumers,
                capture_points=route.capture_points,
                access_path=route.access_path,
                subscribers=route.subscribers,
            )
        )
    if unmapped:
        joined = ", ".join(sorted(unmapped))
        raise UnmappedLiveConfigKeysError(f"Live configuration keys lack consumers: {joined}")
    return tuple(sorted(entries, key=lambda entry: entry.registry_key))


def live_subscriber_keys(name: str) -> frozenset[str]:
    """Return canonical live registry keys consumed by one subscriber."""
    keys = frozenset(
        entry.registry_key for entry in live_consumer_matrix() if name in entry.subscribers
    )
    if name in _STATEFUL_ROUTES["ai"].subscribers:
        return keys | (AI_EMBEDDING_CONFIG_KEY_SET - {AI_EMBEDDING_API_KEY_KEY})
    return keys


__all__ = [
    "CanonicalConfigKeySet",
    "LiveConsumerMatrixEntry",
    "PreparedService",
    "ServiceSubscriber",
    "UnmappedLiveConfigKeysError",
    "live_consumer_matrix",
    "live_subscriber_keys",
]
