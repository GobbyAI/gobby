# Configuration Audit

Authoring task: #17044. Draft date: 2026-06-14.

**Disposition: historical.** This file is a dated 2026-06-14 snapshot of the
Configuration page and `/api/config` surface as they existed for P13 overlay
planning. Do not treat route tables or sidecar rows as current API
documentation. Live definition routes are in
[`docs/guides/http-endpoints.md`](../guides/http-endpoints.md): variable
defaults are `/api/variables`, live session values are
`/api/sessions/{session_id}/variables/{get,set}`, and there is no
`/api/workflows` or public `workflow_type`.

This audit inventories the legacy Configuration page and the backend configuration surface so P13 can build a settings overlay from facts instead of copying the current page shape. The matrix status vocabulary is:

- `live`: backend and frontend paths both exist and the control can keep working in the overlay.
- `dead-backend`: backend persistence exists but no visible frontend control exists today.
- `dead-frontend`: frontend sends or shows a value that the backend does not persist or canonically support.
- `cli-only`: supported configuration is intentionally available through CLI or operator tooling without a web control.
- `mismatched-type`: frontend renders a control that cannot safely represent the backend schema type.
- `missing-validation`: the option works but needs a bounded overlay control or backend validator before migration.

Disposition vocabulary is `keep`, `drop`, and `fix`. Rows with `keep` include the target overlay section id. Rows with `fix` or `drop` are also enumerated in Follow-up Cleanup Items.

## Backend Inventory

| Surface | Routes / models | Notes |
| --- | --- | --- |
| Core values | `GET /api/config/schema`, `GET/PUT /api/config/values`, `POST /api/config/values/validate`, `POST /api/config/values/reset`; `SaveConfigRequest.values` | Schema is `DaemonConfig.model_json_schema()`; save flattens partial values, validates against `DaemonConfig`, persists normal values and secret-backed values separately, and updates runtime config. |
| Template YAML | `GET/PUT /api/config/template`; `SaveTemplateRequest.content` | Full defaults plus DB overrides as YAML; save applies transactional config/secret changes and requires restart. |
| Import/export | `POST /api/config/export`, `POST /api/config/import`; `ImportConfigRequest` | Exports config store, config, secret key metadata, and prompt overrides; import can persist config store/config/prompts. |
| Prompts | `GET /api/config/prompts`, `GET/PUT/DELETE /api/config/prompts/{path}`; `SavePromptOverrideRequest.content` | Lists bundled/overridden prompts, fetches detail, saves override content, and deletes overrides. |
| Secrets | `GET/POST /api/config/secrets`, `DELETE /api/config/secrets/{name}`; `SaveSecretRequest` | Secret name/value/category/description CRUD with masking and FalkorDB secret validation. |
| Tool approvals | `GET/PUT /api/config/tool-approvals/global`; `SaveApprovalRulesRequest.rules` | Global auto-allow rules plus read-only defaults and built-in exemptions. |
| UI settings | `GET/PUT /api/config/ui-settings`, `DELETE /api/config/ui-settings/{key}`; `SaveUISettingsRequest` | Persists `fontSize`, `model`, `theme`, `defaultChatMode`, `planPendingVariant`, `selectedProjectId`, and `selectedProvider` under `ui_settings.*`. |
| Validation detection preview | `POST /api/config/validation-detection/preview`; `ValidationDetectionPreviewRequest` | Runs matcher preview for a command against current or provided validation-detection config. |
| Rules enforcement sidecar | `GET/PUT /api/rules` collection route | Not under `/api/config`, but current Configuration tab embeds the global rules-engine toggle. |
| Variables sidecar | Historical: `GET/POST/PUT/DELETE /api/workflows?workflow_type=variable`. Current: `GET/POST/PUT/DELETE /api/variables`. | Historical 2026-06-14 note: not under `/api/config`, but the then-current Configuration page included variable defaults. |
| Schema models | `src/gobby/config/*.py`, especially `DaemonConfig` and nested Pydantic models | Flattened schema inventory contains 355 leaf fields. Telemetry fields are included under `telemetry.*`. |

## Frontend Inventory

| Surface | Source | Controls |
| --- | --- | --- |
| Configuration tab | `ConfigurationPage.tsx` `ConfigFormTab` + `ConfigurationPage.SchemaField.tsx` | Generic schema renderer; supports selects for enums, toggles for booleans, number inputs, text/password inputs, collapsible object sections, Save Configuration, Reset to Defaults, restart banner. |
| Validation detection editor | `ValidationDetectionEditor.tsx` | JSON textarea for matcher config, preview command input, Preview button, preview/error feedback. |
| Approvals tab | `ConfigurationPage.tsx` `ApprovalRulesTab` | Read-only built-in exemptions, editable global auto-allow rule rows, Add Rule, Reset To Defaults, Save Rules. |
| Secrets tab | `ConfigurationPage.SecretsTab.tsx` | Add/update/delete secret form with name, value, category, description; masked table; native confirm on delete. |
| Prompts tab | `ConfigurationPage.tsx` `PromptsTab` | Category filter, prompt cards, CodeMirror markdown override editor, Save Override, Revert. |
| Variables tab | `ConfigurationPage.tsx` `VariablesTab` | Workflow variable list, add form, enable toggle, delete installed variables. |
| Template tab | `ConfigurationPage.TemplateTab.tsx` | CodeMirror YAML editor, Save Template, restart banner. |
| Import/export toolbar | `ConfigurationPage.tsx` toolbar | Import JSON through a generated file input; Export JSON download. |
| Small Settings dialog | `Settings.tsx` + `useSettings.ts` | Font size range, theme segmented buttons, default chat mode segmented buttons, reset. |
| Chat/project/provider persistence | `App.tsx`, `ProjectSelector.tsx`, `ProviderPicker.tsx`, `ChatInputVoiceControls.tsx` | Selected project/provider, model, STT/TTS toggles, PTT/VAD mode are controlled outside the Configuration page but are settings-overlay candidates. |

## Proposed Overlay IA

Section ids are stable kebab-case ids. The matrix below assigns every `keep` row to exactly one of these ids.

| Order | Section id | Purpose |
| --- | --- | --- |
| 1 | appearance | Theme, density, font size, plan-pending presentation. |
| 2 | providers-models | Provider/model selection, local generation endpoints, feature model candidates. |
| 3 | chat-voice | Default chat mode, active voice preferences, STT/TTS daemon settings. |
| 4 | projects-sessions | Project selection, session lifecycle, summaries, validation evidence detection. |
| 5 | tool-approvals | Global auto-allow rules and per-tool approval policy. |
| 6 | secrets-auth | Secrets store, auth credentials, secret references. |
| 7 | prompts-templates | Prompt overrides, full YAML template, import/export. |
| 8 | automation-workflows | Tasks, workflows, cron, pipelines, tmux automation, variables. |
| 9 | mcp-tools | MCP proxy, tool search/recommendation, skills hub configuration. |
| 10 | memory-knowledge | Memory, embeddings, Qdrant/FalkorDB, and the wiki watcher. |
| 11 | observability | Telemetry, logs, metrics, tracing, exporters. |
| 12 | integrations-hooks | Communications channels, webhooks, hook broadcasts. |
| 13 | runtime-infrastructure | Daemon ports, CORS, directories, UI serving, code index, search, freshness. |

## Full Matrix

Total rows: 378 (22 manual frontend/route rows plus 356 backend schema rows).

### Structural registry creation gates

`gobby-tasks.enabled` and `tool_result_offload.enabled` control whether their MCP
registries exist. The daemon reads both keys during internal-registry construction;
changing either stored value takes effect after daemon restart. Settings inside an
already-created registry continue to use the live per-epoch configuration contract.

| Option | Backend source | Frontend control | Status | Disposition | Overlay section | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| rules.enforcement_enabled | GET/PUT /api/rules in src/gobby/servers/routes/rules.py | ConfigurationPage ConfigFormTab Rules Engine toggle | live | keep | automation-workflows | Lives outside /api/config but is embedded in the current Configuration tab. |
| tool_approvals.global_rules | GET/PUT /api/config/tool-approvals/global; SaveApprovalRulesRequest.rules | ApprovalRulesTab editable rule rows with add/remove/reset/save | live | keep | tool-approvals | Default rules and built-in exemptions are read-only context. |
| secrets.store.name | POST /api/config/secrets; SaveSecretRequest.name | SecretsTab Secret name input | live | keep | secrets-auth |  |
| secrets.store.value | POST /api/config/secrets; SaveSecretRequest.value | SecretsTab password input; list masks values | live | keep | secrets-auth |  |
| secrets.store.category | POST /api/config/secrets; SaveSecretRequest.category | SecretsTab category select from backend categories | live | keep | secrets-auth |  |
| secrets.store.description | POST /api/config/secrets; SaveSecretRequest.description | SecretsTab optional description input | live | keep | secrets-auth |  |
| prompts.override.content | GET/PUT/DELETE /api/config/prompts/{path}; SavePromptOverrideRequest.content | PromptsTab category list + CodeMirror markdown override editor | live | keep | prompts-templates |  |
| variables.definition | Historical: GET/POST/PUT/DELETE /api/workflows?workflow_type=variable. Current: /api/variables | VariablesTab add/toggle/delete table | live | keep | automation-workflows | Historical 2026-06-14 note: not a configuration route; the then-current page mixed variable definitions into Configuration. |
| template.yaml_content | GET/PUT /api/config/template; SaveTemplateRequest.content | TemplateTab CodeMirror YAML editor + restart banner | live | keep | prompts-templates | Advanced full-template editor; keep as an advanced section, not the primary IA. |
| import_export.export_bundle | POST /api/config/export | ConfigurationPage toolbar Export JSON download | live | keep | prompts-templates |  |
| import_export.import_bundle | POST /api/config/import; ImportConfigRequest | ConfigurationPage toolbar file picker + import alert | live | keep | prompts-templates |  |
| ui_settings.fontSize | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.fontSize | Settings dialog range input; document --font-size-base | missing-validation | fix | appearance | Frontend allows 12-48; type comment says 12-24; backend only checks int. |
| ui_settings.theme | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.theme | Settings dialog theme segmented buttons; document data-theme | live | keep | appearance |  |
| ui_settings.model | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.model | Chat ProviderPicker model selection via useSettings.updateModel | live | keep | providers-models |  |
| client.chatMode | useSettings chatMode only; intentionally excluded from backend persistence | Chat input mode selector for current conversation | live | drop | (none) | Keep per-conversation control in chat, not the global settings overlay. |
| ui_settings.defaultChatMode | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.defaultChatMode | Settings dialog Default Mode segmented buttons | missing-validation | fix | chat-voice | Backend persists arbitrary strings; frontend normalizes with CHAT_MODES. |
| ui_settings.sttEnabled | useSettings sends PUT /api/config/ui-settings; backend request model omits field | ChatInputVoiceControls microphone toggle | dead-frontend | fix | chat-voice | Persistable in frontend/localStorage but ignored by SaveUISettingsRequest. |
| ui_settings.ttsEnabled | useSettings sends PUT /api/config/ui-settings; backend request model omits field | ChatInputVoiceControls speaker toggle | dead-frontend | fix | chat-voice | Persistable in frontend/localStorage but ignored by SaveUISettingsRequest. |
| ui_settings.voiceInputMode | useSettings sends PUT /api/config/ui-settings; backend request model omits field | ChatInputVoiceControls PTT/VAD toggle | dead-frontend | fix | chat-voice | Persistable in frontend/localStorage but ignored by SaveUISettingsRequest. |
| ui_settings.planPendingVariant | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.planPendingVariant | No visible control found; updatePlanPendingVariant is only test-covered | dead-backend | fix | appearance | Backend and hook support it, but the overlay needs an explicit control or the option should be dropped. |
| ui_settings.selectedProjectId | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.selectedProjectId | ProjectSelector persisted directly from App.tsx | live | keep | projects-sessions |  |
| ui_settings.selectedProvider | GET/PUT /api/config/ui-settings; SaveUISettingsRequest.selectedProvider | ProviderPicker persisted directly from App.tsx/useChat provider state | live | keep | providers-models |  |
| daemon_port | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| bind_host | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | runtime-infrastructure |  |
| daemon_health_check_interval | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| test_mode | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| cors_origins | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | runtime-infrastructure | array items=string map= |
| hub_backend | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | drop | (none) | retired operator setting; runtime is fixed to postgres |
| database_url | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | drop | (none) | sole PostgreSQL selector; should stay out of the overlay and secret-backed storage |
| websocket.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| websocket.port | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| websocket.ping_interval | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| websocket.ping_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| telemetry.service_name | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | observability |  |
| logging.level | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | observability |  |
| logging.format | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | observability |  |
| logging.dir | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | observability |  |
| logging.max_size_mb | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| logging.backup_count | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| logging.runtime_max_size_mb | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| logging.growth_warn_mb_per_interval | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| telemetry.traces_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | observability |  |
| telemetry.traces_to_console | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | observability |  |
| telemetry.trace_sample_rate | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| telemetry.trace_retention_days | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| telemetry.metrics_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | observability |  |
| telemetry.exporter.otlp_endpoint | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | observability |  |
| telemetry.exporter.otlp_protocol | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | observability |  |
| telemetry.exporter.otlp_headers | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | observability | object items= map=string |
| telemetry.exporter.prometheus_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | observability |  |
| telemetry.llm_tracing.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | observability |  |
| telemetry.llm_tracing.capture_content | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | observability |  |
| telemetry.llm_tracing.providers | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | observability | array items=string map= |
| session_summary.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | projects-sessions |  |
| session_summary.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | projects-sessions | array items=string map= |
| session_summary.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | projects-sessions |  |
| session_summary.prompt | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| session_summary.summary_file_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| compact_handoff.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | projects-sessions |  |
| compact_handoff.refresh_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| context_injection.enabled | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| context_injection.default_source | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| context_injection.max_file_size | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| context_injection.max_content_size | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| context_injection.max_transcript_messages | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| context_injection.truncation_suffix | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| context_injection.context_template | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead ContextInjectionConfig group; parsed but never consumed |
| mcp_client_proxy.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | mcp-tools |  |
| mcp_client_proxy.connect_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | mcp-tools |  |
| mcp_client_proxy.proxy_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | mcp-tools |  |
| mcp_client_proxy.tool_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | mcp-tools |  |
| mcp_client_proxy.tool_timeouts | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | mcp-tools | object items= map=number |
| mcp_client_proxy.search_mode | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | mcp-tools |  |
| mcp_client_proxy.min_similarity | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | mcp-tools |  |
| mcp_client_proxy.top_k | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | mcp-tools |  |
| mcp_client_proxy.refresh_on_server_add | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | mcp-tools |  |
| mcp_client_proxy.refresh_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | mcp-tools |  |
| gobby-tasks.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | restart-structural | keep | automation-workflows | Registry creation gate; daemon restart required. |
| tool_result_offload.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | restart-structural | keep | mcp-tools | Registry creation gate; daemon restart required. |
| gobby-tasks.show_result_on_create | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| gobby-tasks.file_extraction.file_extensions | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=string map= |
| gobby-tasks.file_extraction.known_files | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=string map= |
| gobby-tasks.file_extraction.path_prefixes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=string map= |
| gobby-tasks.expansion.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | automation-workflows |  |
| gobby-tasks.expansion.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=string map= |
| gobby-tasks.expansion.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| gobby-tasks.expansion.prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.expansion.system_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.expansion.codebase_research_enabled | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead expansion research knob; no runtime consumer |
| gobby-tasks.expansion.research_model | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead expansion research knob; no runtime consumer |
| gobby-tasks.expansion.research_max_steps | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead expansion research knob; no runtime consumer |
| gobby-tasks.expansion.research_system_prompt | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead expansion research knob; no runtime consumer |
| gobby-tasks.expansion.web_research_enabled | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead expansion research knob; no runtime consumer |
| gobby-tasks.expansion.default_strategy | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | automation-workflows |  |
| gobby-tasks.expansion.timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| gobby-tasks.expansion.research_timeout | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead expansion research knob; no runtime consumer |
| gobby-tasks.expansion.pattern_criteria.patterns | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | automation-workflows | object items= map=array |
| gobby-tasks.expansion.pattern_criteria.detection_keywords | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | automation-workflows | object items= map=array |
| gobby-tasks.validation.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | automation-workflows |  |
| gobby-tasks.validation.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=string map= |
| gobby-tasks.validation.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| gobby-tasks.validation.system_prompt | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.validation.prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.validation.criteria_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.validation.criteria_system_prompt | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.validation.max_iterations | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| gobby-tasks.validation.escalation_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| gobby-tasks.validation.escalation_notify | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | automation-workflows |  |
| gobby-tasks.validation.escalation_webhook_url | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| gobby-tasks.validation.auto_generate_on_create | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| gobby-tasks.validation.auto_generate_on_expand | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| web_chat_sandbox.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| web_chat_sandbox.extra_read_paths | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | runtime-infrastructure | array items=string map= |
| web_chat_sandbox.extra_write_paths | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | runtime-infrastructure | array items=string map= |
| agent_sandbox.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| agent_sandbox.extra_read_paths | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | runtime-infrastructure | array items=string map= |
| agent_sandbox.extra_write_paths | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | runtime-infrastructure | array items=string map= |
| communications.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | integrations-hooks |  |
| communications.webhook_base_url | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | integrations-hooks |  |
| communications.channel_defaults.rate_limit_per_minute | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks |  |
| communications.channel_defaults.burst | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks |  |
| communications.channel_defaults.retry_count | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks |  |
| communications.channel_defaults.poll_interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks |  |
| communications.channel_defaults.retention_days | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks |  |
| communications.inbound_enabled | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead toggle; no runtime consumer |
| communications.outbound_enabled | (removed) | (removed) | dead | drop | (none) | removed in #19400 — dead toggle; no runtime consumer |
| communications.auto_create_sessions | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | integrations-hooks |  |
| digest.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | runtime-infrastructure |  |
| digest.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | runtime-infrastructure | array items=string map= |
| digest.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| digest.timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| memory_recall.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | memory-knowledge |  |
| memory_recall.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | memory-knowledge | array items=string map= |
| memory_recall.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory_recall.timeout | Retired with the substantive-prompt classifier; recall makes no LLM call | (none — no surface) | retired | drop | (none) | removed from the runtime config contract; the hook load-order chain now starts at workflow.timeout |
| memory_recall.candidate_limit | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory_recall.selected_limit | Retired with the substantive-prompt classifier (#20765); the rank limit is now candidate_limit | (none — no surface) | retired | drop | (none) | removed from the runtime config contract |
| memory_recall.min_score | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge | search floor; the backfill loop chases it |
| memory_recall.selection_min_score | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge | added #20771; selection floor, the only control that reduces per-turn injection |
| memory_recall.query_synthesis_threshold | Retired with the substantive-prompt classifier (#20765); the query is built without an LLM | (none — no surface) | retired | drop | (none) | removed from the runtime config contract |
| memory_recall.query_max_chars | Retired with the substantive-prompt classifier (#20765); the query is built without an LLM | (none — no surface) | retired | drop | (none) | removed from the runtime config contract |
| recommend_tools.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | providers-models |  |
| recommend_tools.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | providers-models | array items=string map= |
| recommend_tools.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | providers-models |  |
| recommend_tools.prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| recommend_tools.hybrid_rerank_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| recommend_tools.llm_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| tool_summarizer.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | providers-models |  |
| tool_summarizer.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | providers-models | array items=string map= |
| tool_summarizer.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | providers-models |  |
| tool_summarizer.prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| tool_summarizer.system_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| tool_summarizer.server_description_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| tool_summarizer.server_description_system_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| import_mcp_server.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | providers-models |  |
| import_mcp_server.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | providers-models | array items=string map= |
| import_mcp_server.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | providers-models |  |
| import_mcp_server.prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| import_mcp_server.github_fetch_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| import_mcp_server.search_fetch_prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | providers-models |  |
| knowledge_graph_queue.interval_minutes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| knowledge_graph_queue.batch_size | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| hook_extensions.websocket.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | integrations-hooks |  |
| hook_extensions.websocket.broadcast_events | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | integrations-hooks | array items=string map= |
| hook_extensions.websocket.include_payload | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | integrations-hooks |  |
| hook_extensions.webhooks.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | integrations-hooks |  |
| hook_extensions.webhooks.endpoints | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | integrations-hooks | array items=object map= |
| hook_extensions.webhooks.default_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks |  |
| hook_extensions.webhooks.async_dispatch | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | integrations-hooks |  |
| hooks.adapter_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks | Default 105 seconds; daemon restart required. |
| hooks.provider_timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | integrations-hooks | Default 120 seconds; daemon restart and provider reinstall required. |
| workflow.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| workflow.timeout | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows | Default 90 seconds; daemon restart required; must remain between memory recall and adapter deadlines. |
| workflow.debug_echo_context | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| databases.qdrant.url | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| databases.qdrant.api_key | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | secrets-auth |  |
| databases.qdrant.port | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| databases.qdrant.collection_prefix | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| databases.falkordb.host | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| databases.falkordb.port | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| databases.falkordb.password | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | secrets-auth |  |
| databases.falkordb.graph_name | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| databases.falkordb.graph_search | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| databases.falkordb.graph_min_score | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| databases.falkordb.rrf_k | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| ai.embeddings.model | Registry schema via /api/config/schema; activate via embedding switch coordinator | Settings -> managed read-only field | managed | keep | memory-knowledge |  |
| ai.embeddings.dim | Registry schema via /api/config/schema; activate via embedding switch coordinator | Settings -> managed read-only field | managed | keep | memory-knowledge |  |
| ai.embeddings.api_base | Registry schema via /api/config/schema; activate via embedding switch coordinator | Settings -> managed read-only field | managed | keep | memory-knowledge |  |
| ai.embeddings.api_key | Registry schema via /api/config/schema; save via revisioned PATCH | Settings -> secret field | live | keep | secrets-auth |  |
| ai.embeddings.query_prefix | Registry schema via /api/config/schema; activate via embedding switch coordinator | Settings -> managed read-only field | managed | keep | memory-knowledge |  |
| ai.embeddings.catalog_key | Registry schema via /api/config/schema; activate via embedding switch coordinator | Settings -> managed action | managed | keep | memory-knowledge |  |
| ai.generation.timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | providers-models |  |
| ai.generation.candidate_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | providers-models |  |
| ai.generation.endpoints | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | providers-models | object items= map=object |
| ai.generation.profile_defaults | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | providers-models | object items= map=array |
| memory.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory.backend | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| memory.auto_crossref | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory.crossref_threshold | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.crossref_max_links | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.access_debounce_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.kg.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | memory-knowledge |  |
| memory.kg.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | memory-knowledge | array items=string map= |
| memory.dream.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | memory-knowledge |  |
| memory.dream.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | memory-knowledge | array items=string map= |
| memory.dream.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory.dream.schedule_cron | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| memory.dream.prompt_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| memory.dream.max_tokens | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.max_runtime_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.work_unit_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.evidence_channel_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.evidence_retry_attempts | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.evidence_phase_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.min_action_confidence | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.min_delete_confidence | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.dream.include_global_memories | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory.dream.reconcile_after_apply | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory.dream.reconcile_after_revert | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory.code_link_min_score | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.temporal_decay_half_life_days | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory.min_recall_score | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| memory_backup.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| memory_backup.backup_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | memory-knowledge |  |
| skills.inject_core_skills | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | mcp-tools |  |
| skills.core_skills_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | mcp-tools |  |
| skills.injection_format | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | mcp-tools |  |
| skills.hubs | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | mcp-tools | object items= map=object |
| chat_history.max_message_chars | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| chat_history.max_total_chars | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| message_tracking.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | projects-sessions |  |
| message_tracking.poll_interval | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| message_tracking.debounce_delay | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| message_tracking.max_message_length | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| message_tracking.broadcast_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | projects-sessions |  |
| session_lifecycle.active_session_pause_minutes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| session_lifecycle.stale_session_timeout_hours | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| session_lifecycle.expire_check_interval_minutes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| session_lifecycle.transcript_processing_interval_minutes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| session_lifecycle.transcript_processing_batch_size | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | projects-sessions |  |
| session_lifecycle.transcript_archive_dir | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| metrics.list_limit | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | observability |  |
| verification_defaults.unit_tests | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.type_check | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.lint | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.format | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.build | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.doc_tests | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.integration | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.security | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.code_review | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | projects-sessions |  |
| verification_defaults.custom | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | projects-sessions | object items= map=string |
| project_verification_synthesis.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | providers-models |  |
| project_verification_synthesis.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | providers-models | array items=string map= |
| project_verification_synthesis.confidence_threshold | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | providers-models |  |
| validation_detection.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ValidationDetectionEditor JSON textarea + preview command | live | keep | projects-sessions |  |
| validation_detection.builtin_matchers_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ValidationDetectionEditor JSON textarea + preview command | live | keep | projects-sessions |  |
| validation_detection.disabled_builtin_matcher_ids | DaemonConfig schema via /api/config/schema; save via /api/config/values | ValidationDetectionEditor JSON textarea + preview command | live | keep | projects-sessions | array items=string map= |
| validation_detection.recognized_wrappers | DaemonConfig schema via /api/config/schema; save via /api/config/values | ValidationDetectionEditor JSON textarea + preview command | live | keep | projects-sessions | array items=string map= |
| validation_detection.wrapper_rules | DaemonConfig schema via /api/config/schema; save via /api/config/values | ValidationDetectionEditor JSON textarea + preview command | live | keep | projects-sessions | array items=object map= |
| validation_detection.custom_matchers | DaemonConfig schema via /api/config/schema; save via /api/config/values | ValidationDetectionEditor JSON textarea + preview command | live | keep | projects-sessions | array items=object map= |
| search.mode | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | runtime-infrastructure | bounded to keyword/embedding/auto/hybrid but rendered as free text |
| search.keyword_weight | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| search.embedding_weight | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| search.notify_on_fallback | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| ui.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| ui.mode | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | runtime-infrastructure | bounded to auto/production/dev but rendered as free text |
| ui.port | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| ui.host | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | runtime-infrastructure |  |
| ui.web_dir | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | runtime-infrastructure |  |
| ui.memory_graph_limit | (removed) | (removed) | dead | drop | (none) | removed in #19157 — the 2D memory graph had no UI consumer; unified into ui.knowledge_graph_limit |
| ui.knowledge_graph_limit | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input; graph settings panel | live | keep | runtime-infrastructure | 0 = no limit; also editable from the knowledge-graph gear panel (#19157) |
| ui.knowledge_graph_relationship_limit | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input; graph settings panel | live | keep | runtime-infrastructure | 0 = no limit; added in #19157 |
| auth.username | Retired by account-identity-cutover; canonical login identity is stored in users.email | (none — login uses the canonical users table) | retired | drop | (none) | removed from the runtime config contract; cutover deletes the legacy row |
| auth.password | Auth service; not a registered runtime config key | (none — dead draft editor removed in task-19645 R5) | cli-only | drop | (none) | credential management stays CLI-only until #19650 lands an auth surface |
| auth.session_secret | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | drop | (none) | auto-generated session cookie signing secret; schema marks it ui_hidden |
| tmux.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| tmux.command | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| tmux.socket_name | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| tmux.socket_path | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| tmux.config_file | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| tmux.session_prefix | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| tmux.history_limit | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.wsl_distribution | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | automation-workflows |  |
| tmux.idle_check_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| tmux.idle_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.idle_reprompt_delay_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.max_reprompt_attempts | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.reasoning_watchdog_interrupt_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| tmux.reasoning_watchdog_settle_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.init_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.init_activity_grace_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.registration_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| tmux.auto_enter_approval_prompts | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| tmux.auto_enter_agent_terminals | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| tmux.auto_enter_agent_interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| cron.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| cron.check_interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| cron.max_concurrent_jobs | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| cron.running_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| cron.cleanup_after_days | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| cron.backoff_delays | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=integer map= |
| system_loops.automation.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | automation-workflows |  |
| system_loops.automation.interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| pipelines.prompt_step.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | automation-workflows |  |
| pipelines.prompt_step.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | automation-workflows | array items=string map= |
| pipelines.nesting_depth_limit | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | automation-workflows |  |
| voice.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | chat-voice |  |
| voice.tts_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | chat-voice |  |
| voice.tts_provider | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | chat-voice | currently supports chatterbox but rendered as free text |
| voice.tts_reference_audio | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | chat-voice |  |
| voice.tts_reference_text | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | chat-voice |  |
| voice.tts_temperature | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| voice.tts_chatterbox_max_generation_tokens | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| voice.tts_clause_max_chars | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| voice.tts_device | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | chat-voice | bounded device selector but rendered as free text |
| voice.stt_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | chat-voice |  |
| voice.transcription_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| voice.whisper_model_size | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | chat-voice | bounded whisper size selector but rendered as free text |
| voice.whisper_device | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | chat-voice | bounded device selector but rendered as free text |
| voice.whisper_compute_type | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | chat-voice | bounded compute type selector but rendered as free text |
| voice.whisper_prompt | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | chat-voice |  |
| voice.whisper_vocabulary | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | chat-voice | array items=string map= |
| voice.openai_compatible_audio | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | chat-voice | array items=object map= |
| tool_approval.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | tool-approvals |  |
| tool_approval.default_policy | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | tool-approvals |  |
| tool_approval.policies | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | tool-approvals | array items=object map= |
| chat.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | chat-voice |  |
| chat.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | chat-voice | array items=string map= |
| chat.default_mode | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | missing-validation | fix | chat-voice | bounded to normal/accept_edits/bypass/plan but persisted as free text |
| chat.attachment_max_file_bytes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| chat.attachment_max_total_bytes_per_message | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| chat.attachment_max_files_per_message | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| chat.attachment_unbound_retention_hours | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| chat.attachment_gc_interval_minutes | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | chat-voice |  |
| merge_resolution.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | providers-models |  |
| merge_resolution.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | providers-models | array items=string map= |
| skill_description.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | providers-models |  |
| skill_description.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | providers-models | array items=string map= |
| context_window_overrides | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for map/object | mismatched-type | fix | providers-models | object items= map=integer |
| code_index.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| code_index.auto_index_on_commit | (removed) | (removed) | dead | drop | (none) | removed in #19400 — no hook or indexer read site |
| code_index.maintenance_interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.missing_root_purge_observations | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.max_file_size_bytes | (removed) | (removed) | dead | drop | (none) | removed in #19400 — zero consumers; gcode applies its own built-in limits |
| code_index.exclude_patterns | (removed) | (removed) | dead | drop | (none) | removed in #19400 — zero consumers; gcode uses its own hardcoded DEFAULT_EXCLUDES |
| code_index.embedding_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| code_index.graph_enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| code_index.qdrant_collection_prefix | (removed) | (removed) | dead | drop | (none) | removed in #19400 — validation-only mirror of databases.qdrant.collection_prefix |
| code_index.languages | (removed) | (removed) | dead | drop | (none) | removed in #19400 — nothing passes a language list to gcode |
| code_index.symbol_summary.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| code_index.symbol_summary.batch_size | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.symbol_summary.profile | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField select | live | keep | runtime-infrastructure |  |
| code_index.symbol_summary.candidates | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> string list | live | keep | runtime-infrastructure | array items=string |
| code_index.symbol_summary.max_concurrency | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.symbol_summary.max_tokens | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.sync_worker_interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.sync_worker_batch_size | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| code_index.content_extensions | (removed) | (removed) | dead | drop | (none) | removed in #19400 — zero consumers; gcode decides content handling itself |
| indexing.respect_gitignore | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| bin_freshness.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | runtime-infrastructure |  |
| bin_freshness.initial_delay_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| bin_freshness.interval_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| bin_freshness.jitter_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| bin_freshness.github_timeout_seconds | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | runtime-infrastructure |  |
| wiki.enabled | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField toggle | live | keep | memory-knowledge |  |
| wiki.roots | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | memory-knowledge | array items=object map= |
| wiki.debounce_interval | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| wiki.poll_interval | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField number input | live | keep | memory-knowledge |  |
| wiki.ignore_globs | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text input fallback for array | mismatched-type | fix | memory-knowledge | array items=string map= |
| wiki.codewiki_on_commit | DaemonConfig schema via /api/config/schema; save via /api/config/values | none (toggle removed from memory-knowledge) | dormant | drop |  | CodeWiki generation is dormant pending the wiki redesign (#19665); the config key is retained but not surfaced. |
| clones_dir | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | runtime-infrastructure |  |
| worktrees_dir | DaemonConfig schema via /api/config/schema; save via /api/config/values | ConfigFormTab -> SchemaField text/password input | live | keep | runtime-infrastructure |  |

## Follow-up Cleanup Items

These are the rows that P13 must either fix before/while building the overlay or explicitly drop from the overlay. The full matrix above remains the source of truth for exact backend/frontend mapping.

### Fix: Structured Editors

42 schema rows are arrays, object lists, or maps currently rendered through the generic text-input fallback. Build JSON/list/map editors or dedicated section controls for these rows:

| Option | Current control | Target section |
| --- | --- | --- |
| cors_origins | ConfigFormTab -> SchemaField text input fallback for array | runtime-infrastructure |
| telemetry.exporter.otlp_headers | ConfigFormTab -> SchemaField text input fallback for map/object | observability |
| telemetry.llm_tracing.providers | ConfigFormTab -> SchemaField text input fallback for array | observability |
| session_summary.candidates | ConfigFormTab -> SchemaField text input fallback for array | projects-sessions |
| mcp_client_proxy.tool_timeouts | ConfigFormTab -> SchemaField text input fallback for map/object | mcp-tools |
| gobby-tasks.file_extraction.file_extensions | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| gobby-tasks.file_extraction.known_files | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| gobby-tasks.file_extraction.path_prefixes | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| gobby-tasks.expansion.candidates | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| gobby-tasks.expansion.pattern_criteria.patterns | ConfigFormTab -> SchemaField text input fallback for map/object | automation-workflows |
| gobby-tasks.expansion.pattern_criteria.detection_keywords | ConfigFormTab -> SchemaField text input fallback for map/object | automation-workflows |
| gobby-tasks.validation.candidates | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| web_chat_sandbox.extra_read_paths | ConfigFormTab -> SchemaField text input fallback for array | runtime-infrastructure |
| web_chat_sandbox.extra_write_paths | ConfigFormTab -> SchemaField text input fallback for array | runtime-infrastructure |
| agent_sandbox.extra_read_paths | ConfigFormTab -> SchemaField text input fallback for array | runtime-infrastructure |
| agent_sandbox.extra_write_paths | ConfigFormTab -> SchemaField text input fallback for array | runtime-infrastructure |
| digest.candidates | ConfigFormTab -> SchemaField text input fallback for array | runtime-infrastructure |
| memory_recall.candidates | ConfigFormTab -> SchemaField text input fallback for array | memory-knowledge |
| recommend_tools.candidates | ConfigFormTab -> SchemaField text input fallback for array | providers-models |
| tool_summarizer.candidates | ConfigFormTab -> SchemaField text input fallback for array | providers-models |
| import_mcp_server.candidates | ConfigFormTab -> SchemaField text input fallback for array | providers-models |
| hook_extensions.websocket.broadcast_events | ConfigFormTab -> SchemaField text input fallback for array | integrations-hooks |
| hook_extensions.webhooks.endpoints | ConfigFormTab -> SchemaField text input fallback for array | integrations-hooks |
| ai.generation.endpoints | ConfigFormTab -> SchemaField text input fallback for map/object | providers-models |
| ai.generation.profile_defaults | ConfigFormTab -> SchemaField text input fallback for map/object | providers-models |
| memory.kg.candidates | ConfigFormTab -> SchemaField text input fallback for array | memory-knowledge |
| memory.dream.candidates | ConfigFormTab -> SchemaField text input fallback for array | memory-knowledge |
| skills.hubs | ConfigFormTab -> SchemaField text input fallback for map/object | mcp-tools |
| verification_defaults.custom | ConfigFormTab -> SchemaField text input fallback for map/object | projects-sessions |
| project_verification_synthesis.candidates | ConfigFormTab -> SchemaField text input fallback for array | providers-models |
| cron.backoff_delays | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| pipelines.prompt_step.candidates | ConfigFormTab -> SchemaField text input fallback for array | automation-workflows |
| voice.whisper_vocabulary | ConfigFormTab -> SchemaField text input fallback for array | chat-voice |
| voice.openai_compatible_audio | ConfigFormTab -> SchemaField text input fallback for array | chat-voice |
| tool_approval.policies | ConfigFormTab -> SchemaField text input fallback for array | tool-approvals |
| chat.candidates | ConfigFormTab -> SchemaField text input fallback for array | chat-voice |
| merge_resolution.candidates | ConfigFormTab -> SchemaField text input fallback for array | providers-models |
| skill_description.candidates | ConfigFormTab -> SchemaField text input fallback for array | providers-models |
| context_window_overrides | ConfigFormTab -> SchemaField text input fallback for map/object | providers-models |
| code_index.symbol_summary.candidates | ConfigFormTab -> string list | runtime-infrastructure |
| wiki.roots | ConfigFormTab -> SchemaField text input fallback for array | memory-knowledge |
| wiki.ignore_globs | ConfigFormTab -> SchemaField text input fallback for array | memory-knowledge |

### Fix: Missing Validation

10 rows need bounded controls or backend request validation:

| Option | Problem | Target section |
| --- | --- | --- |
| ui_settings.fontSize | Frontend allows 12-48; type comment says 12-24; backend only checks int. | appearance |
| ui_settings.defaultChatMode | Backend persists arbitrary strings; frontend normalizes with CHAT_MODES. | chat-voice |
| search.mode | bounded to keyword/embedding/auto/hybrid but rendered as free text | runtime-infrastructure |
| ui.mode | bounded to auto/production/dev but rendered as free text | runtime-infrastructure |
| voice.tts_provider | currently supports chatterbox but rendered as free text | chat-voice |
| voice.tts_device | bounded device selector but rendered as free text | chat-voice |
| voice.whisper_model_size | bounded whisper size selector but rendered as free text | chat-voice |
| voice.whisper_device | bounded device selector but rendered as free text | chat-voice |
| voice.whisper_compute_type | bounded compute type selector but rendered as free text | chat-voice |
| chat.default_mode | bounded to normal/accept_edits/bypass/plan but persisted as free text | chat-voice |

### Fix: Dead Frontend / Dead Backend

| Option | Status | Problem | Target section |
| --- | --- | --- | --- |
| ui_settings.sttEnabled | dead-frontend | Persistable in frontend/localStorage but ignored by SaveUISettingsRequest. | chat-voice |
| ui_settings.ttsEnabled | dead-frontend | Persistable in frontend/localStorage but ignored by SaveUISettingsRequest. | chat-voice |
| ui_settings.voiceInputMode | dead-frontend | Persistable in frontend/localStorage but ignored by SaveUISettingsRequest. | chat-voice |
| ui_settings.planPendingVariant | dead-backend | Backend and hook support it, but the overlay needs an explicit control or the option should be dropped. | appearance |

### Drop From Overlay

These rows should not be rebuilt as settings overlay controls:

| Option | Reason |
| --- | --- |
| client.chatMode | Keep per-conversation control in chat, not the global settings overlay. |
| hub_backend | retired operator setting; runtime is fixed to postgres |
| database_url | sole PostgreSQL selector; should stay out of the overlay and secret-backed storage |
| auth.session_secret | auto-generated session cookie signing secret; schema marks it ui_hidden |
| wiki.codewiki_on_commit | CodeWiki generation is dormant pending the wiki redesign (#19665); the retained config key gets a control again only when the daemon surface re-enables. |

## Sign-off

Status: approved.

| Approver | Date | Amendments |
| --- | --- | --- |
| Josh | 2026-06-14 | Approved as-is. |
