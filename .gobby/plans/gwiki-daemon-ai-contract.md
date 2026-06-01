# gwiki daemon AI capability contract

**Plan ID:** gwiki-daemon-ai-contract

## O1: Overview

`kind: framing`

Implement the daemon-side AI and multimodal contract consumed by
`gobby-cli/.gobby/plans/gwiki-multimodal-ai.md` P6. This companion plan owns daemon
capability status, route response shapes, structured capability errors, and the internal daemon
adoption work needed for gwiki and gcode to consume one capability vocabulary.

The sibling `.gobby/plans/gwiki-daemon-web.md` remains focused on `gwiki` gateway, wiki API,
MCP, web-chat, watchers, update coordination, and scheduled wiki behavior.

## S1: Source Contract

`kind: framing`

This plan implements the daemon side of upstream `gobby-cli` Plan ID
`gwiki-multimodal-ai`, `## P6: Daemon capability-registry contract (sibling repo)`,
`### 6.1 Author the daemon capability contract (CLI side)`. It cross-references upstream
P6 D1-D5 only; this repo's deliverables and acceptance criteria below remain authoritative for
daemon files, route behavior, and tests.

- Voice status/transcribe route contract maps to upstream P6 D1. Local owners: P1.1, P1.2,
  and P3.1.
- Vision extract/status route contract maps to upstream P6 D2. Local owners: P2.2 and P3.1.
- Text generate/status route contract maps to upstream P6 D3. Local owners: P2.1, P3.1,
  and P4.1.
- `/api/providers/models` discovery-only behavior and status-route truth-source precedence map
  to upstream P6 D4. Local owners: C1, P1.1, P2.1, P2.2, and AC1.
- Additive daemon hub adoption that preserves existing `code_*` and `gwiki_*` data maps to
  upstream P6 D5. Local owner: P4.2.

## C1: Scope And Constraints

`kind: framing`

- **Capability vocabulary**: daemon routes use the canonical `AICapability` values
  `audio_transcribe`, `audio_translate`, `vision_extract`, and `text_generate`.
- **Status route truth**: `/api/voice/status`, `/api/llm/status`, and
  `/api/llm/vision/status` are the availability truth source. `/api/providers/models` remains
  provider/model discovery only.
- **Backward compatibility**: existing successful response fields remain present. New fields are
  additive unless the route already fails with an HTTP error.
- **Capability errors**: unavailable or provider-lacks-capability failures return structured
  capability metadata: `code`, `capability`, `provider`, `model`, and `reason`.
- **Adapter boundary**: `/api/llm/generate` routes through `TextGenerationService`; routes do not
  call provider factories directly.
- **Audio metadata**: faster-whisper segment offsets, language, and task metadata are preserved
  through the native STT path.
- **Hub adoption**: daemon baseline upgrade treats standalone `gwiki_*` hub tables additively and
  preserves existing `code_*` and `gwiki_*` data.
- **Out of scope**: this delta does not implement the `embeddings.*` to `ai.embeddings.*`
  config-store namespace migration. That migration remains a separate daemon/CLI compatibility cut.
- **Agy transport**: `agy` remains unavailable for `text_generate` until a daemon transport exists.

## P1: Voice Capability Contract

`kind: framing`

**Goal**: make audio transcription and translation independently probeable, and return the transcript
metadata required by multimodal gwiki ingest.

### 1.1 Advertise voice capability flags [category: code]

`kind: deliverable`

Targets: `src/gobby/servers/routes/voice.py`, `tests/servers/routes/test_voice_routes.py`

Extend `GET /api/voice/status` to include `transcription_enabled` and
`translation_enabled`. The values are derived from daemon audio capability bindings, including local
Whisper and `voice.openai_compatible_audio` binding flags.

**Acceptance:**

- 1.1.1 - Voice status includes `transcription_enabled` and `translation_enabled` in the normal,
  no-config, websocket-backed, disabled-STT, and remote-audio binding cases. test:
  `tests/servers/routes/test_voice_routes.py`.
- 1.1.2 - The flags reflect capability-level availability, so transcribe and translate can differ
  for the same reachable status route. test:
  `tests/servers/routes/test_voice_routes.py::TestVoiceRoutes::test_status_advertises_remote_audio_capability_flags`.
- 1.1.3 - `/api/providers/models` is unchanged as discovery-only behavior. behavior:
  "voice availability is decided by /api/voice/status, not /api/providers/models".

### 1.2 Return transcription metadata [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/ai/audio.py`, `src/gobby/voice/stt.py`,
`src/gobby/servers/routes/voice.py`, `tests/ai/test_audio_capabilities.py`,
`tests/servers/routes/test_voice_routes.py`

Extend `POST /api/voice/transcribe` to keep the backward-compatible `text` field and add
`segments`, `language`, `task`, `provider`, `model`, and `capability`. Preserve faster-whisper
segment/language metadata through native STT and parse equivalent metadata from OpenAI-compatible
`verbose_json` audio responses when present.

**Acceptance:**

- 1.2.1 - Audio service results preserve adapter-supplied `segments`, `language`, and `task`.
  test: `tests/ai/test_audio_capabilities.py::test_audio_service_preserves_adapter_metadata`.
- 1.2.2 - Whisper STT exposes verbose transcription/translation output while existing text-only
  methods keep returning strings. file: `src/gobby/voice/stt.py`.
- 1.2.3 - The voice route response includes `text`, `segments`, `language`, `task`,
  `capability`, `provider`, and `model`. test:
  `tests/servers/routes/test_voice_routes.py::TestVoiceRoutes::test_transcribe_success`.
- 1.2.4 - OpenAI-compatible audio adapters request `verbose_json` and parse segment/language
  metadata without dropping the legacy `text`. test:
  `tests/ai/test_audio_capabilities.py::test_openai_compatible_adapter_posts_transcription_request`.

## P2: Text And Vision Capability Contract

`kind: framing`

**Goal**: keep daemon text generation and vision extraction behind canonical capability services and
return route shapes compatible with the CLI multimodal client.

### 2.1 Keep text generation on TextGenerationService [category: code]

`kind: deliverable`

Targets: `src/gobby/servers/routes/llm.py`, `tests/servers/routes/test_llm_routes.py`

Keep `GET /api/llm/status` as the `text_generate` capability availability source and route
`POST /api/llm/generate` through `TextGenerationService`.

**Acceptance:**

- 2.1.1 - `/api/llm/status` returns the daemon AI capability registry snapshot, including
  `text_generate`. test: `tests/servers/routes/test_llm_routes.py::test_llm_status_returns_registry_snapshot`.
- 2.1.2 - `/api/llm/generate` executes `TextGenerationService.generate`. test:
  `tests/servers/routes/test_llm_routes.py::test_generate_selects_acp_backed_provider`.
- 2.1.3 - `agy` text generation remains unavailable until a transport exists. behavior:
  "`agy` has no available `text_generate` binding".

### 2.2 Return vision extraction contract fields [category: code]

`kind: deliverable`

Targets: `src/gobby/ai/vision.py`, `src/gobby/servers/routes/llm.py`,
`tests/servers/routes/test_llm_routes.py`

Extend `POST /api/llm/vision/extract` to return `description`, optional `ocr_text`, `provider`,
`model`, and `capability`, while retaining the existing `text` field as a compatibility alias for
`description`.

**Acceptance:**

- 2.2.1 - Vision extraction results can carry optional `ocr_text`. file:
  `src/gobby/ai/vision.py`.
- 2.2.2 - The vision route response includes `description`, `ocr_text`, `provider`, `model`,
  and `capability`, while retaining `text`. test:
  `tests/servers/routes/test_llm_routes.py::test_vision_extract_upload_executes_service`.
- 2.2.3 - `/api/llm/vision/status` remains the availability route for `vision_extract`. test:
  `tests/servers/routes/test_llm_routes.py::test_vision_status_lists_only_proven_providers_as_available`.

## P3: Capability Error Contract

`kind: framing`

**Goal**: make daemon capability failures machine-readable for CLI routing and degradation.

### 3.1 Standardize capability unavailable errors [category: code] (depends: P1, P2)

`kind: deliverable`

Targets: `src/gobby/servers/routes/voice.py`, `src/gobby/servers/routes/llm.py`,
`tests/servers/routes/test_voice_routes.py`, `tests/servers/routes/test_llm_routes.py`

Return structured error bodies for unavailable capabilities, including provider-lacks-capability.
The payload includes `code`, `capability`, `provider`, `model`, and `reason`; voice keeps the
legacy `text: ""` compatibility field on route-level failures.

**Acceptance:**

- 3.1.1 - Provider-lacks-audio-capability returns `code="capability_unavailable"` with
  capability/provider/model/reason metadata. test:
  `tests/servers/routes/test_voice_routes.py::TestVoiceRoutes::test_transcribe_provider_lacks_capability_returns_structured_error`.
- 3.1.2 - `/api/llm/generate` capability failures return structured body metadata. test:
  `tests/servers/routes/test_llm_routes.py::test_generate_returns_deterministic_unavailable_error`.
- 3.1.3 - `/api/llm/vision/extract` provider-lacks-capability failures return structured detail
  body metadata. test:
  `tests/servers/routes/test_llm_routes.py::test_vision_extract_rejects_unproven_provider`.

## P4: Internal Daemon Adoption

`kind: framing`

**Goal**: migrate daemon internals to the same capability contract and preserve standalone hub data
when the daemon adopts a partial CLI-owned hub.

### 4.1 Migrate code-index summaries to text_generate [category: code]

`kind: deliverable`

Targets: `src/gobby/code_index/summarizer.py`, `src/gobby/runner_lifecycle_subsystems.py`,
`tests/code_index/test_summarizer.py`

Change code-index symbol summarization to call `TextGenerationService.generate` with capability
`text_generate` request metadata instead of reaching into legacy `LLMService` providers.

**Acceptance:**

- 4.1.1 - `SymbolSummarizer` accepts `TextGenerationService` and sends
  `TextGenerationRequest(caller="code_index.symbol_summary")`. test:
  `tests/code_index/test_summarizer.py::test_summarize_one`.
- 4.1.2 - Startup constructs the summarizer with `build_daemon_text_generation_service`. file:
  `src/gobby/runner_lifecycle_subsystems.py`.
- 4.1.3 - Capability or adapter failures during summarization return `None` without aborting
  indexing. test: `tests/code_index/test_summarizer.py`.

### 4.2 Adopt standalone gwiki hub data additively [category: code]

`kind: deliverable`

Targets: `src/gobby/storage/hub/postgres.py`,
`tests/storage/hub/test_postgres_baseline_application.py`

Extend Postgres baseline classification and statement filtering so the daemon can adopt standalone
`gwiki_*` hubs and mixed `code_*` plus `gwiki_*` hubs without treating them as corrupt partial
baselines or re-creating owned subset tables.

**Acceptance:**

- 4.2.1 - Baseline classification recognizes standalone `gwiki_*` tables as
  `gwiki_standalone`. test:
  `tests/storage/hub/test_postgres_baseline_application.py::test_classify_baseline_state_distinguishes_fresh_infra_and_corruption`.
- 4.2.2 - Baseline classification recognizes complete `code_*` data with additive `gwiki_*`
  tables as `gcore_code_index`. test:
  `tests/storage/hub/test_postgres_baseline_application.py::test_classify_baseline_state_distinguishes_fresh_infra_and_corruption`.
- 4.2.3 - Baseline application skips create statements for preexisting `code_*` and `gwiki_*`
  subset tables while still applying daemon-owned tables and schema bookkeeping. test:
  `tests/storage/hub/test_postgres_baseline_application.py::test_apply_postgres_baseline_adopts_gcore_code_index_state`.
- 4.2.4 - Baseline application adopts standalone `gwiki_*` state and records the daemon baseline
  version. test:
  `tests/storage/hub/test_postgres_baseline_application.py::test_apply_postgres_baseline_adopts_gwiki_standalone_state`.

## VS1: Verification

`kind: verification`

Plan validation:

- `uv run gobby plans validate .gobby/plans/gwiki-daemon-ai-contract.md`
- `uv run gobby plans validate .gobby/plans/gwiki-daemon-ai-contract.md --mode expansion`

Focused implementation validation:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_audio_capabilities.py`
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_voice_routes.py tests/servers/routes/test_llm_routes.py`
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_summarizer.py`
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/hub/test_postgres_baseline_application.py`

## AC1: Acceptance Criteria

`kind: verification`

- `/api/voice/status` advertises independent transcription and translation capability flags.
- `/api/voice/transcribe` preserves `text` and returns audio segments, language, task, provider,
  model, and capability.
- `/api/llm/generate` uses `TextGenerationService` and `/api/llm/status` remains the
  `text_generate` availability source.
- `/api/llm/vision/extract` returns description, optional OCR text, provider, model, and
  capability.
- Capability-unavailable and provider-lacks-capability failures are structured and
  machine-readable.
- `/api/providers/models` remains discovery-only.
- Code-index summaries consume `text_generate`.
- Daemon hub baseline adoption preserves existing standalone `code_*` and `gwiki_*` subset data.

## V1 Plan Changelog

`kind: verification`

- **R1 (2026-06-01)**: Added companion daemon AI contract plan for gwiki multimodal P6 D1-D5,
  separated from wiki gateway/API/MCP/web/update work in `gwiki-daemon-web.md`.

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Advertise voice capability flags
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_voice_routes.py"
  labels:
    - covers:gwiki-daemon-ai-contract:1.1:1.1.1
    - covers:gwiki-daemon-ai-contract:1.1:1.1.2
    - covers:gwiki-daemon-ai-contract:1.1:1.1.3
  implementation_domain: backend
  tdd: true
  source_section: "1.1"
- title: Return transcription metadata
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_audio_capabilities.py tests/servers/routes/test_voice_routes.py"
  labels:
    - covers:gwiki-daemon-ai-contract:1.2:1.2.1
    - covers:gwiki-daemon-ai-contract:1.2:1.2.2
    - covers:gwiki-daemon-ai-contract:1.2:1.2.3
    - covers:gwiki-daemon-ai-contract:1.2:1.2.4
  implementation_domain: backend
  tdd: true
  source_section: "1.2"
- title: Keep text generation on TextGenerationService
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_llm_routes.py"
  labels:
    - covers:gwiki-daemon-ai-contract:2.1:2.1.1
    - covers:gwiki-daemon-ai-contract:2.1:2.1.2
    - covers:gwiki-daemon-ai-contract:2.1:2.1.3
  implementation_domain: backend
  tdd: true
  source_section: "2.1"
- title: Return vision extraction contract fields
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_llm_routes.py"
  labels:
    - covers:gwiki-daemon-ai-contract:2.2:2.2.1
    - covers:gwiki-daemon-ai-contract:2.2:2.2.2
    - covers:gwiki-daemon-ai-contract:2.2:2.2.3
  implementation_domain: backend
  tdd: true
  source_section: "2.2"
- title: Standardize capability unavailable errors
  category: code
  task_type: feature
  depends_on:
    - "1.1"
    - "1.2"
    - "2.1"
    - "2.2"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_voice_routes.py tests/servers/routes/test_llm_routes.py"
  labels:
    - covers:gwiki-daemon-ai-contract:3.1:3.1.1
    - covers:gwiki-daemon-ai-contract:3.1:3.1.2
    - covers:gwiki-daemon-ai-contract:3.1:3.1.3
  implementation_domain: backend
  tdd: true
  source_section: "3.1"
- title: Migrate code-index summaries to text_generate
  category: code
  task_type: refactor
  depends_on:
    - "2.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_summarizer.py"
  labels:
    - covers:gwiki-daemon-ai-contract:4.1:4.1.1
    - covers:gwiki-daemon-ai-contract:4.1:4.1.2
    - covers:gwiki-daemon-ai-contract:4.1:4.1.3
  implementation_domain: backend
  tdd: true
  source_section: "4.1"
- title: Adopt standalone gwiki hub data additively
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/hub/test_postgres_baseline_application.py"
  labels:
    - covers:gwiki-daemon-ai-contract:4.2:4.2.1
    - covers:gwiki-daemon-ai-contract:4.2:4.2.2
    - covers:gwiki-daemon-ai-contract:4.2:4.2.3
    - covers:gwiki-daemon-ai-contract:4.2:4.2.4
  implementation_domain: backend
  tdd: true
  source_section: "4.2"
```
