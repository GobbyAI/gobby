"""Tests for Responses endpoint activation policy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.ai import endpoint_activation
from gobby.ai._text_generation_service import image_candidate_eligible
from gobby.ai.endpoint_activation import (
    EndpointActivationError,
    modalities_for_served_model,
    probe_chat_completions_endpoint,
    probe_responses_endpoint,
)
from gobby.ai.registry import AICapability, build_daemon_ai_capability_registry
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.config.documents import ConfigDocumentsService
from gobby.config.runtime import ConfigSnapshot, RuntimeSecretBinding
from gobby.config.secret_mask import MASKED_SECRET
from gobby.config.values import ConfigValuesService
from gobby.llm.base import LLMTextResult
from gobby.runtime_grants.service import _vision_extract_enabled
from gobby.storage.config_mutations import ConfigMutationResult, ConfigPatch
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _endpoint(*, tool_chat: bool = True) -> GenerationEndpointConfig:
    return GenerationEndpointConfig(
        wire_api="responses",
        api_base="https://openrouter.ai/api/v1",
        api_key="super-secret-key",
        model="moonshotai/kimi-k3",
        tool_chat=tool_chat,
    )


def test_thread_provider_identity_is_stable_for_endpoint() -> None:
    endpoint_activation._assert_thread_provider(
        SimpleNamespace(model_provider="gobby_endpoint_openrouter"),
        "openrouter",
        phase="thread resume",
    )

    with pytest.raises(
        EndpointActivationError,
        match="expected 'gobby_endpoint_openrouter'",
    ):
        endpoint_activation._assert_thread_provider(
            SimpleNamespace(model_provider="openai"),
            "openrouter",
            phase="thread resume",
        )


@pytest.mark.asyncio
async def test_core_probe_failure_keeps_endpoint_dark_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_text(_name: str, _endpoint: GenerationEndpointConfig) -> None:
        raise RuntimeError("401 invalid super-secret-key")

    monkeypatch.setattr(endpoint_activation, "_probe_text", fail_text)

    with pytest.raises(EndpointActivationError) as exc_info:
        await probe_responses_endpoint("openrouter", _endpoint(), DaemonConfig())

    assert str(exc_info.value) == (
        "Responses endpoint authentication failed; verify the configured secret"
    )
    assert "super-secret-key" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_vision_only_failure_activates_text_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pass_probe(*_args: object) -> None:
        return None

    async def fail_vision(*_args: object) -> None:
        raise RuntimeError("image input unsupported")

    monkeypatch.setattr(endpoint_activation, "_probe_text", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_json", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_tool_context_and_resume", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_vision", fail_vision)

    result = await probe_responses_endpoint("openrouter", _endpoint(), DaemonConfig())

    assert result.vision_enabled is False
    assert result.endpoint.input_modalities == ["text"]
    assert result.endpoint.probed_json is True
    assert result.endpoint.tool_chat is True


@pytest.mark.asyncio
async def test_activation_timeout_covers_complete_serial_probe_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def delayed_probe(name: str) -> None:
        calls.append(name)
        released = asyncio.Event()
        handle = asyncio.get_running_loop().call_later(0.03, released.set)
        try:
            await released.wait()
        finally:
            handle.cancel()

    async def slow_text(*_args: object) -> None:
        await delayed_probe("text")

    async def slow_json(*_args: object) -> None:
        await delayed_probe("json")

    async def slow_tool(*_args: object) -> None:
        await delayed_probe("tool")

    async def slow_vision(*_args: object) -> None:
        await delayed_probe("vision")

    monkeypatch.setattr(endpoint_activation, "_probe_text", slow_text)
    monkeypatch.setattr(endpoint_activation, "_probe_json", slow_json)
    monkeypatch.setattr(endpoint_activation, "_probe_tool_context_and_resume", slow_tool)
    monkeypatch.setattr(endpoint_activation, "_probe_vision", slow_vision)

    with pytest.raises(EndpointActivationError, match="timed out after 0.08 seconds"):
        await probe_responses_endpoint(
            "openrouter",
            _endpoint(),
            DaemonConfig(ai={"generation": {"timeout_seconds": 0.08}}),
        )

    assert calls == ["text", "json", "tool"]


@pytest.mark.asyncio
async def test_activation_retries_transient_errors_three_times_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("429 rate limited; Retry-After: 120")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await endpoint_activation._retry_activation(operation)

    assert result == "ok"
    assert attempts == 3
    assert delays == [60.0, 60.0]


@pytest.mark.asyncio
async def test_activation_surfaces_terminal_429_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("429 rate limited")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="429 rate limited"):
        await endpoint_activation._retry_activation(operation)

    assert attempts == 3


async def test_activation_does_not_retry_non_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("invalid response contract")

    with pytest.raises(RuntimeError, match="invalid response contract"):
        await endpoint_activation._retry_activation(operation)

    assert calls == 1


def _chat_endpoint(**overrides: object) -> GenerationEndpointConfig:
    payload: dict[str, object] = {
        "protocol": "vllm",
        "wire_api": "chat-completions",
        "api_base": "http://127.0.0.1:8000/v1",
        "model": "vision-vlm",
        "tool_chat": True,
    }
    payload.update(overrides)
    return GenerationEndpointConfig.model_validate(payload)


class _FakeChatAdapter:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        client: object | None = None,
    ) -> None:
        self.headers = headers if headers is not None else {}
        self.client = client
        self.text = "GOBBY_K3_TEXT_OK and more"
        self.vision_text = "I received an image."
        self.json_result: dict[str, object] = {"ok": True}
        self.json_error: BaseException | None = None
        self.vision_error: BaseException | None = None
        self.generate_calls: list[dict[str, object]] = []
        self.json_calls: list[dict[str, object]] = []

    async def generate_text_result(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None,
        reasoning_effort: str | None = None,
        images: list[str] | None = None,
    ) -> LLMTextResult:
        del reasoning_effort
        self.generate_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "model": model,
                "max_tokens": max_tokens,
                "images": list(images) if images is not None else None,
            }
        )
        if images:
            if self.vision_error is not None:
                raise self.vision_error
            return LLMTextResult(text=self.vision_text)
        return LLMTextResult(text=self.text)

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None,
        model: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        allow_fallback: bool = True,
    ) -> dict[str, object]:
        del reasoning_effort
        self.json_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "model": model,
                "max_tokens": max_tokens,
                "allow_fallback": allow_fallback,
            }
        )
        if self.json_error is not None:
            raise self.json_error
        return self.json_result


class _FakeCompletions:
    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        if not self.succeed:
            raise RuntimeError("tool calls unsupported")
        message = SimpleNamespace(
            tool_calls=[SimpleNamespace(function=SimpleNamespace(name="gobby_probe_tool"))],
            content=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_client(*, succeed: bool = True) -> tuple[object, _FakeCompletions]:
    completions = _FakeCompletions(succeed=succeed)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def _snapshot(
    revision: int,
    *,
    desired: DaemonConfig,
    desired_values: Mapping[str, object],
    desired_secrets: Mapping[str, str] | None = None,
) -> ConfigSnapshot:
    bindings = {
        key: RuntimeSecretBinding(f"$secret:{key}", plaintext, f"fingerprint-{key}")
        for key, plaintext in (desired_secrets or {}).items()
    }
    values = dict(desired_values)
    return ConfigSnapshot(
        revision=revision,
        desired=desired,
        active=desired,
        row_revisions=dict.fromkeys(values, revision),
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values=values,
        active_values=values,
        desired_bindings=bindings,
        active_bindings=bindings,
    )


class _ValuesRuntime:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.current = snapshot
        self.reconciled = snapshot

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.current

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        del revision
        self.current = self.reconciled
        return self.current


class _ValuesMutations:
    def __init__(self) -> None:
        self.calls: list[tuple[int, ConfigPatch]] = []

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        self.calls.append((expected_revision, patch))
        return ConfigMutationResult(expected_revision + 1, frozenset(patch.values))


class _DocumentRuntime:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.snapshot = snapshot
        self.reconciled = snapshot

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        del revision
        self.snapshot = self.reconciled
        return self.snapshot


class _DocumentMutations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, ConfigPatch]] = []

    def replace_namespace(
        self,
        *,
        namespace: str,
        expected_revision: int,
        patch: ConfigPatch,
    ) -> ConfigMutationResult:
        self.calls.append((namespace, expected_revision, patch))
        return ConfigMutationResult(expected_revision + 1, frozenset(patch.values))


async def _inline[T](operation: Callable[[], T]) -> T:
    return operation()


_CANDIDATE_REPOSITORY = ConfigRepository(
    cast(Any, object()),
    secret_store=cast(SecretStore, object()),
)


def _probed_chat_config() -> tuple[DaemonConfig, dict[str, object], str]:
    endpoint = _chat_endpoint(
        api_key="$secret:local_key",
        probed_model="vision-vlm",
        input_modalities=["text", "image"],
        probed_json=True,
        probed_tools=True,
    )
    config = DaemonConfig(ai={"generation": {"endpoints": {"local": endpoint}}})
    prefix = "ai.generation.endpoints.local"
    values: dict[str, object] = {
        f"{prefix}.protocol": "vllm",
        f"{prefix}.wire_api": "chat-completions",
        f"{prefix}.api_base": "http://127.0.0.1:8000/v1",
        f"{prefix}.model": "vision-vlm",
        f"{prefix}.api_key": "$secret:local_key",
        f"{prefix}.tool_chat": True,
        f"{prefix}.probed_model": "vision-vlm",
        f"{prefix}.input_modalities": ["text", "image"],
        f"{prefix}.probed_json": True,
        f"{prefix}.probed_tools": True,
    }
    return config, values, prefix


def _evidence_cleared(patch: ConfigPatch, prefix: str) -> bool:
    for field in ("probed_model", "input_modalities", "probed_json", "probed_tools"):
        key = f"{prefix}.{field}"
        if patch.values.get(key) is not None and key not in patch.unset:
            return False
    return True


@pytest.mark.asyncio
async def test_vision_probe_degrades_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FakeChatAdapter()
    adapter.vision_error = RuntimeError("image input unsupported")
    client, _completions = _tool_client()
    adapter.client = client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: adapter,
    )

    async def _resolve(_endpoint: GenerationEndpointConfig) -> str:
        return "vision-vlm"

    monkeypatch.setattr(endpoint_activation, "resolve_vllm_served_model", _resolve)

    result = await probe_chat_completions_endpoint("local", _chat_endpoint(), DaemonConfig())

    assert result.vision_enabled is False
    assert result.endpoint.input_modalities == ["text"]
    assert result.endpoint.probed_model == "vision-vlm"
    assert "image" not in (result.endpoint.input_modalities or [])


@pytest.mark.asyncio
async def test_model_scoped_modalities_mixed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeChatAdapter()
    client, _completions = _tool_client()
    adapter.client = client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: adapter,
    )

    async def _resolve(_endpoint: GenerationEndpointConfig) -> str:
        return "vision-vlm"

    monkeypatch.setattr(endpoint_activation, "resolve_vllm_served_model", _resolve)

    result = await probe_chat_completions_endpoint("local", _chat_endpoint(), DaemonConfig())

    assert result.endpoint.probed_model == "vision-vlm"
    assert modalities_for_served_model(result.endpoint, "vision-vlm") == ["text", "image"]
    assert modalities_for_served_model(result.endpoint, "text-llm") is None
    text_binding = next(
        binding
        for binding in build_daemon_ai_capability_registry(
            DaemonConfig(
                ai={"generation": {"endpoints": {"local": result.endpoint}}},
            ),
            provider_installed=lambda _entry: True,
        )
        .status(AICapability.TEXT_GENERATE)
        .bindings
        if binding.provider == "endpoint:local"
    )
    assert image_candidate_eligible(text_binding, model="vision-vlm") is True
    assert image_candidate_eligible(text_binding, model="text-llm") is False


@pytest.mark.asyncio
async def test_identity_change_invalidates_modalities() -> None:
    desired, stored, prefix = _probed_chat_config()
    snapshot = _snapshot(
        4,
        desired=desired,
        desired_values=stored,
        desired_secrets={f"{prefix}.api_key": "local-secret"},
    )
    values_runtime = _ValuesRuntime(snapshot)
    values_mutations = _ValuesMutations()
    values_service = ConfigValuesService(
        runtime=values_runtime,
        mutations=values_mutations,
        run_blocking=_inline,
    )
    identity_patch = await values_service.patch_flat(
        expected_revision=4,
        values={
            f"{prefix}.protocol": "vllm",
            f"{prefix}.wire_api": "chat-completions",
            f"{prefix}.api_base": "http://127.0.0.1:8000/v1",
            f"{prefix}.model": "other-vlm",
            f"{prefix}.api_key": MASKED_SECRET,
            f"{prefix}.probed_model": "vision-vlm",
            f"{prefix}.input_modalities": ["text", "image"],
            f"{prefix}.probed_json": True,
            f"{prefix}.probed_tools": True,
        },
    )
    del identity_patch
    assert _evidence_cleared(values_mutations.calls[0][1], prefix)

    same_runtime = _ValuesRuntime(snapshot)
    same_mutations = _ValuesMutations()
    same_service = ConfigValuesService(
        runtime=same_runtime,
        mutations=same_mutations,
        run_blocking=_inline,
    )
    await same_service.patch_flat(
        expected_revision=4,
        values={
            f"{prefix}.protocol": "vllm",
            f"{prefix}.wire_api": "chat-completions",
            f"{prefix}.api_base": "http://127.0.0.1:8000/v1",
            f"{prefix}.model": "vision-vlm",
            f"{prefix}.api_key": MASKED_SECRET,
            f"{prefix}.tool_chat": True,
            f"{prefix}.probed_model": "vision-vlm",
            f"{prefix}.input_modalities": ["text", "image"],
            f"{prefix}.probed_json": True,
            f"{prefix}.probed_tools": True,
        },
    )
    same_patch = same_mutations.calls[0][1]
    assert same_patch.values.get(f"{prefix}.probed_model") == "vision-vlm"
    assert same_patch.values.get(f"{prefix}.input_modalities") == ["text", "image"]
    assert f"{prefix}.probed_model" not in same_patch.unset

    unset_runtime = _ValuesRuntime(snapshot)
    unset_mutations = _ValuesMutations()
    unset_service = ConfigValuesService(
        runtime=unset_runtime,
        mutations=unset_mutations,
        run_blocking=_inline,
    )
    await unset_service.patch_flat(
        expected_revision=4,
        values={
            f"{prefix}.protocol": "vllm",
            f"{prefix}.wire_api": "chat-completions",
            f"{prefix}.api_base": "http://127.0.0.1:8000/v1",
            f"{prefix}.model": "vision-vlm",
        },
        unset=frozenset({f"{prefix}.api_key"}),
    )
    assert _evidence_cleared(unset_mutations.calls[0][1], prefix)

    verified_runtime = _ValuesRuntime(snapshot)
    verified_mutations = _ValuesMutations()
    verified_service = ConfigValuesService(
        runtime=verified_runtime,
        mutations=verified_mutations,
        run_blocking=_inline,
    )
    await verified_service.patch_flat(
        expected_revision=4,
        values={
            f"{prefix}.protocol": "vllm",
            f"{prefix}.wire_api": "chat-completions",
            f"{prefix}.api_base": "http://127.0.0.1:8000/v1",
            f"{prefix}.model": "other-vlm",
            f"{prefix}.probed_model": "other-vlm",
            f"{prefix}.input_modalities": ["text", "image"],
            f"{prefix}.probed_json": True,
            f"{prefix}.probed_tools": False,
        },
        probe_verified=True,
    )
    verified_patch = verified_mutations.calls[0][1]
    assert verified_patch.values[f"{prefix}.probed_model"] == "other-vlm"
    assert verified_patch.values[f"{prefix}.input_modalities"] == ["text", "image"]

    failed_runtime = _ValuesRuntime(snapshot)
    failed_mutations = _ValuesMutations()
    failed_service = ConfigValuesService(
        runtime=failed_runtime,
        mutations=failed_mutations,
        run_blocking=_inline,
    )
    await failed_service.patch_flat(
        expected_revision=4,
        values={
            f"{prefix}.protocol": "vllm",
            f"{prefix}.wire_api": "chat-completions",
            f"{prefix}.api_base": "http://127.0.0.1:8000/v1",
            f"{prefix}.model": "other-vlm",
            f"{prefix}.probed_model": "other-vlm",
            f"{prefix}.input_modalities": ["text"],
            f"{prefix}.probed_json": False,
            f"{prefix}.probed_tools": False,
        },
        probe_verified=True,
    )
    failed_patch = failed_mutations.calls[0][1]
    failed_modalities = failed_patch.values[f"{prefix}.input_modalities"]
    assert failed_modalities == ["text"]
    assert isinstance(failed_modalities, list)
    assert "image" not in failed_modalities

    document_yaml = yaml.safe_dump(
        {
            "ai": {
                "generation": {
                    "endpoints": {
                        "local": {
                            "protocol": "vllm",
                            "wire_api": "chat-completions",
                            "api_base": "http://127.0.0.1:8000/v1",
                            "model": "edited-offline",
                            "api_key": MASKED_SECRET,
                            "tool_chat": True,
                            "probed_model": "vision-vlm",
                            "input_modalities": ["text", "image"],
                            "probed_json": True,
                            "probed_tools": True,
                        }
                    }
                }
            }
        }
    )
    doc_mutations = _DocumentMutations()
    documents = ConfigDocumentsService(
        runtime=_DocumentRuntime(snapshot),
        mutations=doc_mutations,
        runtime_candidate=lambda overrides: _CANDIDATE_REPOSITORY.runtime_candidate(overrides, {}),
        resolve_secret=lambda _name: "local-secret",
        run_blocking=_inline,
    )
    await documents.replace_yaml(expected_revision=4, content=document_yaml)
    assert _evidence_cleared(doc_mutations.calls[0][2], prefix)

    unchanged_yaml = yaml.safe_dump(
        {
            "ai": {
                "generation": {
                    "endpoints": {
                        "local": {
                            "protocol": "vllm",
                            "wire_api": "chat-completions",
                            "api_base": "http://127.0.0.1:8000/v1",
                            "model": "vision-vlm",
                            "api_key": MASKED_SECRET,
                            "tool_chat": True,
                            "probed_model": "vision-vlm",
                            "input_modalities": ["text", "image"],
                            "probed_json": True,
                            "probed_tools": True,
                        }
                    }
                }
            }
        }
    )
    preserve_mutations = _DocumentMutations()
    preserve_documents = ConfigDocumentsService(
        runtime=_DocumentRuntime(snapshot),
        mutations=preserve_mutations,
        runtime_candidate=lambda overrides: _CANDIDATE_REPOSITORY.runtime_candidate(overrides, {}),
        resolve_secret=lambda _name: "local-secret",
        run_blocking=_inline,
    )
    await preserve_documents.replace_yaml(expected_revision=4, content=unchanged_yaml)
    preserved = preserve_mutations.calls[0][2]
    assert preserved.values.get(f"{prefix}.probed_model") == "vision-vlm"
    assert preserved.values.get(f"{prefix}.input_modalities") == ["text", "image"]


@pytest.mark.asyncio
async def test_optional_credentials_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[_FakeChatAdapter] = []

    def _adapter(endpoint: GenerationEndpointConfig) -> _FakeChatAdapter:
        headers = {}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        adapter = _FakeChatAdapter(headers=headers)
        client, _completions = _tool_client()
        adapter.client = client
        captured.append(adapter)
        return adapter

    monkeypatch.setattr(endpoint_activation, "create_local_provider_adapter", _adapter)

    async def _resolve(_endpoint: GenerationEndpointConfig) -> str:
        return "vision-vlm"

    monkeypatch.setattr(endpoint_activation, "resolve_vllm_served_model", _resolve)

    keyless = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(),
        DaemonConfig(),
    )
    keyed = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(api_key="local-secret"),
        DaemonConfig(),
    )

    assert keyless.endpoint.probed_model == "vision-vlm"
    assert keyed.endpoint.probed_model == "vision-vlm"
    assert "Authorization" not in captured[0].headers
    assert captured[1].headers["Authorization"] == "Bearer local-secret"


@pytest.mark.asyncio
async def test_probe_outcome_table(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(_endpoint: GenerationEndpointConfig) -> str:
        return "vision-vlm"

    monkeypatch.setattr(endpoint_activation, "resolve_vllm_served_model", _resolve)

    text_adapter = _FakeChatAdapter()
    text_adapter.text = "nope"
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: text_adapter,
    )
    with pytest.raises(EndpointActivationError):
        await probe_chat_completions_endpoint("local", _chat_endpoint(), DaemonConfig())

    json_adapter = _FakeChatAdapter()
    json_adapter.json_error = RuntimeError("json_object rejected")
    json_client, _json_completions = _tool_client()
    json_adapter.client = json_client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: json_adapter,
    )
    json_result = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(),
        DaemonConfig(),
    )
    assert json_result.endpoint.probed_json is False
    assert json_adapter.json_calls[0]["allow_fallback"] is False

    tool_adapter = _FakeChatAdapter()
    tool_client, _tool_completions = _tool_client(succeed=False)
    tool_adapter.client = tool_client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: tool_adapter,
    )
    tool_result = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(),
        DaemonConfig(),
    )
    assert tool_result.endpoint.probed_tools is False

    skipped_adapter = _FakeChatAdapter()
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: skipped_adapter,
    )
    skipped = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(tool_chat=False),
        DaemonConfig(),
    )
    assert skipped.endpoint.probed_tools is None
    assert skipped_adapter.client is None

    restored_adapter = _FakeChatAdapter()
    restored_client, _restored_completions = _tool_client()
    restored_adapter.client = restored_client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: restored_adapter,
    )
    restored = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(
            probed_model="vision-vlm",
            input_modalities=["text"],
            probed_json=False,
            probed_tools=False,
        ),
        DaemonConfig(),
    )
    assert restored.endpoint.probed_json is True
    assert restored.endpoint.probed_tools is True
    assert restored.endpoint.input_modalities == ["text", "image"]


@pytest.mark.asyncio
async def test_vision_probe_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(_endpoint: GenerationEndpointConfig) -> str:
        return "vision-vlm"

    monkeypatch.setattr(endpoint_activation, "resolve_vllm_served_model", _resolve)

    image_adapter = _FakeChatAdapter()
    image_client, _image_completions = _tool_client()
    image_adapter.client = image_client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: image_adapter,
    )
    unknown = _chat_endpoint()
    assert unknown.input_modalities is None
    assert unknown.probed_model is None
    image_capable = await probe_chat_completions_endpoint("local", unknown, DaemonConfig())
    assert any(call.get("images") for call in image_adapter.generate_calls)
    assert image_capable.endpoint.input_modalities == ["text", "image"]
    assert image_capable.vision_enabled is True

    text_adapter = _FakeChatAdapter()
    text_adapter.vision_error = RuntimeError("no vision")
    text_client, _text_completions = _tool_client()
    text_adapter.client = text_client
    monkeypatch.setattr(
        endpoint_activation,
        "create_local_provider_adapter",
        lambda _endpoint: text_adapter,
    )
    text_only = await probe_chat_completions_endpoint(
        "local",
        _chat_endpoint(),
        DaemonConfig(),
    )
    assert text_only.endpoint.input_modalities == ["text"]
    assert text_only.vision_enabled is False


@pytest.mark.asyncio
async def test_responses_path_evidence_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    async def pass_probe(*_args: object) -> None:
        return None

    monkeypatch.setattr(endpoint_activation, "_probe_text", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_json", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_tool_context_and_resume", pass_probe)
    monkeypatch.setattr(endpoint_activation, "_probe_vision", pass_probe)

    endpoint = GenerationEndpointConfig(
        wire_api="responses",
        api_base="https://openrouter.ai/api/v1",
        api_key="super-secret-key",
        model="moonshotai/kimi-k3",
        tool_chat=True,
    )
    result = await probe_responses_endpoint("openrouter", endpoint, DaemonConfig())

    assert result.endpoint.probed_model == "moonshotai/kimi-k3"
    assert result.endpoint.input_modalities == ["text", "image"]
    assert result.endpoint.probed_json is True
    assert result.endpoint.probed_tools is True
    assert not hasattr(result.endpoint, "vision_extract")

    unknown = GenerationEndpointConfig(
        wire_api="responses",
        api_base="https://openrouter.ai/api/v1",
        api_key="super-secret-key",
        model="moonshotai/kimi-k3",
        tool_chat=True,
    )
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(ai={"generation": {"endpoints": {"openrouter": unknown}}}),
        provider_installed=lambda _entry: True,
    )
    assert registry.binding(AICapability.VISION_EXTRACT, "endpoint:openrouter") is None
    snapshot = ConfigSnapshot(
        revision=1,
        desired=DaemonConfig(ai={"generation": {"endpoints": {"openrouter": unknown}}}),
        active=DaemonConfig(ai={"generation": {"endpoints": {"openrouter": unknown}}}),
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values={},
        active_values={},
        desired_bindings={},
        active_bindings={},
    )
    assert _vision_extract_enabled(snapshot) is False


def test_vision_extract_field_stripped_on_load() -> None:
    endpoint = GenerationEndpointConfig.model_validate(
        {
            "api_base": "http://127.0.0.1:1234/v1",
            "model": "llava",
            "vision_extract": True,
        }
    )
    assert not hasattr(endpoint, "vision_extract")
    assert endpoint.probed_model is None
    assert endpoint.input_modalities is None
    assert endpoint.probed_json is None
    assert endpoint.probed_tools is None
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(ai={"generation": {"endpoints": {"legacy": endpoint}}}),
        provider_installed=lambda _entry: True,
    )
    assert registry.binding(AICapability.VISION_EXTRACT, "endpoint:legacy") is None
    snapshot = ConfigSnapshot(
        revision=1,
        desired=DaemonConfig(ai={"generation": {"endpoints": {"legacy": endpoint}}}),
        active=DaemonConfig(ai={"generation": {"endpoints": {"legacy": endpoint}}}),
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values={},
        active_values={},
        desired_bindings={},
        active_bindings={},
    )
    assert _vision_extract_enabled(snapshot) is False


def test_activate_request_accepts_chat_completions_protocols() -> None:
    from gobby.servers.routes.configuration_generation_endpoints import (
        ActivateGenerationEndpointRequest,
    )

    request = ActivateGenerationEndpointRequest.model_validate(
        {
            "expected_revision": 1,
            "protocol": "vllm",
            "wire_api": "chat-completions",
            "api_base": "http://127.0.0.1:8000/v1",
            "model": "vision-vlm",
        }
    )
    assert request.protocol == "vllm"
    assert request.wire_api == "chat-completions"
    assert not hasattr(request, "vision_extract")
