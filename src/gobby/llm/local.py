"""
Local LLM provider implementation.

Routes LLM calls to a local OpenAI-compatible endpoint (LM Studio, Ollama,
vLLM, llama.cpp server, etc.) via the ``openai`` Python SDK.

Used when a feature config sets ``provider: "local"`` — e.g. for title
synthesis — giving lightweight, zero-cost inference with automatic
fallback to Claude via the tier system when the local server is down.
"""

import json
import logging
from pathlib import Path
from typing import Any

from gobby.llm.base import AuthMode, LLMProvider

logger = logging.getLogger(__name__)

# Known cloud model shortnames that shouldn't be sent to a local server.
_CLOUD_MODEL_ALIASES: frozenset[str] = frozenset(
    {
        # Claude
        "haiku",
        "sonnet",
        "opus",
        # OpenAI
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "gpt-5-mini",
        "o1",
        "o3",
        "o3-mini",
        "o4-mini",
    }
)


class LocalLLMProvider(LLMProvider):
    """LLM provider for local OpenAI-compatible endpoints.

    Talks to any server that implements the ``/v1/chat/completions``
    endpoint (LM Studio, Ollama, vLLM, etc.) using the ``openai`` SDK.

    Configuration is read from ``DaemonConfig.local`` (``LocalConfig``):
    - ``url``: Base URL (e.g. ``http://localhost:1234/v1``)
    - ``model``: Default model name to request
    - ``api_key``: Optional API key (some local servers require one)
    """

    @property
    def provider_name(self) -> str:
        return "local"

    @property
    def auth_mode(self) -> AuthMode:
        return "api_key"

    def __init__(self, config: Any) -> None:
        """Initialise from DaemonConfig.

        Args:
            config: DaemonConfig instance — must have a non-None ``local`` field.

        Raises:
            ValueError: If ``config.local`` is not configured.
        """
        local_cfg = getattr(config, "local", None)
        if not local_cfg:
            raise ValueError(
                "Provider 'local' requires the 'local' config section "
                "(url, model). Configure it in your daemon YAML."
            )

        self._url: str = local_cfg.url
        self._default_model: str = local_cfg.model
        self._api_key: str = local_cfg.api_key or "not-needed"
        self._client: Any | None = None

        try:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self._url,
                api_key=self._api_key,
            )
            logger.debug(
                "Local LLM provider initialised (url=%s, model=%s)",
                self._url,
                self._default_model,
            )
        except ImportError:
            logger.error("openai package not found — install with: uv add openai")
        except Exception as e:
            logger.error("Failed to initialise local LLM client: %s", e)

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, model: str | None) -> str:
        """Return a model name safe to send to the local endpoint.

        If *model* is a known cloud alias (e.g. ``"haiku"``), log a warning
        and fall back to ``config.local.model``.
        """
        if model is None:
            return self._default_model

        if model.lower() in _CLOUD_MODEL_ALIASES:
            logger.warning(
                "Model '%s' is a cloud alias — using local default '%s' instead",
                model,
                self._default_model,
            )
            return self._default_model

        return model

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if not self._client:
            raise RuntimeError("Local LLM client not initialised")

        resolved = self._resolve_model(model)
        response = await self._client.chat.completions.create(
            model=resolved,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt or "You are a helpful assistant.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens or 8000,
        )
        return response.choices[0].message.content or ""

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError("Local LLM client not initialised")

        resolved = self._resolve_model(model)

        # Try structured JSON mode first; fall back if the server rejects it.
        try:
            response = await self._client.chat.completions.create(
                model=resolved,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        or "You are a helpful assistant. Respond with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
                response_format={"type": "json_object"},
            )
        except Exception as json_mode_err:
            # Many local models don't support response_format — retry without.
            logger.debug(
                "json_object mode rejected (%s), retrying without response_format",
                json_mode_err,
            )
            response = await self._client.chat.completions.create(
                model=resolved,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        or "You are a helpful assistant. Respond with valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
            )

        content = response.choices[0].message.content
        if not content:
            raise ValueError("Empty response from local LLM")

        # Strip markdown fences that some models wrap JSON in.
        # Only strip the *outermost* opening/closing fences — internal fence
        # lines (e.g. inside a code block embedded in a JSON string) must be
        # preserved.
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines)

        try:
            result: dict[str, Any] = json.loads(stripped)
            return result
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse local LLM response as JSON: {e}") from e

    async def generate_summary(
        self,
        context: dict[str, Any],
        prompt_template: str | None = None,
    ) -> str:
        if not self._client:
            return "Session summary unavailable (local LLM client not initialised)"

        formatted_context = {
            "transcript_summary": context.get("transcript_summary", ""),
            "last_messages": json.dumps(context.get("last_messages", []), indent=2),
            "git_status": context.get("git_status", ""),
            "file_changes": context.get("file_changes", ""),
            **{
                k: v
                for k, v in context.items()
                if k not in ["transcript_summary", "last_messages", "git_status", "file_changes"]
            },
        }

        if not prompt_template:
            raise ValueError(
                "prompt_template is required for generate_summary. "
                "Configure 'session_summary.prompt' via gobby-config MCP tools"
            )

        try:
            from jinja2 import Environment

            env = Environment(autoescape=False)  # nosec B701 # generating text prompts
            template = env.from_string(prompt_template)
            prompt = template.render(**formatted_context)
        except ImportError:
            logger.warning("Jinja2 not available, using str.format fallback")
            prompt = prompt_template.format(**formatted_context)

        try:
            resolved = self._resolve_model(None)
            response = await self._client.chat.completions.create(
                model=resolved,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a session summary generator. "
                        "Create comprehensive, actionable summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error("Failed to generate summary with local LLM: %s", e)
            return f"Session summary generation failed: {e}"

    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        import base64
        import mimetypes

        if not self._client:
            return "Image description unavailable (local LLM client not initialised)"

        path = Path(image_path)
        if not path.exists():
            return f"Image not found: {image_path}"

        try:
            image_data = path.read_bytes()
            image_base64 = base64.standard_b64encode(image_data).decode("utf-8")

            mime_type, _ = mimetypes.guess_type(str(path))
            if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime_type = "image/png"

            prompt = (
                "Please describe this image in detail, focusing on key visual elements, "
                "any text visible, and the overall context or meaning."
            )
            if context:
                prompt = f"{context}\n\n{prompt}"

            resolved = self._resolve_model(model)
            response = await self._client.chat.completions.create(
                model=resolved,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_base64}",
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                max_tokens=1024,
            )
            return response.choices[0].message.content or "No description generated"
        except Exception as e:
            logger.error("Failed to describe image with local LLM: %s", e)
            return f"Image description failed: {e}"
