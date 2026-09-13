# Configure models and endpoints

Load when changing feature profiles, candidate order, endpoint configuration or
embedding settings. Discover the public configuration schema and current values
first. Model/provider enumeration is evidence of choices, not proof that a
capability is usable or that a profile is configured locally.

Features select `feature_low`, `feature_mid` or `feature_high` and ordered
candidates. Inspect the feature's candidates and `ai.generation.profile_defaults`;
bundled defaults are not installed-state evidence. Preserve provider/model and
reasoning selections. See [feature routing](../../../../../../../../docs/guides/llm-features.md#profile-candidates).

Named generation endpoints live at `ai.generation.endpoints.<name>`.
Use `endpoint:<name>` for the default served model or
`endpoint:<name>/<model>` for a pinned model on surfaces accepting endpoint
model selectors. Feature candidate lists require the explicit
`endpoint:<name>/<model>` form. `local:` selectors and
`ai.generation.local.*` are removed. Endpoint protocol, wire API, authentication
and model must agree; never fabricate probe evidence in a generic patch.

Operator/client activation uses
`PUT /api/config/generation-endpoints/{endpoint_name}/activate` with the observed
revision and endpoint request fields. It probes before committing. Explicitly
select the wire API: activation defaults to `responses`, whereas the ordinary
endpoint model defaults to `chat-completions`. Activation defaults `tool_chat`
to true; ordinary endpoint configuration defaults it to false. Responses requires
an API key and the `openai-compatible` protocol. Inspect `probe_diagnostics` and
capability evidence; a failed tool probe must not be treated as tool support.

Embedding structure (`ai.embeddings.model`, `dim`, `api_base`, `query_prefix`,
`catalog_key`) uses the managed switch lifecycle, not a generic values patch.
Operator `gobby embeddings switch` and `/api/embeddings/switch/*` own that process.
The embedding API key follows normal live secret policy. Never mutate the live
model merely to verify documentation; use isolated fixtures.

Initial embedding setup is operator `gobby install embedding`; its
`--embedding-provider`, `--embedding-model`, `--embedding-url` and
`--embedding-dim` options select installation inputs. Consult current help and
the linked configuration guide before setup; use the switch lifecycle for an
existing indexed installation.

Discover embedding choices with `gobby embeddings catalog`; inspect configuration
health with `gobby embeddings doctor` and an existing switch with
`gobby embeddings switch --status`. `--resume` continues an interrupted run;
`--abort` cannot reverse a switch that has begun flipping active state.
Operator/client `/api/embeddings/status` and `/doctor` expose diagnostics;
POST `/api/embeddings` generates a batch against the configured capability.
These are not MCP configuration tools or evidence that a model switch completed.

If selection fails, inspect served model ids, endpoint reachability and evidence,
then use the owning diagnostics/recovery path. A runtime grant's availability
binding governs CLI capability use; enumeration does not override it. See
[AI configuration](../../../../../../../../docs/guides/ai-configuration.md) and
[storage and embeddings](../../../../../../../../docs/guides/configuration.md#storage-embeddings-and-memory).
