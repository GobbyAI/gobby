from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from gobby.adapters.acp_client import StreamEvent
from gobby.ai import (
    ACPTextGenerateAdapter,
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    DroidCLITextGenerateAdapter,
    LLMProviderTextGenerateAdapter,
    TextGenerationRequest,
    TextGenerationService,
)
from gobby.llm.base import LLMProvider

pytestmark = pytest.mark.unit


class RecordingAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return f"{self.provider}:{request.prompt}"


@pytest.mark.asyncio
async def test_text_generation_service_selects_available_registry_binding() -> None:
    providers = {
        "claude": AIAdapterStyle.LLM_PROVIDER,
        "codex": AIAdapterStyle.LLM_PROVIDER,
        "local": AIAdapterStyle.OPENAI_COMPATIBLE,
        "gemini": AIAdapterStyle.ACP,
        "grok": AIAdapterStyle.ACP,
        "qwen": AIAdapterStyle.ACP,
        "droid": AIAdapterStyle.CLI,
    }
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=adapter_style,
                available=True,
            )
            for provider, adapter_style in providers.items()
        ]
    )
    adapters = {provider: RecordingAdapter(provider) for provider in providers}
    service = TextGenerationService(registry, adapters)

    for provider in providers:
        response = await service.generate(
            TextGenerationRequest(prompt="summarize", provider=provider)
        )
        assert response == f"{provider}:summarize"
        assert adapters[provider].requests[-1].provider == provider


class FakeLLMProvider(LLMProvider):
    @property
    def provider_name(self) -> str:
        return "fake"

    async def generate_summary(
        self, context: dict[str, Any], prompt_template: str | None = None
    ) -> str:
        return "summary"

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
    ) -> str:
        return f"{system_prompt}:{prompt}:{model}:{max_tokens}:{caller}"

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        *,
        caller: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        return "image"


@pytest.mark.asyncio
async def test_llm_provider_adapter_forwards_text_generation_request() -> None:
    adapter = LLMProviderTextGenerateAdapter(FakeLLMProvider())

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="hello",
            system_prompt="system",
            model="model-a",
            max_tokens=42,
            caller="test",
        )
    )

    assert response == "system:hello:model-a:42:test"


class FakeACPClient:
    def __init__(self) -> None:
        self.started: dict[str, object] | None = None
        self.sent: list[dict[str, object]] = []
        self.stopped = False

    async def start(self, **kwargs: object) -> None:
        self.started = kwargs

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, message: str, **kwargs: object) -> AsyncIterator[StreamEvent]:
        self.sent.append({"message": message, **kwargs})
        yield StreamEvent(event_type="content_delta", data={"content": "hello "})
        yield StreamEvent(event_type="content_delta", data={"content": "world"})
        yield StreamEvent(event_type="result", data={"content": "ignored fallback"})


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "grok", "qwen"])
async def test_acp_text_generate_adapter_runs_one_shot_prompt_turn(provider: str) -> None:
    client = FakeACPClient()
    adapter = ACPTextGenerateAdapter(lambda: client)  # type: ignore[arg-type]

    response = await adapter.generate(
        TextGenerationRequest(
            provider=provider,
            prompt="user prompt",
            system_prompt="system prompt",
            model="model-a",
            cwd="/tmp/project",
        )
    )

    assert response == "hello world"
    assert client.started == {
        "auto_session": True,
        "cwd": "/tmp/project",
        "model": "model-a",
    }
    assert client.sent == [
        {
            "message": "system prompt\n\nuser prompt",
            "model": "model-a",
        }
    ]
    assert client.stopped is True


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int | None = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


class HangingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(b"", returncode=None)
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        super().kill()


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_executes_noninteractive_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
    ) -> FakeProcess:
        calls.append(
            {
                "command": command,
                "stdout": stdout,
                "stderr": stderr,
                "cwd": cwd,
                "env": env,
            }
        )
        return FakeProcess(b"done\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="explain",
            system_prompt="system",
            model="claude-opus-4-7",
            cwd="/tmp/project",
        )
    )

    assert response == "done"
    assert calls[0]["command"] == (
        "/usr/local/bin/droid",
        "exec",
        "--output-format",
        "text",
        "--model",
        "claude-opus-4-7",
        "system\n\nexplain",
    )
    assert calls[0]["cwd"] == "/tmp/project"
    assert calls[0]["env"]["GOBBY_HOOKS_DISABLED"] == "1"  # type: ignore[index]


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_reports_exec_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(
        *_command: str,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
    ) -> FakeProcess:
        return FakeProcess(b"", b"bad auth", returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    with pytest.raises(RuntimeError, match="Droid exec failed with exit code 2: bad auth"):
        await adapter.generate(TextGenerationRequest(prompt="hello"))


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_reports_timeout_with_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HangingProcess()

    async def fake_create_subprocess_exec(
        *_command: str,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
    ) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(
        command_path="/usr/local/bin/droid",
        timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await adapter.generate(TextGenerationRequest(prompt="hello world"))

    assert process.killed is True
    assert "Droid exec timed out after 0.01s" in str(exc_info.value)
    assert "/usr/local/bin/droid exec --output-format text 'hello world'" in str(exc_info.value)
