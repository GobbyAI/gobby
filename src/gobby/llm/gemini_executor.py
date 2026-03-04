"""
Gemini implementation of AgentExecutor using the Google GenAI SDK.

Supports both API key and ADC (Application Default Credentials) auth modes.
Uses the unified google-genai SDK which handles both Google AI Studio and
Vertex AI through a single Client interface.
"""

import asyncio
import json
import logging
from typing import Any, Literal

from gobby.llm.cost_table import calculate_cost
from gobby.llm.executor import (
    AgentExecutor,
    AgentResult,
    CostInfo,
    ToolCallRecord,
    ToolHandler,
    ToolResult,
    ToolSchema,
)

logger = logging.getLogger(__name__)

GeminiAuthMode = Literal["api_key", "adc"]


class GeminiExecutor(AgentExecutor):
    """
    Gemini implementation of AgentExecutor using google-genai SDK.

    Uses the unified google-genai SDK which supports both API key (Google AI
    Studio) and ADC (Vertex AI) auth through a single Client.

    The executor implements a proper agentic loop:
    1. Send prompt to Gemini with tool declarations
    2. When Gemini returns function calls, execute via tool_handler
    3. Send function responses back as Content(role="user")
    4. Repeat until Gemini stops requesting functions or limits are reached
    """

    def __init__(
        self,
        auth_mode: GeminiAuthMode = "api_key",
        default_model: str = "gemini-2.0-flash",
        api_key: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ):
        """
        Initialize GeminiExecutor.

        Args:
            auth_mode: "api_key" for Google AI Studio, "adc" for Vertex AI.
            default_model: Default model name.
            api_key: API key (required for api_key mode, ignored for adc).
            project: GCP project ID (for adc mode).
            location: GCP region (for adc mode, defaults to "us-central1").
        """
        if auth_mode == "api_key" and not api_key:
            raise ValueError(
                "api_key is required when auth_mode='api_key'. "
                "Set GEMINI_API_KEY or pass api_key explicitly."
            )
        self.auth_mode = auth_mode
        self.default_model = default_model
        self.api_key = api_key
        self.project = project
        self.location = location or "us-central1"
        self.logger = logger
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazily initialize the google-genai Client."""
        if self._client is None:
            from google import genai

            if self.auth_mode == "adc":
                self._client = genai.Client(
                    vertexai=True,
                    project=self.project,
                    location=self.location,
                )
            else:
                self._client = genai.Client(api_key=self.api_key)
        return self._client

    @property
    def provider_name(self) -> str:
        """Return the provider name."""
        return "gemini"

    def _convert_tools(self, tools: list[ToolSchema]) -> Any:
        """
        Convert ToolSchema list to google-genai tool declarations.

        Returns a list with a single genai.types.Tool containing all
        function declarations.
        """
        from google.genai import types

        declarations = []
        for tool in tools:
            # Build parameter schema — genai expects OpenAPI-style schema
            params = tool.input_schema.copy()
            if "type" not in params:
                params["type"] = "object"
            # Remove additionalProperties if present — Gemini doesn't support it
            params.pop("additionalProperties", None)

            declarations.append(
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters=params,
                )
            )

        return [types.Tool(function_declarations=declarations)]

    async def run(
        self,
        prompt: str,
        tools: list[ToolSchema],
        tool_handler: ToolHandler,
        system_prompt: str | None = None,
        model: str | None = None,
        max_turns: int = 10,
        timeout: float = 120.0,
    ) -> AgentResult:
        """Execute an agentic loop with Gemini function calling."""
        effective_model = model or self.default_model
        tool_calls_records: list[ToolCallRecord] = []
        cost_tracker = CostInfo(model=effective_model)
        turns_counter = [0]

        async def _run_loop() -> AgentResult:
            from google.genai import types

            client = self._get_client()
            turns_used = 0
            final_output = ""

            # Convert tools
            genai_tools = self._convert_tools(tools) if tools else None

            # Build config
            config = types.GenerateContentConfig(
                system_instruction=system_prompt or "You are a helpful assistant.",
                tools=genai_tools,
            )

            # Build initial contents
            contents: list[types.Content] = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)],
                )
            ]

            while turns_used < max_turns:
                turns_used += 1
                turns_counter[0] = turns_used

                try:
                    response = await client.aio.models.generate_content(
                        model=effective_model,
                        contents=contents,
                        config=config,
                    )
                except Exception as e:
                    self.logger.error(f"Gemini API error: {e}")
                    return AgentResult(
                        output="",
                        status="error",
                        tool_calls=tool_calls_records,
                        error=f"Gemini API error: {e}",
                        turns_used=turns_used,
                        cost_info=cost_tracker,
                    )

                # Track token usage
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    meta = response.usage_metadata
                    cost_tracker.prompt_tokens += getattr(meta, "prompt_token_count", 0) or 0
                    cost_tracker.completion_tokens += (
                        getattr(meta, "candidates_token_count", 0) or 0
                    )

                # Check for function calls in response
                candidate = response.candidates[0] if response.candidates else None
                if candidate is None:
                    return AgentResult(
                        output="",
                        status="error",
                        tool_calls=tool_calls_records,
                        error="No candidates in Gemini response",
                        turns_used=turns_used,
                        cost_info=cost_tracker,
                    )

                parts = candidate.content.parts if candidate.content else []
                function_calls = [
                    p for p in parts if hasattr(p, "function_call") and p.function_call
                ]

                # Extract text content
                text_parts = [p.text for p in parts if hasattr(p, "text") and p.text]
                if text_parts:
                    final_output = "\n".join(text_parts)

                # If no function calls, we're done
                if not function_calls:
                    cost_tracker.total_cost = calculate_cost(
                        effective_model,
                        cost_tracker.prompt_tokens,
                        cost_tracker.completion_tokens,
                    )
                    return AgentResult(
                        output=final_output,
                        status="success",
                        tool_calls=tool_calls_records,
                        turns_used=turns_used,
                        cost_info=cost_tracker,
                    )

                # Add assistant response to contents
                contents.append(candidate.content)

                # Execute all function calls and build response parts
                response_parts: list[types.Part] = []
                for fc_part in function_calls:
                    fc = fc_part.function_call
                    fn_name = fc.name
                    fn_args = dict(fc.args) if fc.args else {}

                    # Record the call
                    record = ToolCallRecord(tool_name=fn_name, arguments=fn_args)
                    tool_calls_records.append(record)

                    # Execute via handler
                    try:
                        result = await tool_handler(fn_name, fn_args)
                        record.result = result

                        if result.success:
                            content = (
                                json.dumps(result.result)
                                if result.result is not None
                                else "Success"
                            )
                        else:
                            content = f"Error: {result.error}"
                    except Exception as e:
                        self.logger.error(f"Tool handler error for {fn_name}: {e}")
                        record.result = ToolResult(tool_name=fn_name, success=False, error=str(e))
                        content = f"Error: {e}"

                    response_parts.append(
                        types.Part.from_function_response(
                            name=fn_name,
                            response={"result": content},
                        )
                    )

                # All function responses go in a single user Content message
                contents.append(types.Content(role="user", parts=response_parts))

            # Max turns reached
            cost_tracker.total_cost = calculate_cost(
                effective_model,
                cost_tracker.prompt_tokens,
                cost_tracker.completion_tokens,
            )
            return AgentResult(
                output=final_output,
                status="partial",
                tool_calls=tool_calls_records,
                turns_used=turns_used,
                cost_info=cost_tracker,
            )

        # Run with timeout
        try:
            return await asyncio.wait_for(_run_loop(), timeout=timeout)
        except TimeoutError:
            cost_tracker.total_cost = calculate_cost(
                effective_model,
                cost_tracker.prompt_tokens,
                cost_tracker.completion_tokens,
            )
            return AgentResult(
                output="",
                status="timeout",
                tool_calls=tool_calls_records,
                error=f"Execution timed out after {timeout}s",
                turns_used=turns_counter[0],
                cost_info=cost_tracker,
            )
