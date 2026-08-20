"""Routes for daemon-owned AI capability execution."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import UUID4, AliasChoices, BaseModel, ConfigDict, Field, field_validator

from gobby.ai import (
    AICapability,
    CapabilityUnavailableError,
    TextGenerationRequest,
    ToolChatRequest,
    ToolLoopLimits,
    ToolPolicy,
    VisionExtractRequest,
    build_daemon_ai_capability_registry,
    build_daemon_vision_extract_service,
)
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import (
    FeatureCandidateInput,
    FeatureProfile,
)
from gobby.llm.base import validate_vision_description
from gobby.llm.image_payloads import MAX_IMAGE_BYTES, prepare_image_inputs
from gobby.servers.chat_attachment_limits import resolve_server_attachment_limits
from gobby.servers.responses import JSONResponse
from gobby.servers.upload_limits import read_bounded_upload

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


logger = logging.getLogger(__name__)


def _server_operation(server: HTTPServer, name: str) -> tuple[DaemonConfig | None, Any]:
    from gobby.runner_init.services import AIServiceBundle
    from gobby.servers.http import HTTPServer as RuntimeHTTPServer

    if isinstance(server, RuntimeHTTPServer):
        bundle = server.capture_runtime_bundle()
        config = bundle.snapshot.active if bundle else server.startup_config
        if bundle is not None:
            ai_services = bundle.services.get("ai_services")
            if isinstance(ai_services, AIServiceBundle):
                return config, getattr(ai_services, name)
        return config, getattr(server.services, name, None)
    return server.config, getattr(server.services, name, None)


DEFAULT_TEXT_GENERATE_PROFILE = FeatureProfile.LOW.value
DEFAULT_CHAT_COMPLETIONS_PROFILE = FeatureProfile.HIGH.value
_VISION_TEMP_DIR_NAME = "gobby-vision"
_VISION_TEMP_MAX_AGE_SECONDS = 24 * 60 * 60
_VISION_TEMP_CLEANUP_INTERVAL_SECONDS = max(60.0, min(3600.0, _VISION_TEMP_MAX_AGE_SECONDS / 2))
_VISION_TEMP_CLEANUP_TASK_ATTR = "vision_temp_cleanup_task"


class TextGeneratePayload(BaseModel):
    """Request body for one-shot text_generate execution."""

    prompt: str = Field(min_length=1)
    provider: str | None = None
    profile: str | None = None
    candidates: tuple[FeatureCandidateInput, ...] = ()
    system_prompt: str | None = Field(
        default=None,
        validation_alias=AliasChoices("system_prompt", "system"),
    )
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None
    cwd: str | None = None
    candidate_timeout_seconds: float | None = Field(default=None, gt=0.0)
    cli_candidate_timeout_seconds: float | None = Field(default=None, gt=0.0)
    total_timeout_seconds: float | None = Field(default=None, gt=0.0)
    images: list[str] | None = None

    @property
    def effective_profile(self) -> str | None:
        """Default generic generation to the daemon's LOW feature profile."""
        if self.provider or self.model or self.profile or self.candidates:
            return self.profile
        return DEFAULT_TEXT_GENERATE_PROFILE


def _generation_timeout_overrides(
    payload: TextGeneratePayload,
    *,
    configured_candidate: float,
    configured_cli_candidate: float,
    configured_total: float,
) -> tuple[float | None, float | None, float | None]:
    """Resolve per-request timeout overrides against the configured budgets.

    Payload-supplied per-candidate timeouts are capped at the total attempt
    budget only — a caller that declares a long-running aggregate generation may
    raise its candidate budget above the
    tight configured per-candidate defaults, which exist for fast failover on
    short per-file generations (#17710) and still apply when the payload omits
    them.
    """
    total = (
        min(payload.total_timeout_seconds, configured_total)
        if payload.total_timeout_seconds is not None
        else None
    )
    total_cap = total if total is not None else configured_total
    candidate = (
        min(payload.candidate_timeout_seconds, total_cap)
        if payload.candidate_timeout_seconds is not None
        else (min(configured_candidate, total_cap) if total is not None else None)
    )
    cli_candidate = (
        min(payload.cli_candidate_timeout_seconds, total_cap)
        if payload.cli_candidate_timeout_seconds is not None
        else (min(configured_cli_candidate, total_cap) if total is not None else None)
    )
    return candidate, cli_candidate, total


class ChatMessage(BaseModel):
    """One OpenAI-style chat message."""

    role: Literal["system", "user", "assistant"]
    content: str


class ToolPolicyPayload(BaseModel):
    """Caller-declared tool policy for a tool_chat request."""

    cli: Literal["gcode"]
    tools: tuple[str, ...] = Field(min_length=1)
    allow_mutation: Literal[False] = False


class ToolLoopLimitsPayload(BaseModel):
    """Complete version-1 tool-loop limits override."""

    model_config = ConfigDict(extra="forbid")

    max_turns: int | None = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_bytes_per_tool_result: int = Field(gt=0)
    tool_timeout_seconds: float = Field(gt=0)
    loop_timeout_seconds: int = Field(gt=0)


class ChatCompletionsPayload(BaseModel):
    """Request body for daemon-side provider-agnostic tool_chat generation."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    project_path: str = Field(min_length=1)
    tool_policy: ToolPolicyPayload
    caller: str
    request_id: UUID4
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    candidates: tuple[FeatureCandidateInput, ...] = ()
    limits: ToolLoopLimitsPayload | None = None
    reasoning_effort: str | None = None
    speed_mode: Literal["standard", "fast"] = "standard"

    @field_validator("caller")
    @classmethod
    def validate_caller(cls, value: str) -> str:
        caller = value.strip()
        if not caller:
            raise ValueError("caller must not be blank")
        return caller

    @property
    def effective_profile(self) -> str | None:
        """Default to the daemon HIGH feature profile unless routing is explicit."""
        if self.provider or self.model or self.profile or self.candidates:
            return self.profile
        return DEFAULT_CHAT_COMPLETIONS_PROFILE


def _split_chat_messages(messages: list[ChatMessage]) -> tuple[str | None, str]:
    """Split chat messages into a joined system prompt and a user prompt.

    System messages become the agent ``system_prompt``; the remaining user and
    assistant messages are joined into the investigation prompt. Raises
    ``ValueError`` when no non-empty user/assistant content is present.
    """
    system_parts = [m.content for m in messages if m.role == "system" and m.content.strip()]
    prompt_parts = [
        f"{m.role.capitalize()}:\n{m.content}"
        for m in messages
        if m.role != "system" and m.content.strip()
    ]
    user_prompt = "\n\n".join(prompt_parts)
    if not user_prompt.strip():
        raise ValueError(
            "chat completion requires at least one non-empty user or assistant message"
        )
    system_prompt = "\n\n".join(system_parts) or None
    return system_prompt, user_prompt


def _finish_reason_from_stop_reason(stop_reason: str | None) -> str:
    if stop_reason is None:
        return "stop"
    if stop_reason in ("", "completed", "stop"):
        return "stop"
    if stop_reason in {"max_turns", "max_tool_calls"}:
        return "length"
    return stop_reason


def create_llm_router(server: HTTPServer) -> APIRouter:
    """Create daemon AI capability routes."""
    router = APIRouter(prefix="/api/llm", tags=["llm"])

    @router.get("/status")
    async def llm_status() -> dict[str, Any]:
        """Return daemon AI capability registry status."""
        registry = build_daemon_ai_capability_registry(server.config)
        return registry.status_snapshot()

    @router.post("/generate")
    async def generate_text(payload: TextGeneratePayload) -> Any:
        """Run one-shot text_generate through the daemon capability registry."""
        config, service = _server_operation(server, "text_generation_service")
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")

        if service is None:
            raise HTTPException(status_code=503, detail="Text generation service not initialized")
        generation_config = config.ai.generation
        candidate_timeout, cli_candidate_timeout, total_timeout = _generation_timeout_overrides(
            payload,
            configured_candidate=generation_config.candidate_timeout_seconds,
            configured_cli_candidate=generation_config.cli_candidate_timeout_seconds,
            configured_total=generation_config.timeout_seconds,
        )
        try:
            images = payload.images or None
            if images:
                await prepare_image_inputs(images)
            result = await service.generate_result(
                TextGenerationRequest(
                    prompt=payload.prompt,
                    provider=payload.provider,
                    profile=payload.effective_profile,
                    candidates=payload.candidates,
                    system_prompt=payload.system_prompt,
                    model=payload.model,
                    max_tokens=payload.max_tokens,
                    reasoning_effort=payload.reasoning_effort,
                    caller="llm-generate-route",
                    cwd=payload.cwd,
                    candidate_timeout_seconds=candidate_timeout,
                    cli_candidate_timeout_seconds=cli_candidate_timeout,
                    total_timeout_seconds=total_timeout,
                    images=images,
                )
            )
            response: dict[str, Any] = {
                "text": result.text,
                "capability": AICapability.TEXT_GENERATE.value,
                "provider": result.provider,
                "model": result.model,
            }
            if result.usage is not None:
                response["usage"] = result.usage
            if result.applied_reasoning_effort is not None:
                response["applied_reasoning_effort"] = result.applied_reasoning_effort
            return response
        except CapabilityUnavailableError as e:
            logger.info("Text generation capability unavailable: %s", e)
            return JSONResponse(status_code=400, content=_capability_error_detail(e))
        except ValueError as e:
            logger.info("Text generation rejected: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Text generation failed")
            raise HTTPException(status_code=500, detail="Text generation failed") from e

    @router.post("/chat/completions")
    async def chat_completions(payload: ChatCompletionsPayload, request: Request) -> Any:
        """Run daemon-side provider-agnostic agentic ``tool_chat`` generation.

        Resolves a ``tool_chat`` binding from the requested profile/candidates,
        dispatches on the binding's ``AIAdapterStyle``, and runs the agent under
        the caller's declared tool policy and directive. Returns an OpenAI-shaped
        completion plus an ``investigation`` provenance block. This route holds
        no provider names and performs no fallback — an unsatisfiable capability
        is reported as a 400.
        """
        claims = server.auth_service.verified_agent_claims(request)
        session_header = (
            claims.session_id if claims is not None else request.headers.get("X-Gobby-Session-Id")
        )
        try:
            authenticated_session_id = UUID(session_header) if session_header else None
        except ValueError:
            authenticated_session_id = None
        if authenticated_session_id is None:
            raise HTTPException(
                status_code=401,
                detail="Authenticated session header is required",
            )

        config, service = _server_operation(server, "tool_chat_service")
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")
        if service is None:
            raise HTTPException(status_code=503, detail="Tool chat service not initialized")
        try:
            system_prompt, user_prompt = _split_chat_messages(payload.messages)
            result = await service.chat_result(
                ToolChatRequest(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    tool_policy=ToolPolicy(
                        cli=payload.tool_policy.cli,
                        tools=tuple(payload.tool_policy.tools),
                        allow_mutation=payload.tool_policy.allow_mutation,
                    ),
                    project_path=payload.project_path,
                    session_id=authenticated_session_id,
                    profile=payload.effective_profile,
                    provider=payload.provider,
                    candidates=payload.candidates,
                    model=payload.model,
                    limits=(
                        ToolLoopLimits(**payload.limits.model_dump())
                        if payload.limits is not None
                        else None
                    ),
                    reasoning_effort=payload.reasoning_effort,
                    caller=payload.caller,
                    request_id=str(payload.request_id),
                    speed_mode=payload.speed_mode,
                )
            )
            response: dict[str, Any] = {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": _finish_reason_from_stop_reason(result.stop_reason),
                    }
                ],
                "model": result.model,
                "investigation": {
                    "caller": payload.caller,
                    "request_id": str(payload.request_id),
                    "tool_use_count": result.tool_use_count,
                    "turns": result.turns,
                    "tools": result.tools,
                    "adapter_style": result.adapter_style,
                    "stop_reason": result.stop_reason,
                },
                "speed": result.speed,
            }
            if result.usage is not None:
                response["usage"] = result.usage
            if result.applied_reasoning_effort is not None:
                response["applied_reasoning_effort"] = result.applied_reasoning_effort
            return response
        except CapabilityUnavailableError as e:
            logger.info("tool_chat capability unavailable: %s", e)
            return JSONResponse(status_code=400, content=_capability_error_detail(e))
        except ValueError as e:
            logger.info("tool_chat rejected: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("tool_chat failed")
            raise HTTPException(status_code=500, detail="tool_chat failed") from e

    @router.get("/vision/status")
    async def vision_status() -> dict[str, Any]:
        """Return vision_extract capability status."""
        registry = build_daemon_ai_capability_registry(server.config)
        return registry.status(AICapability.VISION_EXTRACT).to_dict()

    @router.post("/vision/extract")
    async def extract_vision(
        file: UploadFile = File(...),
        provider: str | None = Form(default=None),
        model: str | None = Form(default=None),
        context: str | None = Form(default=None),
    ) -> Any:
        """Extract a text description from an uploaded image."""
        config = server.config
        if config is None:
            raise HTTPException(status_code=503, detail="Daemon config not found")

        max_upload_bytes = min(
            MAX_IMAGE_BYTES,
            resolve_server_attachment_limits(server).max_file_bytes,
        )
        image_bytes = await _read_bounded_image_upload(file, max_bytes=max_upload_bytes)
        try:
            image_path = _write_temp_image(image_bytes, file.filename)
        except RuntimeError as e:
            logger.error("Vision upload preparation failed: %s", e)
            raise HTTPException(status_code=500, detail="Vision upload failed") from e

        try:
            service = build_daemon_vision_extract_service(config)
            result = await service.extract(
                VisionExtractRequest(
                    image_path=str(image_path),
                    provider=provider or None,
                    model=model or None,
                    context=context or None,
                    caller="llm-vision-route",
                )
            )
            description = validate_vision_description(result.text)
            return {
                "text": description,
                "description": description,
                "ocr_text": result.ocr_text,
                "bytes": len(image_bytes),
                "content_type": file.content_type or "application/octet-stream",
                "capability": result.capability.value,
                "provider": result.provider,
                "model": result.model,
            }
        except CapabilityUnavailableError as e:
            logger.info("Vision capability unavailable: %s", e)
            return JSONResponse(status_code=400, content=_capability_error_detail(e))
        except ValueError as e:
            logger.info("Vision extraction rejected: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Vision extraction failed")
            raise HTTPException(status_code=500, detail="Vision extraction failed") from e
        finally:
            if image_path is not None:
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove vision temp file %s", image_path)

    return router


async def _read_bounded_image_upload(
    file: UploadFile,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> bytes:
    """Read at most one byte past the provider image limit."""
    return await read_bounded_upload(file, max_bytes=max_bytes, label="Image")


def _capability_error_detail(error: CapabilityUnavailableError) -> dict[str, Any]:
    return {
        "code": "capability_unavailable",
        "capability": error.capability.value,
        "provider": error.provider,
        "model": error.model,
        "reason": error.reason or str(error),
    }


def _write_temp_image(image_bytes: bytes, filename: str | None) -> Path:
    suffix = _image_suffix(filename)
    temp_dir = _vision_temp_dir()
    image_path: Path | None = None
    try:
        with NamedTemporaryFile(
            delete=False,
            prefix="vision-",
            suffix=suffix,
            dir=temp_dir,
        ) as temp_file:
            image_path = Path(temp_file.name)
            temp_file.write(image_bytes)
            # Extraction runs in the daemon process, so uploaded images stay owner-only.
            os.chmod(temp_file.name, stat.S_IRUSR | stat.S_IWUSR)
            return image_path
    except OSError as exc:
        if image_path is not None:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove incomplete vision temp file %s", image_path)
        raise RuntimeError(f"Failed to write vision temp image in {str(temp_dir)!r}") from exc


def _vision_temp_dir() -> Path:
    try:
        temp_dir = Path(tempfile.gettempdir()) / _VISION_TEMP_DIR_NAME
        temp_dir.mkdir(mode=stat.S_IRWXU, exist_ok=True)
        os.chmod(temp_dir, stat.S_IRWXU)
        return temp_dir
    except OSError as exc:
        logger.error("Failed to prepare vision temp directory %s", _VISION_TEMP_DIR_NAME)
        raise RuntimeError(
            f"Failed to prepare vision temp directory {_VISION_TEMP_DIR_NAME!r}"
        ) from exc


def _cleanup_stale_vision_temp_files(now: float | None = None) -> None:
    temp_dir = Path(tempfile.gettempdir()) / _VISION_TEMP_DIR_NAME
    if not temp_dir.is_dir():
        return
    cutoff = (time.time() if now is None else now) - _VISION_TEMP_MAX_AGE_SECONDS
    for path in temp_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.debug("Failed to remove stale vision temp file %s", path, exc_info=True)


def _run_vision_temp_cleanup_once() -> None:
    try:
        _cleanup_stale_vision_temp_files()
    except OSError:
        logger.debug("Failed to scan vision temp directory", exc_info=True)


async def _vision_temp_cleanup_loop() -> None:
    while True:
        _run_vision_temp_cleanup_once()
        await asyncio.sleep(_VISION_TEMP_CLEANUP_INTERVAL_SECONDS)


def start_vision_temp_cleanup_task(app: Any) -> None:
    _run_vision_temp_cleanup_once()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    existing = getattr(app.state, _VISION_TEMP_CLEANUP_TASK_ATTR, None)
    if existing is not None and not existing.done():
        existing.cancel()
    setattr(
        app.state,
        _VISION_TEMP_CLEANUP_TASK_ATTR,
        loop.create_task(_vision_temp_cleanup_loop()),
    )


async def stop_vision_temp_cleanup_task(app: Any) -> None:
    task = getattr(app.state, _VISION_TEMP_CLEANUP_TASK_ATTR, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass
    setattr(app.state, _VISION_TEMP_CLEANUP_TASK_ATTR, None)


def _image_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    return ".png"


__all__ = ["create_llm_router", "start_vision_temp_cleanup_task", "stop_vision_temp_cleanup_task"]
