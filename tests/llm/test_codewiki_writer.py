from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from gobby.ai import AIAdapterStyle, AICapability, AICapabilityRegistry, CapabilityBinding
from gobby.llm.codewiki_writer import (
    CodeWikiWriterError,
    CodeWikiWriterRequest,
    CodeWikiWriterService,
)

pytestmark = pytest.mark.unit


class RecordingRunner:
    def __init__(self, text: str = "Generated CodeWiki prose") -> None:
        self.text = text
        self.calls: list[tuple[list[str], Path, float, dict[str, str]]] = []

    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        env_overrides: Mapping[str, str],
    ) -> None:
        self.calls.append((list(command), cwd, timeout_seconds, dict(env_overrides)))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(self.text, encoding="utf-8")


async def test_codewiki_writer_uses_repo_cwd_read_only_codex_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    service = CodeWikiWriterService(
        _registry_for_codex(),
        command_path="/usr/local/bin/codex",
        runner=runner,
        tech_writer_loader=lambda: "## Tech Writer\nUse evidence.",
    )

    result = await service.write(
        CodeWikiWriterRequest(
            prompt="Write the repo overview.",
            system_prompt="Keep citations grounded.",
            cwd=str(tmp_path),
            candidates=({"candidate": "codex/gpt-5.5", "reasoning_effort": "xhigh"},),
            max_tokens=1234,
            timeout_seconds=7,
            page_kind="repo_overview",
        )
    )

    assert result.text == "Generated CodeWiki prose"
    assert result.provider == "codex"
    assert result.model == "gpt-5.5"
    command, cwd, timeout_seconds, _env = runner.calls[0]
    assert cwd == tmp_path.resolve()
    assert timeout_seconds == 7
    assert command[:4] == ["/usr/local/bin/codex", "--ask-for-approval", "never", "exec"]
    assert _arg_value(command, "--sandbox") == "read-only"
    assert _arg_value(command, "--cd") == str(tmp_path.resolve())
    assert _arg_value(command, "--model") == "gpt-5.5"
    assert "--ignore-rules" in command
    assert 'model_reasoning_effort="xhigh"' in command
    assert "model_max_output_tokens=1234" in command
    prompt = command[-1]
    assert "Do not modify files" in prompt
    assert "Tech-writer methodology to apply, not an agent to spawn" in prompt
    assert "Use evidence." in prompt
    assert "Keep citations grounded." in prompt
    assert "Write the repo overview." in prompt


async def test_codewiki_writer_rejects_unsupported_provider(tmp_path: Path) -> None:
    service = CodeWikiWriterService(
        _registry_for_codex(),
        command_path="/usr/local/bin/codex",
        runner=RecordingRunner(),
        tech_writer_loader=lambda: "method",
    )

    with pytest.raises(CodeWikiWriterError) as exc_info:
        await service.write(
            CodeWikiWriterRequest(
                prompt="Write page.",
                system_prompt=None,
                cwd=str(tmp_path),
                candidates=("claude/opus",),
            )
        )

    assert exc_info.value.code == "unsupported_provider_model"
    assert exc_info.value.to_dict()["error"]["retryable"] is False
    assert exc_info.value.diagnostics["rejected_candidates"] == [
        {"candidate": "claude/opus", "reason": "unsupported_provider"}
    ]


async def test_codewiki_writer_timeout_is_structured_non_retryable(tmp_path: Path) -> None:
    async def timeout_runner(
        _command: Sequence[str],
        _cwd: Path,
        _timeout_seconds: float,
        _env_overrides: Mapping[str, str],
    ) -> None:
        raise TimeoutError

    service = CodeWikiWriterService(
        _registry_for_codex(),
        command_path="/usr/local/bin/codex",
        runner=timeout_runner,
        tech_writer_loader=lambda: "method",
    )

    with pytest.raises(CodeWikiWriterError) as exc_info:
        await service.write(
            CodeWikiWriterRequest(
                prompt="Write page.",
                system_prompt=None,
                cwd=str(tmp_path),
                candidates=("codex/gpt-5.5",),
                timeout_seconds=3,
            )
        )

    assert exc_info.value.code == "timeout"
    assert exc_info.value.to_dict()["error"]["retryable"] is False


async def test_codewiki_writer_requires_profile_or_candidates(tmp_path: Path) -> None:
    service = CodeWikiWriterService(
        _registry_for_codex(),
        command_path="/usr/local/bin/codex",
        runner=RecordingRunner(),
        tech_writer_loader=lambda: "method",
    )

    with pytest.raises(CodeWikiWriterError, match="exactly one"):
        await service.write(
            CodeWikiWriterRequest(
                prompt="Write page.",
                system_prompt=None,
                cwd=str(tmp_path),
            )
        )


def _registry_for_codex() -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("gpt-5.5",),
            )
        ]
    )


def _arg_value(command: Sequence[str], name: str) -> str:
    return command[command.index(name) + 1]
