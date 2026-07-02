# Review: llm + providers + prompts + wiki

- **Scope:** `src/gobby/llm/`, `src/gobby/providers/`, `src/gobby/prompts/`, `src/gobby/wiki/` — plus the cross-cutting wiring into `src/gobby/ai/` (text-generation routing, capability registry), `src/gobby/runner_init/`, `src/gobby/tasks/expansion/`, and the scheduler/cron path that drives wiki jobs.
- **Reviewer:** Claude Fable 5 (synthesizer over 6 parallel Fable 5 sub-reviewers; every Blocker re-verified against source by the synthesizer)
- **Commit / branch:** b17e2e2e9 / 0.5.0
- **Summary:** 6 Blocker · 22 Important · 24 Nit — the provider stack is structurally sound but leaks failures as in-band success at every adapter seam, and the wiki cron/watcher wiring turns single bad rows or files into silent permanent outages. The single highest-value fix is a whitespace/empty guard plus typed error propagation in the candidate fallback loop; it subsumes four separate empty-success findings.

## Findings

### [BLOCKER] One bad audio binding collapses the whole AI capability registry → `llm_service = None` → every LLM feature dies

- **Where:** `src/gobby/ai/registry.py:259-263` (`register` raises on duplicate provider), `:245-251` (`__init__` loops `self.register(binding)` — all-or-nothing), `:359-394` (`build_daemon_ai_capability_registry`), `:508-518` (`_audio_bindings` from user voice config); `src/gobby/config/voice.py:8-39` (no uniqueness validation); `src/gobby/runner_init/services.py:43-49` (`_init_llm_service` broad `except Exception` → `llm_service = None`); `src/gobby/servers/routes/llm.py:72,82` (registry rebuilt per-request outside any try)
- **Failure mode:** A `voice.openai_compatible_audio` entry with a duplicate `provider`, a reserved name, or two case-variants ("Speaches"/"speaches" — `_normalize_provider` lowercases) makes `AICapabilityRegistry(bindings)` raise `ValueError("Duplicate audio_transcribe binding ...")` during construction. `_init_llm_service` catches it and leaves `runner.llm_service = None`, so session summaries, digest, memory KG/dream, task expansion/validation, tool recommendation, MCP import, and merge resolution all silently no-op for the daemon's lifetime. The HTTP routes rebuild the registry per request *outside* the try and return 500. An audio-config typo takes down text generation.
- **Why it matters:** Cross-cutting blast radius from one unrelated capability row, signalled only by a single startup log line. No test covers the duplicate-binding construction path (`tests/ai/test_capability_registry.py` exercises only valid configs).
- **Minimal fix:** In `build_daemon_ai_capability_registry`, dedupe/skip colliding bindings with a logged warning (or make `register` replace-and-warn for config-sourced bindings); add a pydantic validator on `VoiceConfig.openai_compatible_audio` rejecting duplicate/reserved provider ids; add a construction test.
- **Confidence:** high — verified `register` raise, the `__init__` register loop, and the `services.py` swallow-to-None end to end.

### [BLOCKER] ACP/local provider errors surface as successful empty text and halt the candidate fallback chain

- **Where:** `src/gobby/ai/text_generation.py:682-694` (`_collect_acp_text` collects only `content_delta`/`result`, drops `error` events), `:182-198` (`_try_generate_result_candidates` treats any non-exception return — including `""` — as terminal success); `src/gobby/adapters/acp_client.py:617-625,726-731` (errors yielded as `StreamEvent("error")`, never raised); `src/gobby/llm/local.py:212` (`response.choices[0].message.content or ""`)
- **Failure mode:** For gemini/grok/qwen candidates a JSON-RPC error is normalized to an `event_type="error"` event; `_collect_acp_text` returns `""`. `_try_generate_result_candidates` coerces it to `LLMTextResult(text="")`, logs `success=True`, and returns — candidate B is never tried, and `LLMService.call_feature` hands `""` back as success. The local path has the same shape (`content or ""`). Some callers self-defend (`sessions/summarize.py` rejects empty) but `tool_summarizer`, recommendation, importer, pipeline prompt steps, and merge resolver accept `""` as a real generation.
- **Why it matters:** Success-while-contract-violated: a provider failure both produces wrong output and suppresses the documented profile fallback — the exact disease the candidate chain exists to prevent. Confirmed recurrence of the `docs/reviews/search-index-ai.md` Blocker; commit 98d8bb4e9 ("tighten text generation routing") did not fix it.
- **Minimal fix:** In `_collect_acp_text`, `raise RuntimeError(event.data["message"])` on `event_type == "error"`; belt-and-suspenders, treat whitespace-only `text_result.text` as a candidate failure in `_try_generate_result_candidates` so the chain advances. The single empty-guard fix subsumes the Claude, Codex, and local variants below.
- **Confidence:** high — verified the in-scope `local.py:212` piece and the `text_generation.py` mechanism (the latter also re-confirmed during #15789).

### [BLOCKER] `generate_text` returns an empty string as success when the Claude SDK yields no text, halting fallback

- **Where:** `src/gobby/llm/claude.py:484-488` (also the `describe_image` inner query)
- **Failure mode:** In `_run_query`, `message_count == 0` and "messages but no text content" are only `logger.warning`s; `result_text` (`""`) is returned as a normal result, so `generate_text` returns `LLMTextResult(text="")`. The fallback orchestrator treats any non-exception return as success and stops — no further candidate is tried, and the calling feature (title synthesis, summaries) receives empty text as a successful generation. `_generate_json_sdk` does this correctly (`:610` raises `ValueError` on empty), proving the intended contract is raise-on-empty.
- **Why it matters:** A degraded SDK turn (max_turns exhausted with only tool-use blocks, or a swallowed stream error) silently produces empty output, defeats multi-provider fallback, and persists empty artifacts.
- **Minimal fix:** In `_run_query` and the `describe_image` equivalent, raise `RuntimeError`/`ClaudeSDKProviderFailure` when `message_count == 0` or `not result_text`, mirroring `_generate_json_sdk`; add a test for the zero-messages path.
- **Confidence:** high — full path verified provider → adapter → fallback loop.

### [BLOCKER] `describe_image` converts every failure into a success string that reaches the HTTP API as a 200 "description"

- **Where:** `src/gobby/llm/claude.py:693,667,675,751` → `src/gobby/ai/vision.py:92-97,108-113` → `src/gobby/servers/routes/llm.py:143-160`
- **Failure mode:** `_describe_image_sdk` returns literal strings — `"Image description unavailable (Claude CLI not found)"`, `"Image not found: ..."`, `"Failed to read image: ..."`, and `f"Image description failed: {e}"` (`:751`, an `except RuntimeError` that also swallows `ClaudeSDKProviderFailure`/`ClaudeSDKShutdownCancellation`, both RuntimeError subclasses) as the function's normal return. `extract_vision` ships it verbatim as `{"text": ..., "description": ...}` with HTTP 200; the route's 400/500 handlers are unreachable because the provider never raises. `tests/llm/test_claude.py:550` codifies the fake-success contract.
- **Why it matters:** API consumers cannot distinguish a real description from an error sentence; error text is persisted/displayed as image content.
- **Minimal fix:** Raise from `_describe_image_sdk`/`_prepare_image_data` failure branches and let `extract_vision`'s existing handlers map to 400/500; update the test. The local-provider equivalent (`local.py:296-302,339-341`) has the same defect at IMPORTANT severity (no HTTP-200 path proven) — fix together.
- **Confidence:** high — full path verified to the HTTP route.

### [BLOCKER] A symlinked file in a watched wiki root kills the watcher task permanently and silently

- **Where:** `src/gobby/wiki/watcher.py:200-201` (`_ignored` does `path.expanduser().resolve().relative_to(scope.root)`), `:181-186` (`_snapshot` per-path `except OSError`), `:140-145` (`_scan_once` catches only `(OSError, RuntimeError)`); `src/gobby/runner_lifecycle_periodic.py:221-223` (task created with no done-callback); `src/gobby/runner_lifecycle_shutdown.py:286-293` (dead tasks' exceptions never retrieved)
- **Failure mode:** A symlink inside the root whose resolved target is outside the root makes `relative_to` raise `ValueError`. The per-path try catches only `OSError`; `_scan_once` catches only `(OSError, RuntimeError)`; neither catches `ValueError`, so it escapes `run()`, the wiki-watcher task dies, nothing logs it, and local-edit indexing stops for the daemon's lifetime. The team anticipated this exception elsewhere (`record_change` callers catch `ValueError` at `:160`) but not on the snapshot path.
- **Why it matters:** One user-created symlink permanently disables local-change indexing with zero diagnostics (only `health()["running"] == False` if someone looks).
- **Minimal fix:** Compute the relative path without resolving (`path.relative_to(scope.root)` works for rglob results) or add `ValueError` to the excepts at `:130,145,185`; add a done-callback on the watcher task that logs unexpected exit.
- **Confidence:** high — verified `_ignored` resolves-then-relative_to and that the two enclosing excepts omit `ValueError`.

### [BLOCKER] gwiki timeouts are recorded as completed cron runs — failure counters reset, no backoff, no surfaced error

- **Where:** `src/gobby/gwiki_gateway.py:300-303,370-381` (timeout returns an `ok: false` envelope instead of raising); `src/gobby/wiki/scheduled_jobs.py:69-83,94-103,131-145` (handlers return `_history_output(...)` regardless of `ok`); `src/gobby/scheduler/executor.py:81-89` (any non-raising handler → `status="completed"`, `error=None`); `src/gobby/scheduler/scheduler.py:204-211` (completed → `consecutive_failures=0`)
- **Failure mode:** When `gwiki research/refresh/audit/index` exceeds the 30s default timeout (`_create_gateway` at `scheduled_jobs.py:250-264` sets no override; compare `codewiki_trigger.py:89` using 120s for similar work) the process is killed and the handler returns a JSON string whose `status` is "degraded" buried in run output. The run is marked completed, counters reset, backoff never engages. Nightly research/audit (LLM-driven, realistically >30s) can fail every day forever while the scheduler reports success.
- **Why it matters:** Success-while-contract-violated: scheduled wiki maintenance silently never happens; the only failure signal is inside an output blob nobody alarms on.
- **Minimal fix:** In the wiki handlers (or `_history_output`) raise when `result.get("ok") is False` / status is `failed|degraded` so the executor records a failed run; pass a larger `timeout_seconds` for research/refresh/audit gateways.
- **Confidence:** high — verified the handlers return regardless of `ok`; the executor's completed-on-no-raise behavior is the scheduler contract.

### [IMPORTANT] Candidate parsing diverges between config validation (`partition`) and runtime (`rpartition`) — `org/model` ids validate but are unreachable

- **Where:** `src/gobby/ai/text_generation.py:640-644` (`_parse_candidate` uses `rpartition("/")`) vs `src/gobby/config/feature_base.py:86-96` and `src/gobby/ai/registry.py:397-413` (both `partition("/")`)
- **Failure mode:** `local:lm-studio/qwen/qwen3-coder-30b` passes config validation as provider `local:lm-studio` / model `qwen/qwen3-coder-30b`, and the registry registers it under `local:lm-studio` — but `_parse_candidate` rpartitions to provider `local:lm-studio/qwen`, which has no binding, so selection fails on every call. LM Studio / HF model ids routinely contain `/`. `docs/guides/llm-features.md:38-42` documents `local:<endpoint>/<model>` with no slash restriction. Fallback continues, so it degrades silently rather than failing outright. Confirmed still present at HEAD (the prior #15789 seed); commit 1922f2d4a fixed bare-`local` resolution but not this.
- **Minimal fix:** Change `_parse_candidate` to `partition("/")` (provider ids cannot contain `/`); add a test with a slashed model id.
- **Confidence:** high — three independent reviewers reproduced it.

### [IMPORTANT] Multi-candidate fallback swallows `LLMProviderCancellation`, breaking graceful shutdown and spawning providers mid-shutdown

- **Where:** `src/gobby/ai/text_generation.py:199-209` (`except Exception` + `continue`), `:138-144` (`generate_result` re-raise only when `len(candidates) == 1`); consumer at `src/gobby/memory/digest.py:704`
- **Failure mode:** `ClaudeSDKShutdownCancellation` (subclass of `LLMProviderCancellation(RuntimeError)`) is caught by the candidate loop's `except Exception`, so routing tries the remaining candidates (codex app-server spawn, local HTTP) during daemon shutdown. With >1 candidate (all default profiles have 3), the final raise is the wrapper `RuntimeError`, so `except LLMProviderCancellation` at `digest.py:704` misses it — exactly what commit 788e9b088 (#15426) was meant to prevent.
- **Minimal fix:** Re-raise `LLMProviderCancellation` immediately in both candidate loops (`except LLMProviderCancellation: raise` before `except Exception`).
- **Confidence:** high.

### [IMPORTANT] Provider availability is frozen at daemon startup (TOCTOU) for all feature calls

- **Where:** `src/gobby/ai/registry.py:365,577,776` (`installed()` evaluated once at build), `:309-342` (`select` reads only the frozen `binding.available`); `src/gobby/runner_init/services.py:46` (service built once)
- **Failure mode:** `CapabilityBinding.available` is computed via `shutil.which` once at startup. Installing claude/codex/droid after startup leaves bindings unavailable until restart — `select()` raises "CLI is not installed" forever. Asymmetrically, `/api/llm/generate` rebuilds the registry per request, so the route works while daemon-resident features fail for the identical request.
- **Minimal fix:** Re-probe `installed()` at selection time for CLI-style bindings (short-TTL cache), or rebuild the registry on a heartbeat as the HTTP route already does.
- **Confidence:** high (mechanism), medium (operational impact).

### [IMPORTANT] Codex text-generate path has no turn-failure handling and no deadline — a feature call can hang forever

- **Where:** `src/gobby/ai/text_generation.py:466-488` (no timeout around `run_turn`); `src/gobby/adapters/codex_impl/client.py:648-675` (`run_turn` subscribes only `turn/started|completed`, `item/*`; exits only on `turn/completed`, polling 0.1s)
- **Failure mode:** If a Codex turn ends without `turn/completed` (turn failure/error notification, app-server wedge after `start_turn` succeeded), the loop spins forever and the adapter awaits forever — no adapter- or service-level deadline (contrast Droid's 600s and ACP's per-line timeout). The chat backend hedges by also subscribing `thread/closed`; the text-generate path does not. Codex is the first-position default candidate for every profile, so one wedged turn stalls summaries/expansion with no error and fallback never fires.
- **Minimal fix:** Wrap the adapter turn in `asyncio.wait_for(..., generation_timeout)` and subscribe `thread/closed`/turn-failure notifications in `run_turn` (or set `turn_completed` on stream EOF).
- **Confidence:** medium — missing deadline is fact; indefinite hang requires a failed/stalled turn, which the unregistered failure events make likely.

### [IMPORTANT] No timeout on the local `AsyncOpenAI` client; a wedged server stalls feature calls ~10 min/attempt, doubled by indiscriminate JSON retry

- **Where:** `src/gobby/llm/local.py:124-127` (client built with no `timeout`/`max_retries`), `:243-262` (`generate_json` catch-all retry)
- **Failure mode:** openai 2.15.0 defaults (verified in-env) are `Timeout(read=600...)`, `max_retries=2`. A hung LM Studio (server up, model wedged) blocks `generate_text_result` up to ~600s × 3 before fallback fires. `generate_json` catches *any* exception from the structured-mode call (timeouts, connection, auth — not just `response_format` rejection) and re-runs the whole completion without `response_format`, doubling the stall. The docstring promises a clean fallback "when the local server is down" — only connection-refused is fast.
- **Minimal fix:** Pass an explicit short `timeout` (e.g. `httpx.Timeout(120.0, connect=5.0)`, config-driven) and `max_retries=0/1`; narrow the `generate_json` retry to `openai.BadRequestError` only.
- **Confidence:** high.

### [IMPORTANT] Path traversal: task text steers arbitrary file reads into the privileged expansion LLM prompt

- **Where:** `src/gobby/tasks/expansion/_compile.py:457` (`normalized = file_path.lstrip("./")`), `:459,472` (`repo_path / normalized`); paths from `extract_mentioned_files` (`src/gobby/tasks/commits.py:391`) over `task.title`/`description`/`validation_criteria`
- **Failure mode:** `lstrip("./")` strips only *leading* `.`/`/`, so a path that does not start with `./` keeps mid-path `..`. Verified repro: a task description containing `src/../../../../../../../etc/ssh/sshd_config` survives the extractor and `lstrip` unchanged; `repo_path / that` resolves outside the repo, passes `.exists()`/`.is_file()`, and the first 3500 chars are concatenated into the expansion prompt. No `resolve()`-containment check anywhere in the chain.
- **Why it matters:** External/less-privileged task text (MCP-created tasks, issue imports, spawned-agent tasks) can read local files matching the default extension set and exfiltrate them through expansion output.
- **Minimal fix:** Resolve and require containment: `absolute = (repo_path / normalized).resolve(); if not absolute.is_relative_to(repo_path.resolve()): continue`; drop the `lstrip("./")` crutch.
- **Confidence:** high (mechanism reproduced), medium (exploitability depends on untrusted task sources).

### [IMPORTANT] Systemic prompt-injection surface: user/external text interpolated raw into privileged LLM prompts

- **Where:** `install/shared/prompts/memory/turn_record.md:10,13` (transcript user+agent content), `memory/fact_extraction.md:22`, `expansion/user.md:31,34` (task description + repo file contents), `validation/validate.md:35-43` (task + diff + file content), `features/tool_summary.md:15` and `server_description.md:18` (third-party MCP tool/server descriptions — fully attacker-controlled if a hostile MCP server is added)
- **Failure mode:** None of these delimit/escape the interpolated value. Jinja renders once, so template injection is not possible, but *semantic* prompt injection is: embedded `## Instructions` / "ignore previous instructions / return {…}" payloads can steer JSON-producing, memory-writing, and task-expanding calls. Same shape as the code_index summarizer seed.
- **Minimal fix:** Wrap untrusted spans in explicit delimiters (`<untrusted_content>…</untrusted_content>`) with a system-prompt note to treat enclosed text as data; apply consistently across these templates.
- **Confidence:** high (surfaces exist), medium (impact varies by call privilege).

### [IMPORTANT] PromptLoader fallback emits malformed prompts and swallows all template errors

- **Where:** `src/gobby/prompts/loader.py:139-171` (`_render_jinja` `except UndefinedError`→`:158`, `except Exception`→`:164`; `_render_simple` `:166-171`)
- **Failure mode:** On any Jinja error `_render_jinja` falls back to `_render_simple`, which does `template_str.format(**context)` on a *Jinja* template. Verified repro: a template containing `{% set x = 1 %}` raises `KeyError '% set x = 1 %'` and the raw template with `{{ }}`/`{% %}` is returned; `{{ task_id }}` becomes the literal `{ task_id }` (no substitution). `strict=False` is the only mode any caller uses, so `validate_context` is dead and a missing variable degrades silently to a malformed prompt instead of raising.
- **Minimal fix:** Drop `_render_simple` (or make it a true passthrough) and let `_render_jinja` raise; stop catching `UndefinedError`/`Exception`. Add a test that a missing variable raises.
- **Confidence:** high (defect verified), medium (current callers pass full context, so trigger is rare today).

### [IMPORTANT] Custom `default` filter override breaks Jinja semantics (latent landmine)

- **Where:** `src/gobby/prompts/loader.py:150` (`env.filters["default"] = lambda v, d="": d if v is None else v`)
- **Failure mode:** Shadows Jinja's built-in `default` with a strictly worse one under `StrictUndefined`. Verified repros: `{{ x | default('y') }}` on undefined `x` raises `UndefinedError` (builtin returns `'y'`); the 3-arg boolean form `{{ x | default('d', true) }}` raises `TypeError: <lambda>() takes from 1 to 2 positional arguments but 3 were given`. Either error routes into the broken `_render_simple` fallback above.
- **Minimal fix:** Delete line 150 and rely on Jinja's built-in `default` (which already honors `StrictUndefined` and the boolean arg).
- **Confidence:** high (defect verified), impact latent.

### [IMPORTANT] `resolver.py` is a dead module whose documented contract is unimplemented and whose tables have drifted from runtime

- **Where:** `src/gobby/llm/resolver.py:25,31,72,133-191`
- **Failure mode:** `resolve_provider`/`validate_provider_name` have zero production callers (verified repo-wide; the `_resolve_provider` hits in `spawn_agent/_factory.py:418` are an unrelated local function); the only production import is `ProviderError` in `memory/dream/planner.py:10` (caught but never raised). The docstring promises `ProviderNotConfiguredError` on availability failure but the function never checks availability; `allow_unconfigured`/`config` are accepted and never read; `MissingProviderError` is unreachable; `ResolutionSource` includes `"config"` which is never produced. `SUPPORTED_PROVIDERS` (claude/codex/gemini/local) has drifted from the real runtime set (chat ingress: claude/codex/gemini/grok/qwen/droid/agy), and `PROVIDER_NAME_PATTERN` rejects the documented `local:<endpoint>` form. Real provider resolution lives in `ai/registry.py` + `config/feature_base.py`.
- **Minimal fix:** Delete `resolver.py` (moving `ProviderError` to where the planner needs it) or make it honest — drop the unread params, unreachable exceptions, and drifted tables.
- **Confidence:** high — all symbols searched repo-wide.

### [IMPORTANT] Stale Sonnet 4.6 context window: static table says 200K, vendor catalog says 1M

- **Where:** `src/gobby/llm/context_windows.py:75,83` (`"sonnet": 200_000`, `"claude-sonnet-4-6": 200_000` in `_STATIC_CONTEXT_LENGTHS`)
- **Failure mode:** Per the current Anthropic catalog (claude-api skill, cached 2026-05-26), Sonnet 4.6 has a 1M window. The last-resort static fallback returns 200_000 — a 5× under-report — whenever provider/catalog/registry data is unavailable (DB down, fresh install before OpenRouter fetch, provider that doesn't report usage). Tests pin the stale value (`tests/llm/test_context_window.py:121,137,156,313`), so the drift is enforced. Downstream this skews `ContextUsageSnapshot.calculate_ratio`, triggering context-pressure/compaction at 1/5 of real capacity.
- **Minimal fix:** Update `"sonnet"` and `"claude-sonnet-4-6"` to `1_000_000` (leave `claude-sonnet-4-5` at 200K — its 1M was a marked tier; leave droid rows, which are documented as provider-owned); update the pinned tests.
- **Confidence:** high on the discrepancy (two vendor tables agree), medium on static-path frequency.

### [IMPORTANT] The `[1m]` long-context marker is stripped, then the base-tier window is returned

- **Where:** `src/gobby/llm/context_windows.py:57-63,196-204,207-211` (marker regex + `strip_context_window_marker_suffix` + `normalize_model_lookup_id`)
- **Failure mode:** The in-file comment says the marker "selects the 1M tier", but normalization strips it and resolves the *base* model's window: `claude-sonnet-4-5[1m]` → `claude-sonnet-4-5` → 200_000. It happens to work for opus/fable because those family values are already 1M; the only test covers that masked case (`claude-opus-4-8[1m]` → 1M) — there is no sonnet/haiku `[1m]` test, i.e. no test of the case where stripping changes the answer. The same flows through the `model_costs` prefix LIKE-match.
- **Minimal fix:** In `_lookup_context_length`, detect a stripped 1M marker and floor the resolved value at `1_000_000` (or look up a `<family>-1m` key first); add a sonnet-tier `[1m]` test.
- **Confidence:** high (mechanical), medium real-world frequency.

### [IMPORTANT] Registry can return a context window of `0`, which shadows correct fallbacks

- **Where:** `src/gobby/llm/model_registry.py:97` (`context_length = entry.get("context_length") or 0`) → `src/gobby/storage/model_costs.py:104-111` (prefix branch checks only `IS NOT NULL`) → `src/gobby/llm/context_windows.py:443-446` (`is not None`)
- **Failure mode:** An OpenRouter entry with missing/zero `context_length` is stored as `0` (verified `or 0` at `model_registry.py:97`). The exact-match guard is truthy (`model_costs.py:101`) but the prefix branch returns `int(0)`, and `_registry_context_window`'s `is not None` lets `ResolvedContextWindow(0, "registry")` win, so the correct static fallback is never reached. Downstream `calculate_ratio` nulls out on `context_window <= 0`, silently disabling context-pressure tracking. `context_length` is also never type-validated.
- **Minimal fix:** In `fetch_models_sync`, skip entries without a positive int `context_length` (or store NULL); add `AND context_length > 0` to the prefix SQL and `registry_val > 0` at `context_windows.py:445`.
- **Confidence:** high on the code path, medium on OpenRouter emitting such entries.

### [IMPORTANT] DB failures propagate uncaught through context-window resolution

- **Where:** `src/gobby/llm/model_registry.py:120-124,127-137` (app-context `try` catches only `(ImportError, AttributeError)`); `src/gobby/llm/context_windows.py:440-447`
- **Failure mode:** A psycopg/connection error from `store.get_context_window` escapes the narrow except, and `_registry_context_window`/`resolve_context_window_with_source` add no handling, so a degraded Postgres turns every context-window resolution (chat backends, session hydration) into an exception instead of a graceful `None`. The catalog path deliberately degrades with debug logs — inconsistent posture.
- **Minimal fix:** Catch DB errors in `lookup_context_window` (both branches), log at debug/warning, return `None` so resolution falls through to static defaults.
- **Confidence:** medium — call path verified; assumes `HubDatabase.fetchone` propagates connection errors.

### [IMPORTANT] CLI-not-found at daemon start is sticky for the daemon's lifetime

- **Where:** `src/gobby/llm/claude_cli.py:61-88` (`verify_cli_path`); `src/gobby/llm/claude.py:133,143-149`
- **Failure mode:** `verify_cli_path` only enters its `shutil.which` retry block when `cached_path` is truthy and the file disappeared; when `cached_path is None` it returns `None` immediately (verified). So if the daemon starts before Claude Code is installed (or PATH was wrong at boot), every `generate_text/json/describe_image` raises "Generation unavailable" forever — even after the user installs the CLI — until restart. The docstring claims it handles re-discovery, but only for the reinstall-race case.
- **Minimal fix:** When `cached_path` is None, attempt `shutil.which("claude")` once before returning None.
- **Confidence:** high.

### [IMPORTANT] `parse_stream` crashes with `ValueError` on NDJSON lines >64 KiB

- **Where:** `src/gobby/llm/stream_json_parser.py:172-176`; subprocess created without `limit=` at `src/gobby/llm/claude_cli.py:173-179`
- **Failure mode:** Verified by repro: a 100 KB line through a default-limit `StreamReader` raises `ValueError: Separator is found, but chunk is longer than limit` out of `readline()`; the generator dies and the stream is unrecoverable. Claude CLI `--verbose --output-format stream-json` full-message/result lines exceed 64 KiB on long responses or large tool output. Latent today — `CLISession`/`parse_stream` have zero production callers (only `find_cli_path`/`verify_cli_path` are imported from this module) — so it becomes a Blocker the moment chat wiring lands.
- **Minimal fix:** Pass `limit=10 * 1024 * 1024` to `create_subprocess_exec`, and catch `ValueError` in `parse_stream` to skip oversized lines.
- **Confidence:** high.

### [IMPORTANT] `_classify_event` crashes on valid-JSON-but-unexpected shapes; exceptions escape `parse_stream` despite its "skip malformed" contract

- **Where:** `src/gobby/llm/stream_json_parser.py:84-155` (esp. `:96-98,106-108,133,141-150`), decode at `:176`, only `json.JSONDecodeError` caught at `:179-182`
- **Failure mode:** Reviewer verified four crash modes by repro: non-object JSON (`123`) → `AttributeError`; `input_tokens:"abc"` → `ValueError` from `int()`; string `content` → `AttributeError`; `retry_after:null` → `TypeError` from `float()`. A string `error` field crashes `error.get` the same way; `raw_line.decode()` raises on invalid UTF-8. Everything but `JSONDecodeError` kills the generator and its consuming session.
- **Minimal fix:** Guard `if not isinstance(data, dict): continue`, wrap `_classify_event` in `try/except Exception` that logs and yields a base `StreamEvent(raw=data)`, and use `decode(errors="replace")`.
- **Confidence:** high.

### [RESOLVED] `config ai.generation.profile_defaults` is ignored for profile-only requests

- **Resolution (#17527):** Profile overrides are now honored — `TextGenerationService._candidate_requests` consults `self._profile_defaults` before the hardcoded defaults (`src/gobby/ai/_text_generation_service.py:545`).
- **Where:** `src/gobby/ai/text_generation.py:286-294` (uses hardcoded `default_candidates_for_profile`); `src/gobby/config/feature_base.py:31-47` (the hardcoded table); `src/gobby/config/app.py:539-551` (`profile_defaults` applied only to feature configs); `src/gobby/servers/routes/llm.py:57-62`
- **Failure mode:** `TextGenerationService._candidate_requests` resolves a profile-only request via the hardcoded `DEFAULT_PROFILE_CANDIDATES`; the user's `ai.generation.profile_defaults` overrides (documented at `docs/guides/llm-features.md:29-36`) are threaded only into `FeatureDefaultConfig.candidates`. So `POST /api/llm/generate` with only a prompt (or an explicit `profile`) routes to cloud CLI candidates even when the operator pinned `feature_low` to local-only — silently defeating privacy/cost intent.
- **Minimal fix:** Pass `config.ai.generation.profile_defaults` into `TextGenerationService` and consult it before `default_candidates_for_profile`.
- **Confidence:** high.

### [IMPORTANT] CLISession spawns `claude` without `--print`, so its stream-json flags are inoperative (dead module, wrong at the subprocess boundary)

- **Where:** `src/gobby/llm/claude_cli.py:155-179` (cmd construction in `start()`)
- **Failure mode:** The command is `[path, "--output-format", "stream-json", "--verbose", "--input-format", "stream-json"]` with no `--print`. The installed CLI help states `--input-format`/`--output-format` "only works with --print"; without `-p` the binary starts an interactive session, so the NDJSON contract `send()` depends on is not honored. Never bites because the class has zero production callers and tests mock the subprocess entirely.
- **Minimal fix:** Add `"--print"` (and `--include-partial-messages` if deltas are wanted), or delete the module if web chat has permanently moved to the SDK path.
- **Confidence:** high — flag requirement verified via `claude --help`; no-callers verified repo-wide.

### [IMPORTANT] `CLISession.send()` can hang forever: terminates only on process EOF, no timeout, no break on result, no concurrency guard

- **Where:** `src/gobby/llm/claude_cli.py:181-205`; `src/gobby/llm/stream_json_parser.py:163-184`
- **Failure mode:** `send()` iterates `parse_stream(self._process.stdout)`, which ends only on EOF; in a multi-turn session the process stays alive after the turn's `result` event, so `readline()` blocks indefinitely. `send()` records `saw_result = True` but never breaks; there is no per-turn timeout and no lock preventing two concurrent `send()`s from interleaving reads. `interrupt()` calls `send_signal` unguarded (raises `ProcessLookupError` if the process already died; `stop()` guards this, `interrupt()` doesn't).
- **Minimal fix:** `break` after yielding a `ResultEvent`, wrap the read loop in `asyncio.timeout`, serialize sends with an `asyncio.Lock`, and guard `interrupt()` with `ProcessLookupError`.
- **Confidence:** high (mechanics), with "live process" as a runtime assumption.

### [IMPORTANT] Watcher flush drops pending changes and advances `last_index_time` even when the index handoff was degraded

- **Where:** `src/gobby/wiki/watcher.py:101-112` (`flush_pending` unconditionally clears flushed paths and sets `_last_index_time`), `:64,75-76` (return value ignored); `src/gobby/wiki/update_coordinator.py:100-128` (returns `status: "degraded"` on `GwikiCommandError`/`GwikiGatewayError` and skips remaining scopes after `failed_scope`)
- **Failure mode:** If `gwiki index` fails for a scope, the coordinator swallows the error into a degraded dict; `flush_pending` then removes every flushed path (including scopes never indexed because the loop returned early), stamps `_last_index_time = time.time()`, and the watcher discards the result. No log, no retry; the index stays stale while status reports a fresh `last_index_time`.
- **Minimal fix:** In `flush_pending`, inspect `result["index_handoff"]["status"]`; on `degraded`, log, do not clear paths for unindexed scopes, and don't update `_last_index_time`.
- **Confidence:** high.

### [IMPORTANT] Full filesystem scan runs synchronously on the event loop every poll tick

- **Where:** `src/gobby/wiki/watcher.py:144` (`current = self._snapshot(scope)` in async `_scan_once`), `:172-187` (`rglob` + `stat` over the whole root); contrast `:129` where init correctly uses `asyncio.to_thread`
- **Failure mode:** With default `poll_interval=0.25` (`config/wiki.py:41-44`), every wiki root is fully walked and stat'ed on the main event loop 4×/second, injecting repeated stalls into the loop serving HTTP/WS/MCP for large wikis. `record_change`/`_scope_for_path`/`_ignored` also do `resolve()` syscalls on the loop.
- **Minimal fix:** `current = await asyncio.to_thread(self._snapshot, scope)` in `_scan_once`.
- **Confidence:** high.

### [IMPORTANT] Wiki cron rows for non-current projects fire forever with "No handler registered"

- **Where:** `src/gobby/runner_lifecycle_subsystems.py:284-285,295-301` (handlers registered only for the startup project); `src/gobby/runner_init/services.py:242-249` (`project_id` = daemon CWD project); `src/gobby/storage/cron.py:583-603` (`get_due_jobs` global); `src/gobby/scheduler/executor.py:295-308` (missing handler → ValueError → failed run)
- **Failure mode:** `_ensure_wiki_cron_job` creates persistent enabled system rows per project. Start the daemon from a different directory and the old projects' rows remain enabled and due, but their handlers (`wiki:<cmd>:project:<old-id>`) are never registered — every dispatch fails on a 30-60 min cadence and those projects' maintenance never runs.
- **Minimal fix:** At startup, register handlers for all projects with wiki system cron rows, or park/disable rows whose scope's project isn't active (`reconcile_system_job_identity(enabled=False, next_run_at=None)`).
- **Confidence:** high.

### [IMPORTANT] Legacy non-system wiki row takeover crashes registration when disabled, and force re-enables user-disabled rows every startup

- **Where:** `src/gobby/wiki/scheduled_jobs.py:300-312` (`update_job(..., enabled=True)` with no `next_run_at`, row never marked system); `src/gobby/storage/cron.py:368-370` (`enabled=True requires next_run_at` → ValueError), `:549-581` (`toggle_job` sets `next_run_at=None` on disable)
- **Failure mode:** A disabled non-system row has `next_run_at=None`; the takeover `update_job(..., enabled=True)` raises ValueError, aborting `register_wiki_cron_jobs` mid-loop (later handlers never registered) and is swallowed by the broad except at `runner_lifecycle_subsystems.py:303-306`. If the row is enabled, the branch silently rewrites and re-enables the user's job every restart (never converted via `mark_as_system_job`).
- **Minimal fix:** Compute and pass `next_run_at`, preserve the existing `enabled`, and call `mark_as_system_job` so the takeover happens once.
- **Confidence:** high (code path), medium (prevalence of legacy rows).

### [IMPORTANT] `reconcile_stale_wiki_cron_scopes` calls a system-only reconcile on rows it never checks for `is_system`

- **Where:** `src/gobby/wiki/scheduled_jobs.py:367-379` (no `legacy.is_system` guard before `reconcile_system_job_identity`); `src/gobby/storage/cron.py:429-433` (raises `SystemRowProtected` for non-system rows); contrast `:330` which does check `is_system`
- **Failure mode:** A non-system legacy bare-scope row makes `reconcile_system_job_identity` raise `SystemRowProtected`. Since this runs (at `:162`) before any handler registration, the entire wiki cron registration aborts — zero handlers registered — reduced to one `logger.error` line (no `exc_info`). All wiki system rows then fail every interval with "No handler registered".
- **Minimal fix:** `if legacy is None or not legacy.is_system: continue` (matching `_retire_queryless_system_research_job`).
- **Confidence:** high.

### [IMPORTANT] No serialization between watcher-triggered and cron-triggered `gwiki index`; refresh re-triggers the watcher for a duplicate index

- **Where:** `src/gobby/wiki/update_coordinator.py:42-175` (no lock); separate coordinator instances at `scheduled_jobs.py:166` vs `runner_lifecycle_periodic.py:208-218`; `src/gobby/wiki/watcher.py:45` (default ignores only `outputs/**`, `meta/health/**` — refresh-written `raw/...` files are watched)
- **Failure mode:** Hourly `gwiki refresh` writes raw files inside the watched root; the coordinator indexes once, then ~0.5s later the watcher detects the same writes and launches a second scoped `gwiki index`, which can run concurrently with the cron handler's index. Scheduler overlap protection is per-job only; nothing serializes `gwiki index`/write subprocesses per scope.
- **Minimal fix:** Share one per-scope `asyncio.Lock` around gateway index/write calls in `WikiUpdateCoordinator`, and add refresh raw-output paths to the watcher's default ignore globs (or suppress watcher flushes for paths the coordinator just indexed).
- **Confidence:** medium (duplicate trigger high; concurrent-writer harm depends on gwiki internals).

### [IMPORTANT] Blocking image read + base64 of unbounded data on the event loop (both providers)

- **Where:** `src/gobby/llm/claude.py:670-671` (`path.read_bytes()` / `b64encode` in `_prepare_image_data`, awaited from async at `:696`); `src/gobby/llm/local.py:305-306`
- **Failure mode:** Both providers synchronously read the entire image and base64-encode it inside the daemon's event loop, with no size cap, buffering ~1.33× the file size before shipping it as prompt content. The HTTP vision route writes whatever was uploaded to a temp file, so a large upload stalls the loop for the duration of read+encode and degrades every concurrent session.
- **Minimal fix:** `await asyncio.to_thread(...)` for read+encode and enforce a max-size check (Anthropic vision caps are a few MB) before encoding.
- **Confidence:** high for the blocking call, medium on impact magnitude.

## Nits

- **claude.py `_retry_async` returns None when `max_retries=0`** (`:276-289`) — the `for attempt in range(max_retries)` body never runs and the function falls through to `None`, propagated as a "successful" result. Latent (callers pass 1/3); validate `max_retries >= 1`.
- **claude.py `_is_transient_error` matches substrings over the whole message** (`:163-181`) — "404"/"401"/"not_found" anywhere in embedded stderr/payload text flips a transient error to permanent (and vice versa). Prefer typed SDK exceptions/exit codes. Same brittleness in `_extract_exit_code_from_message` (`:214-225`) and `_is_error_result_success` (`:184-186`).
- **claude.py `max_tokens` emulated by char truncation** (`:492-494`) — `result[: max_tokens * 4]` chops mid-token after full generation with no marker; full spend still incurred, `usage` reflects the untruncated run.
- **claude.py `captured_usage` can carry usage from a failed retry attempt** (`:457-483`) — nonlocal across attempts; reset to `None` at the top of `_run_query`.
- **claude.py unbounded stderr accumulation per query** (`:325`) — `stderr_lines.append` grows without bound and is dumped wholesale into logs/exceptions; cap with a `deque(maxlen=...)`.
- **claude.py dead tool-enabled generation path was removed** — no production caller remains for hidden Claude tool use.
- **claude.py `ClaudeSDKProviderFailure` is a distinction nothing consumes** (`:102-103,368`) — raised once, never caught; the fallback loop catches bare `Exception`.
- **claude.py redundant double `_verify_cli_path` per call** (`:420/441`, `:551/568`) — public then private re-verification doubles worst-case retry latency.
- **local.py `__init__` swallows client construction failures** (`:134-137`) — `_client=None` leaves every later call raising a generic error instead of failing fast at construction where the factory would wrap it clearly.
- **local.py `describe_image` returns error sentinel strings as success** (`:296-302,339-341`) — consumed verbatim by `vision.py:108-113`; corrupt error text persists into memory image descriptions. (IMPORTANT-adjacent; grouped here as the Claude variant is the Blocker.)
- **local.py sync image read on the event loop** (`:305`) — `await asyncio.to_thread(path.read_bytes)`.
- **service.py `call_json_feature` cannot pass `max_tokens`; local JSON path hardcodes 8000** (`:112-132`, `local.py:243,262`) — the text path plumbs it end-to-end, the JSON path silently ignores it.
- **claude_cli.py `ResultEvent` drops `cost_usd` that tests feed it** (`stream_json_parser.py:136-144`) — unnoticed because nothing consumes it.
- **context_windows.py catalog candidates with source "registry" are silently discarded** (`:327-341`) — a legal `ContextLengthSource` member dropped entirely; treat like `static_default`.
- **context_windows.py provider gating asymmetry** (`:214-220,360-388`) — enumerated `claude-*` keys leak cross-provider while the family fallback doesn't; gate `key.startswith("claude-")` to `{None, "claude", "droid"}`.
- **context_windows.py empty-string override key matches every model** (`:306-309`) — `substr.lower() in model_lower` with `substr=""` pins every window; guard `if substr and ...`.
- **context_windows.py legacy duck-typed catalogs misclassified as authoritative** (`:421-425`) — a catalog exposing only `get_context_window` gets labeled `provider_reported`.
- **model_costs PK on stripped model id** (`postgres_baseline_schema.sql:1297-1304`) — two providers with the same suffix collide on the PK and abort the whole refresh (swallowed to a warning at `runner_init/storage.py:98-102`); `_` in stored keys acts as a LIKE wildcard. Use `(provider, model)` PK or `ON CONFLICT DO UPDATE`, and escape LIKE metacharacters.
- **resolver.py drops workflow `model` when set without `provider`** (`:172-185`) — dead today; the contract gap would surprise a future caller.
- **providers/registry.py dead public API** (`:90-92,104-110,113-123`, field `installed_only`) — `provider_ids`, `installed_provider_metadata`, `provider_status_metadata`, `get_provider_metadata` have zero callers outside the package; `provider_status_metadata` already omits `installed`/`path` with no consumer to notice.
- **wiki `scheduled_scopes` config plumbing reads fields on no config model** (`scheduled_jobs.py:229-239`) — `wiki_config.scheduled_scopes`/`config.wiki_scheduled_scopes` always return None (`WikiConfig` has no such field; `app.py:201` is `extra: "ignore"`), so multi-scope scheduled jobs are unconfigurable dead plumbing.
- **wiki watcher debounce not applied to changes recorded during an in-flight flush** (`watcher.py:82-85,108-110`) — stale `_pending_since` makes leftover paths flush on the next poll with no debounce.
- **wiki watcher `ignore_globs=[]` silently restores defaults** (`watcher.py:45`) — `ignore_globs or [...]`; use `is not None`.
- **wiki dead branch in `_ensure_wiki_cron_job`** (`scheduled_jobs.py:314`) — `if existing.is_system:` is always true after the preceding non-system return.
- **wiki registration failure logged without traceback; sync DB calls in async registration** (`runner_lifecycle_subsystems.py:303-304`, `scheduled_jobs.py:162,215-224,267-322`) — add `exc_info=True`; optionally `to_thread` the storage calls.
- **Doc drift:** `CLAUDE.md` "llm/" section lists `gemini.py` and `litellm.py` which do not exist on disk (litellm removal recorded in `docs/plans/completed/litellm-drawdown.md`) and calls `service.py` a multi-provider "manager" though it is a thin facade over `ai/text_generation`. `docs/guides/llm-features.md:8-12` profile table omits the third `local/<model>` candidate per profile; `:59-60` documents expansion/validation as `call_feature` but the code uses `call_json_feature` (`tasks/expansion/_compile.py:227`, `tasks/validation.py:708`).
- **Missing load-bearing tests:** sonnet/haiku `[1m]` marker; registry returning 0; catalog candidate with source `"registry"`; `describe_image` on the local provider (no coverage at all); the duplicate-audio-binding registry construction; the zero-messages `generate_text` path; the >64 KiB / unexpected-shape stream-parser cases.

## Systemic patterns

- **Errors converted to in-band success values, endemic at every adapter seam.** ACP (`_collect_acp_text` drops error events), Codex (`chunks or fallback_chunks`), local (`content or ""`), Claude SDK (empty-string return; describe_image error sentences), and the gwiki path (`ok:false`/`status:"degraded"` envelopes). The candidate loop and the cron executor both treat any non-exception as success, so the fallback/backoff guarantees are only as strong as the weakest adapter's error discipline. A single whitespace/empty guard in `_try_generate_result_candidates` plus typed-error raises at the adapters fixes the whole family. `_generate_json_sdk` is the one path that already does it right (raises on empty).
- **Work marked done before it succeeded.** Watcher clears pending paths and stamps `last_index_time` on degraded handoffs; cron runs are marked completed on timeout; `_scan_once` advances the snapshot before `record_change` succeeds.
- **All-or-nothing construction under a broad `except`.** The AI capability registry (one bad audio binding) and `register_wiki_cron_jobs` (one stale/legacy row) each abort entirely from a single bad input, and the enclosing `except Exception` reduces it to one log line (often without `exc_info`). Failures should be isolated per binding/row, not fatal to the subsystem.
- **Two parsers for one string format.** Config validation uses `partition`, runtime routing uses `rpartition`, so validation proves nothing about runtime reachability for slashed local model ids. Parsing belongs in one shared function.
- **Capability/availability frozen at startup.** Provider `installed()` and the Claude CLI path are probed once and never re-evaluated, so a long-lived daemon cannot recover from a transient startup condition, while the per-request HTTP route diverges from the resident service for the identical request.
- **Untrusted text flows undelimited into privileged LLM prompts.** Transcripts, task fields, repo file contents, and third-party MCP descriptions reach memory/expansion/validation/summary prompts with no delimiting or escaping — one shared mitigation (a delimiting helper) is missing across the template set. Path traversal in the expansion file-context builder compounds this by letting task text choose which file gets read in.
- **Inconsistent exception-class hygiene and timeout discipline.** Each layer catches a hand-picked tuple (`OSError`/`RuntimeError` here, `+ValueError` there) and the one path that can actually raise `ValueError` (`relative_to` in the watcher snapshot) is the one left uncovered; timeouts are per-adapter folklore (Droid 600s, ACP per-line, Codex none, local openai-default 600s×3) with no shared deadline at the routing layer where it belongs.
- **Token/usage accounting only survives the Claude and local paths.** `LLMTextResult.usage` is dropped at the `LLMService.call_feature` boundary and never reaches the structured `feature_llm_call` log; ACP/Codex/Droid adapters return bare `str`, so per-feature cost telemetry is impossible despite the plumbing existing on two of six adapters.
