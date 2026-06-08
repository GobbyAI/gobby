  # Quiet Feature LLM Success Logs

  ## Summary

  Stop successful feature_llm_call telemetry from appearing in default gobby.log output. Keep failed feature LLM calls visible so provider/model
  problems still surface during normal daemon operation.

  ## Key Changes

  - Before editing, create or claim a gobby-tasks task through MCP.
  - In src/gobby/ai/text_generation.py, change TextGenerationService._log_generation_event to log successful calls at DEBUG and failed calls at
    INFO.

  - Keep the existing event name and extra fields unchanged: feature, profile, provider, model, latency_ms, success, error, and json_parse_outcome.
  - Do not change telemetry formatter behavior and do not add a config toggle.

  ## Tests

  - Add focused tests in tests/ai/test_text_generation.py:
      - successful generation emits no feature_llm_call record at INFO;
      - successful generation still emits feature_llm_call at DEBUG;
      - failed generation still emits feature_llm_call at INFO with success=False.

  - Run targeted validation:
      - GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_text_generation.py -v
      - uv run ruff check src/gobby/ai/text_generation.py tests/ai/test_text_generation.py
      - uv run mypy src/gobby/ai/text_generation.py

  ## Assumptions

  - The noisy entries are successful feature LLM telemetry like success=True.
  - Default gobby.log should remain useful for actionable failures.
  - No public API, config schema, or log file path changes are needed.
  - After changes, commit with the task-linked format and close the task with the commit SHA.