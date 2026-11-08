## Research Findings

### Research Questions

- Does Gobby send telemetry, pings, update checks, crash reports, analytics, version checks, or other requests to projects whose code it uses?
- Does the Zig/Ghostty integration contact upstream during runtime, installation, or builds?
- Which defaults and existing controls change those answers?

### Domain Context

- Task: #24834, “Research: does Gobby send anything to projects we borrowed from or to Zig/Ghostty.” Researcher: gobby#20396, UUID 1e75e36f-6a2a-4348-b656-884f57610c2f.
- Audited 2026-11-06 in the main checkout, /Users/josh/Projects/gobby. Indexed evidence records HEAD fe6bc52e3c67d2937fa427552c4851c45a89c5b5. Existing unrelated working-tree changes were preserved.
- Reviewer correction evidence E21–E23 was collected at HEAD 1113ce61dfbb2add1e323f82426fe84ee86c43d3. Earlier evidence retains its original provenance.
- This is a source and dependency-default audit. No Gobby build, installer, model inference, third-party telemetry submission, container recreation, daemon restart, or packet capture was performed. “Default” means the examined source/distribution default, not a claim about this machine’s installed configuration or observed traffic.
- Scope inspected: crates/ entry points and vendored VT build graph; src/gobby/ runtime, integrations and installers; web/ runtime and npm lifecycle scripts; scripts/; Python, Cargo and npm dependency declarations; installed dependency sources where useful.
- The Orchestrator’s run-specific ruling, relayed by the research lead, keeps this report untracked and delegates closure. No source or task-content edits, Git commit, or link_commit are part of this spike. Research/self-review attestations and review routing are permitted.
- Existing mechanisms already address several contacts. An additional telemetry subsystem is unnecessary to answer this question. Eliminating all upstream contact would require distinguishing telemetry, update checks, functional APIs, and artifact downloads.

### Evidence & Sources

#### Verdict

| Question | Answer | Meaning |
| --- | --- | --- |
| Anything sent to borrowed projects or their infrastructure? | **Yes.** | Impeccable's concept-seed flow can request upstream rolls and explicitly submit opt-out selection telemetry. Its version check is unreachable in Gobby's supported materialized-scripts layout because the required adjacent SKILL.md is absent. The standard Qdrant service reports usage by default unless its own opt-out is configured. |
| Anything sent to Zig/Ghostty? | **Yes during builds/fetch preparation; no upstream runtime reporting found in the examined VT-only integration.** | Zig can download pinned packages from deps.files.ghostty.org. The standalone helper explicitly fetches dependencies. Gobby’s VT library entry point does not initialize the full Ghostty application’s crash/update machinery. |
| Does this prove this machine has sent a particular request? | **No.** | These are supported and default source paths. Cache state, enabled features, installed DB definitions, external service configuration and egress policy determine actual traffic. |
| Are Gobby’s own metrics included? | **No.** | They are outside the brief. Configured functional cloud APIs are identified separately below so ordinary integrations are not mistaken for borrowed-code reporting. |

#### Runtime calls

Evidence IDs refer to the source excerpts below. Each row includes destination, trigger, default and disable method.

| ID / path | Destination and request | Trigger / default | Disable or avoid |
| --- | --- | --- | --- |
| R1 — bundled Impeccable scripts/context.mjs (E1, E21–E23) | impeccable.style, GET /api/version; dormant in Gobby's supported execution path | The CLI calls computeUpdateDirective, which first requires a version in scripts/../SKILL.md. Gobby materializes scripts/** without that file, so it returns before fetching. Execution from the source tree is explicitly forbidden. In a different layout supplying a version-bearing SKILL.md, an eligible invocation checks at most once per 24 hours; failures also advance the cache timestamp. This is not default-on Gobby traffic. | The supported materialized layout already prevents this request. In a layout enabling it, set IMPECCABLE_NO_UPDATE_CHECK=1 or set {"updateCheck": false} in cwd/.impeccable/config.json or config.local.json (local boolean overrides shared). IMPECCABLE_UPDATE_HOST changes the destination. DO_NOT_TRACK is not consulted by this check. |
| R2 — bundled Impeccable scripts/concept-seed.mjs (E2, E3) | impeccable.style, GET /api/roll | The concept-seed flow first tries a valid local concept/composition catalog. If unavailable, it calls the upstream API. The new-work-direction reference explicitly instructs running this script. Query fields are scope, key and reroll, with optional mode, grain and platform. | Avoid that script/flow, provide the valid local catalog via IMPECCABLE_CATALOG_DIR, or set IMPECCABLE_API_URL to a controlled endpoint. No general no-network flag was found. DO_NOT_TRACK and IMPECCABLE_NO_TELEMETRY do **not** stop this GET. An egress denial causes degraded output; it does not erase the attempted request. |
| R3 — same script, pingChosen (E2) | impeccable.style, POST /api/chosen | Only when --chosen is invoked or the exported function is called with a chosenId. API-backed output asks the agent to rerun it when an upstream challenger wins. The POST is enabled absent an opt-out; merely fetching a roll does not itself send it. JSON contains chosenId, key, scope and mode. | IMPECCABLE_NO_TELEMETRY=1 or DO_NOT_TRACK=1; alternatively do not invoke --chosen. These controls are separate from R1 and R2. |
| R4 — same script’s rendered challenger references; scripts/generate-image.mjs | impeccable.style/worlds/cards; api.openai.com/v1/images/generations | Card URLs are produced for a downstream image/browser action; they are not another fetch call inside the seed script. Image generation is an explicitly invoked script/API function. Neither proves background reporting by the daemon. | Do not open external cards or invoke hosted image generation; provide local assets/a local workflow. The card base can be replaced with IMPECCABLE_CARD_BASE. |
| R5 — Qdrant service (E5, E6; upstream sources below) | telemetry.qdrant.io, usage-report POST | Gobby’s Compose service uses qdrant/qdrant:latest and supplies log-level configuration, with no telemetry opt-out. Qdrant documents default reporting for its open-source container. Its reporter sends a report and then sleeps one hour. | Configure the **container** with QDRANT__TELEMETRY_DISABLED=true, telemetry_disabled: true, or --disable-telemetry, and apply that configuration through the service lifecycle. Setting an unrelated daemon process variable alone does not inject it into this Compose service. |
| R6 — web/src/hooks/voice/useVoiceCapture.ts (E4) | cdn.jsdelivr.net/npm/onnxruntime-web@1.24.1/dist/ | Starting MicVAD passes this default ONNX WASM base to the dependency. It is an asset download path when voice/VAD is used, not audio-upload telemetry. The master backend voice setting defaults false (E7). | Leave voice/VAD unused, or build with VITE_VAD_ONNX_WASM_BASE_PATH pointing to locally served compatible assets. The existing local copy-vad-assets script does not itself change this WASM base. |
| R7 — voice/stt.py and voice/tts_chatterbox.py (E7, E8) | huggingface.co; artifact delivery may redirect to Hugging Face’s CDN/Xet infrastructure | When enabled voice providers load models, WhisperModel receives a model name without local_files_only, and ChatterboxTurboTTS.from_pretrained calls snapshot_download. Downloads/metadata checks can occur despite inference being local. Master voice defaults false. | Keep voice disabled; or prepare the exact model cache and start with HF_HUB_OFFLINE=1. An incomplete cache then fails instead of downloading. HF_HUB_DISABLE_TELEMETRY=1 controls library telemetry separately and does not prohibit ordinary model downloads. |
| R8 — optional RTK integration (E9) | Telemetry host is supplied by the third-party binary’s compile-time RTK_TELEMETRY_URL; no fixed host is recoverable from the Gobby source pin | Gobby pins RTK 0.45.0. That version’s source requires explicit stored telemetry consent, enabled configuration, a compiled URL and no environment opt-out before its 23-hour ping. No consent-granting rtk init invocation was found in the examined installer. | RTK_TELEMETRY_DISABLED=1; upstream also provides rtk telemetry disable. The actual installed binary’s URL and consent state were not inspected, so no particular runtime host or request is asserted. |

Impeccable’s comments describe the selection ping more narrowly than its implementation: the body contains four fields, including the seed key. The implementation in E2 is the basis of this report. The skill is genuinely bundled borrowed code: its router attributes the upstream release and instructs materializing scripts from the registry (E3). Inclusion alone does not establish that every script is automatically run.

The runtime verdict uses these primary external sources:

- [Qdrant usage statistics](https://qdrant.tech/documentation/guides/usage-statistics/) documents the default and supported opt-outs. [Qdrant reporter source](https://raw.githubusercontent.com/qdrant/qdrant/master/src/common/telemetry_reporting.rs) identifies the production host, POST and interval. Because Gobby uses latest, this upstream source is a current reference rather than an exact digest match for an installed image.
- [RTK 0.45.0 telemetry implementation](https://raw.githubusercontent.com/rtk-ai/rtk/v0.45.0/src/core/telemetry.rs) and [its versioned telemetry documentation](https://raw.githubusercontent.com/rtk-ai/rtk/v0.45.0/docs/TELEMETRY.md) support R8’s consent gate and controls.
- Installed sources were read without running their networking: .venv/lib/python3.14/site-packages/faster_whisper/utils.py:48-116 (1.2.1), chatterbox/tts_turbo.py:186-204 (0.1.7), and huggingface_hub/constants.py:65-71,167-201 plus utils/_telemetry.py:60-70,109-119 (1.9.2). Hub telemetry is available at {ENDPOINT}/api/telemetry/{path}, with default ENDPOINT huggingface.co, but no call-chain proof was found that the examined Gobby model-loading path emits a separate telemetry event. That capability is **not** counted as an observed or confirmed Gobby telemetry submission.

#### Other functional runtime endpoints

These were encountered during the requested outbound scan. Their presence is ordinary feature behavior; none establishes a report to herdr, Zig, or Ghostty. Configurable destinations are necessarily open-ended.

| Feature / source evidence | Destination host | Trigger / default | Disable or avoid |
| --- | --- | --- | --- |
| Model metadata, src/gobby/llm/model_registry.py:168-188 and runner_model_metadata_refresh.py (E10) | openrouter.ai, /api/v1/models | The model-metadata refresh fetches a public catalog without requiring auth. Exact scheduling/admission belongs to the installed runtime, not the URL constant. | Do not run/admit that refresh, or deny its egress. No dedicated off flag is asserted by this audit. |
| Capability collectors, src/gobby/providers/capabilities/seed.py and collectors/ (E11) | platform.claude.com, code.claude.com, docs.factory.ai | Fetches documentation during a capability refresh using configured/seeded sources. Merely inserting a source row does not make that a network request. | Avoid the refresh or remove/disable the applicable source through authorized configuration. Active schedules were not changed or audited. |
| Skill hubs and remote skill/MCP import (E12, E13) | claude-plugins.dev, skillsmp.com, clawdhub.com; github.com and api.github.com for GitHub collections/imports | Search, install, update and import operations. Some adapters are configured by default, but a configured adapter is not proof of a request at startup. Redirects and configured repositories may add hosts. | Avoid remote searches/installs/updates, use bundled/local sources, or remove the remote hub configuration. ClawHub’s CLI can own additional destinations. |
| Communications (E14) | api.telegram.org, slack.com, discord.com, api.twilio.com, login.microsoftonline.com, login.botframework.com; configured Bot Framework service URL; oauth2.googleapis.com for the email OAuth default; configured SMTP/IMAP hosts | Enabled/configured channel operation, including connection/auth, receives and sends. communications.enabled defaults false. | Leave communications disabled or disable the channel. Dynamic authenticated service URLs and mail servers are configuration-derived, not fixed telemetry endpoints. |
| AI bindings, MCP transports/OAuth, configured webhooks and remote hub/node addresses — context-only, outside the principal findings | Operator/configuration-selected host; api.openai.com is one explicit compatible cloud base in ai/embeddings.py:34 | Requested generation/embedding/transcription/tool/OAuth/webhook or remote-hub operations. Local endpoints are also supported. These background references lack gcode evidence in this report and do not establish a default or a verdict. | Use local bindings, omit remote integrations, or disable the relevant operation. A universal host enumeration is impossible for arbitrary configured URLs. |
| Browser-rendered chat/terminal content — context-only, outside the principal findings | Host in supplied image/link/content | User/agent navigation or loading remote image content; web/src/components/chat/MessageItem.tsx and CodeBlockRenderers.tsx, gclient mouse/links.rs. These background references lack gcode evidence in this report and do not establish a default or a verdict. | Avoid external content/navigation or enforce browser/network policy. Parsed URLs and attribution URLs alone are not automatic fetches. |

#### Build and install fetches

| ID / source | Destination host | Trigger / default | Disable or avoid |
| --- | --- | --- | --- |
| B1 — crates/gterminal/build.rs (E15), vendored manifests (V1) | deps.files.ghostty.org; codeberg.org for the translate-c package | With vt-engine, Cargo runs local Zig with -Demit-lib-vt. Normal Zig dependency resolution may fetch missing packages; the root’s uucode dependency is non-lazy, while many application dependencies are lazy. Nested packages pin SIMD/graphics libraries. Which lazy packages fetch depends on the target/features/build graph. | Supply a **complete** pre-extracted package directory through LIBGHOSTTY_VT_ZIG_SYSTEM_DIR; Gobby passes --system. Cached packages alone can reduce downloads but do not guarantee zero network. [Zig’s own documentation](https://ziglang.org/learn/overview/) says --system disables fetching. |
| B2 — scripts/build_vendored_libghostty_vt.sh:72 (E16) | Same manifest hosts, principally deps.files.ghostty.org and codeberg.org | Explicit helper invocation unconditionally performs zig build --fetch before the actual library build. The helper comment says it warms the machine cache for the package mirror. A manifest URL does not prove that every lazy application package is fetched. | Avoid this fetch helper when a no-network build is required; use the preprepared --system Cargo path. Passing later build arguments to the helper does not remove its first fetch command. |
| B3 — full vendored application dependencies, not the selected VT-only graph (V1) | gitlab.freedesktop.org for the fontconfig source; deps.files.ghostty.org for further app, font and platform dependencies | Only when their lazy packages are requested, or when explicitly preparing a broader dependency set. Do not attribute these URLs to ordinary Gobby VT runtime traffic. | Keep the VT-only target or provide/cache dependencies and disable fetching for the selected build. |
| B4 — vendored src/build/docker/debian/Dockerfile:30-32 | ziglang.org/download/... | The upstream Docker recipe downloads a Zig compiler if someone explicitly builds that recipe. No evidence found that Gobby’s normal Cargo build runs it. Gobby’s own build.rs invokes an already installed Zig binary and fails if absent. | Do not invoke this upstream recipe, or provide a local toolchain in a controlled build. This is the only explicit Zig project download recipe found in this inspected integration. |
| B5 — Cargo.toml/Cargo.lock; pyproject.toml/uv.lock; package-lock files | crates.io/index.crates.io/static.crates.io; PyPI/file delivery hosts; registry.npmjs.org, plus configured registries and Git dependencies | Normal dependency resolution/build/install cache misses. These downloads disclose requested package/version and ordinary connection metadata to their distribution services; they are not evidence of source-code telemetry. Cargo build scripts execute dependency code, so source-level negatives are bounded to examined scripts/dependencies. | Prepopulate/vendor required artifacts, then enforce Cargo --offline and uv --offline, and an offline npm cache/build. Each fails if required artifacts are missing. Do not run resolution to test privacy against live state. |
| B6 — web/package.json postinstall (E17) and installed lifecycle scripts | registry.npmjs.org if esbuild’s platform package is missing | Gobby’s root web postinstall patches local dependencies and copies VAD/Ghostty assets. The lockfile also marks esbuild 0.25.12, fsevents and protobufjs lifecycle behavior. Installed esbuild/install.js:182-187,236-240 can invoke npm and then directly download a platform package. No network request was found in the examined protobufjs postinstall or the four root scripts. | Install the correct platform package/cache before going offline. --ignore-scripts skips lifecycle scripts, including Gobby’s required local asset-copy steps; those outputs must be prepared separately for a working build. |
| B7 — managed Impeccable CLI/browser provisioning (E18) | registry.npmjs.org; storage.googleapis.com/chrome-for-testing-public for Puppeteer’s default Chrome artifacts | Gobby installs its pinned CLI using npm ci --ignore-scripts --no-audit --no-fund --omit=dev. A separate browser-cache helper explicitly runs puppeteer browsers install chrome when it needs that browser. Thus ignoring npm lifecycle scripts does not make browser preparation offline. | Reuse a complete verified CLI/browser cache or avoid browser-requiring flows. Do not assume PUPPETEER_SKIP_DOWNLOAD disables an explicitly invoked browser-install CLI. [Puppeteer’s official Chrome settings](https://pptr.dev/api/puppeteer.chromesettings) identify the default download host and override. |
| B8 — RTK installer (E9) | github.com, with GitHub’s release-asset delivery hosts; Homebrew infrastructure when using brew | Consent-based optional RTK provisioning: prefer an existing compatible binary; installer can run brew install rtk and has a pinned release fallback. | Decline RTK provisioning or provide a compatible local binary. Homebrew’s own analytics/update policy is independent of Gobby and was not changed. |
| B9 — database service provisioning (E5, E19) | Docker Hub registry/auth/layer delivery; github.com for paradedb/paradedb releases; Debian package mirrors in the PostgreSQL image build | Docker Compose pulls missing Qdrant/FalkorDB images. Gobby’s PostgreSQL Dockerfile downloads the pinned pg_search Debian package and installs OS packages. These are install/build operations. | Preload trusted images and build inputs and use an offline build; avoid recreating services during the audit. Registry redirect/CDN hosts depend on the distribution response. |
| B10 — Gobby helper release/version management (E20) | api.github.com, github.com; crates.io API in cli/install_setup.py | Explicit installation/release lookup and binary freshness paths. These refer to Gobby’s own binaries/releases, not telemetry to borrowed projects. | Reuse local binaries or keep the specific install/update/freshness operation offline. No blanket runtime-off switch is asserted. |
| B11 — maintenance/research scripts — context-only, outside the principal findings | code.claude.com in scripts/refresh_trusted_domains.py:23; openrouter.ai in scripts/decisions_live_capture.py:306 | Explicitly running those scripts refreshes a documentation-derived allowlist or calls a provider catalog/API. These background references lack gcode evidence in this report and do not establish a default or a verdict. Merely shipping them does not execute them. | Do not invoke them, or supply controlled/offline inputs where supported. |

The web Ghostty copy script resolves @wterm/ghostty from node_modules and copies its WASM into the served assets (E17). Its copy operation is local. No Ghostty upstream runtime-reporting endpoint was found in that path. Running a fresh npm install remains a separate registry operation.

#### Dependency defaults

- **Qdrant is a positive default-on finding.** Its telemetry is separate from Gobby’s own metrics. The Compose file’s latest tag prevents a reproducible guarantee about every dependency behavior without an image digest and version-specific source. No installed service configuration was changed.
- **Ghostty application configuration is not the VT library graph.** The vendored Config.zig:224-235 has a macOS/iOS Sentry build default, and SharedDeps.zig:414-439 contains application Sentry linkage. However, GhosttyLibVt.zig:230 roots the library in zig.vt_c; GhosttyZig.zig:118 roots that module in src/lib_vt.zig and adds the terminal/Unicode/graphics/SIMD dependencies at lines 132-156. That graph inspection found no Sentry application initialization or updater. Treating the generic config default as proof that gterm sends crash reports would be unsupported.
- **herdr and portable-pty:** gclient/NOTICE.md attributes herdr; gterminal/UPSTREAM.md and NOTICE.md document borrowed terminal/PTY code. The examined client transport targets the configured Gobby daemon; its HTTP/WebSocket clients do not establish a request to herdr’s repository or maintainer. No fixed herdr reporting endpoint was found. This is a scoped source negative, not proof about arbitrary child processes or URLs opened by a user.
- **Hugging Face voice dependencies:** artifact networking is expected on a cache miss unless offline mode is set. The installed Hub library offers separate optional telemetry APIs and accepts HF_HUB_DISABLE_TELEMETRY, DISABLE_TELEMETRY and DO_NOT_TRACK. Offline mode also suppresses those telemetry submissions. Presence of this API does not prove that Gobby calls it.
- **RTK:** telemetry is conditional on explicit consent in the pinned source, not automatically authorized by Gobby’s decision to install/use it. A user may already have a different RTK installation/configuration; this audit did not execute or inspect its telemetry status.
- **ClawCare 0.9.0:** src/gobby/skills/scanner.py uses deterministic built-in rule scanning. A read-only search of the installed clawcare package found no http(s) endpoints, HTTP client calls, telemetry or analytics implementation. No separate model/cloud scanner is invoked by the examined wrapper.
- **ParadeDB pg_search 0.23.4:** the repository pins that package in data/postgres-pgsearch/version.json. The versioned upstream tree, pg_search/Cargo.toml and src/lib.rs initialization were checked; no dedicated telemetry module/initialization was identified. Older third-party forks discussing paradedb.pg_search_telemetry are not reliable evidence for this pinned release. This is not a complete audit of all extension dependencies, nor a claim that an arbitrary latest database image is network-silent. Sources: [versioned manifest](https://raw.githubusercontent.com/paradedb/paradedb/v0.23.4/pg_search/Cargo.toml), [versioned entry point](https://raw.githubusercontent.com/paradedb/paradedb/v0.23.4/pg_search/src/lib.rs).
- **FalkorDB Browser / Next.js:** the current upstream [browser Dockerfile](https://raw.githubusercontent.com/FalkorDB/falkordb-browser/main/Dockerfile) sets NEXT_TELEMETRY_DISABLED=1 in both builder and runner stages. Gobby’s falkordb:latest reference does not pin that source/image correspondence. This is supporting context, not an attestation about this machine’s actual container.
- **Other SDKs/frameworks:** a dependency declaration, funding URL, XML namespace, documentation link or test URL is not by itself a network call. In particular, installed Gradio/transformers dependencies and OpenTelemetry packages do not prove an active Gradio app or vendor-directed collector. Gobby’s own configured metrics/exporter behavior remains outside scope.

#### Reproduction and limitations of the evidence

- Discovery used gcode grep, search-content, outline and symbol-at, with gcode evidence for the cited indexed sources. Searches were locators; a paginated hit list was not used as proof of universal absence.
- Vendored Ghostty files are excluded from the caller’s index. A gcode evidence range request for crates/gterminal/vendor/libghostty-vt/build.zig.zon failed with invalid_selector / “path is not visible in the caller’s index.” The V1 citations therefore use direct, read-only file/range inspection as the documented evidence fallback.
- Installed node_modules and .venv sources are also outside the repository code index. They were inspected directly, with package versions recorded. npm lock metadata was parsed without running lifecycle scripts. No dependency was installed or upgraded for this report.
- External sources were read as documentation/source evidence. No attempt was made to exercise outbound dependency behavior or bypass an egress denial. The lead reported sandbox violation noise; no SRT denial with a concrete request target surfaced in the research tool results used here. Unsupported flags, missing-path probes and workflow bootstrap blocks were corrected without changing repository code.
- A “no runtime reporting found” conclusion is bounded to the examined entry points and pinned sources. Dynamically configured MCP servers, skills, child coding CLIs, user commands, arbitrary web content, browser extensions, system tooling and unpinned container revisions can add traffic.

#### Indexed evidence catalog

These hashes identify gcode evidence excerpts. A displayed subset retains the hash of the returned full range; omitted lines are not claimed to be displayed.

- **E1** `src/gobby/install/shared/skills/impeccable/scripts/context.mjs:1007-1007`, excerpt_hash `e639c54211ed4fc8c02401e1af9e1ff8653ea27e82eae41e69236440c1318d05`:

```text
1007|     const res = await fetch(`${UPDATE_HOST}/api/version`, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
```

`src/gobby/install/shared/skills/impeccable/scripts/context.mjs:1048-1048`, excerpt_hash `9fa4a2bd13d7787919ec305b5f262a1d724f0aaaf4ad6a733a34f3d2f17098a9`:

```text
1048|     if (process.env.IMPECCABLE_NO_UPDATE_CHECK) return null;
```

`src/gobby/install/shared/skills/impeccable/scripts/context.mjs:1100-1100`, excerpt_hash `301371248479e579351e629210e2bff01614d1f205b1177061f3058a289b6275`:

```text
1100|   const updateDirective = await computeUpdateDirective();
```
- **E2** `src/gobby/install/shared/skills/impeccable/scripts/concept-seed.mjs:145-190`, excerpt_hash `0f885d1fc041a8c7f490e3cabb988ee02c721cf2eabaf47b943771df124dc5f0`:

```text
146|   const params = new URLSearchParams({ scope, key, reroll: String(reroll) });
147|   if (mode) params.set('mode', mode);
148|   if (grain) params.set('grain', grain);
149|   if (platform) params.set('platform', platform);
156|       fetch(`${API_BASE}/roll?${params}`, { signal: controller.signal }),
172|   return Boolean(process.env.IMPECCABLE_NO_TELEMETRY || process.env.DO_NOT_TRACK);
178|   if (telemetryDisabled() || !chosenId) return false;
182|     await fetch(`${API_BASE}/chosen`, {
183|       method: 'POST',
185|       body: JSON.stringify({ chosenId, key, scope, mode }),
```
- **E3** `src/gobby/install/shared/skills/impeccable/SKILL.md:1-154`, excerpt_hash `8407b577bba12e15b5313263dcc9748261cbe807915949f4d1b777f7d9f8e252`:

```text
22| Impeccable — Copyright 2025-2026 Paul Bakaus. Licensed under Apache 2.0.
23| Based on Anthropic's frontend-design skill (Copyright 2025 Anthropic, PBC, Apache 2.0).
26| Upstream: https://github.com/pbakaus/impeccable (v3.5.0, commit a075d89b)
27| The `impeccable` skill ships with all 39 released 4.0.4 reference files plus
28| seven Gobby-retained domain references under `references/`, and the full 4.0.4
29| script release under `scripts/`.
31| `get_skill_file(name="impeccable", path="references/<cmd>.md")` on
32| `gobby-skills`; scripts run from the cache directory returned by
33| `materialize_skill_scripts(name="impeccable")`.
132| 1. Call `materialize_skill_scripts(name="impeccable")` on `gobby-skills`. It
133|    writes the canonical `scripts/**` bytes from the registry into a
134|    content-addressed cache, installs the locked dependencies, and returns
135|    `scripts_dir` plus `environment.PUPPETEER_CACHE_DIR`.
```
- **E4** `web/src/hooks/voice/useVoiceCapture.ts:12-16`, excerpt_hash `45266ae8034bd1d696044782fd3d12e9a47d10131895c6c136ae639735e70bcf`:

```text
12| const VOICE_CAPTURE_WORKLET_URL = "/audio-worklets/voice-capture-processor.js";
13| const DEFAULT_ONNX_WASM_BASE_PATH =
14|   "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.24.1/dist/";
15| const ONNX_WASM_BASE_PATH =
16|   import.meta.env.VITE_VAD_ONNX_WASM_BASE_PATH || DEFAULT_ONNX_WASM_BASE_PATH;
```
- **E5** `src/gobby/data/docker-compose.services.yml:1-116`, excerpt_hash `6ef3f2579e1e93c69c7908c789e8050982224b9d54f1eac38dd008abd61fc475`:

```text
7|     image: falkordb/falkordb:latest
40|   qdrant:
41|     image: qdrant/qdrant:latest
45|     environment:
46|       # Auth disabled for local-only access (no TLS = no point in API key)
47|       # To enable: set QDRANT__SERVICE__API_KEY and configure TLS
48|       - QDRANT__LOG_LEVEL=${GOBBY_QDRANT_LOG_LEVEL:-WARN}
61|   postgres:
62|     build:
63|       context: ./postgres-pgsearch
64|       args:
65|         PG_SEARCH_VERSION: ${GOBBY_PG_SEARCH_VERSION:-0.23.4}
66|         PG_SEARCH_SHA256: ${GOBBY_PG_SEARCH_SHA256}
```
- **E6** `src/gobby/cli/installers/qdrant.py:1-180`, excerpt_hash `e14a7b40ebfcc88ea721c0753342a4d5827df2346a55a4815f1b68812dd81892`:

```text
4| Handles Docker-based Qdrant setup for vector search. Qdrant is installed by default
5| during `gobby install` when Docker is available.
```
- **E7** `src/gobby/config/voice.py:62-155`, excerpt_hash `5850ac02502ceb563163795eae55b925bdb091356539d3d8c1333aac3b3da609`:

```text
69|     enabled: bool = Field(
70|         default=False,
75|     tts_enabled: bool = Field(
76|         default=True,
79|     tts_provider: str = Field(
80|         default="chatterbox",
138|     stt_enabled: bool = Field(
139|         default=True,
147|     whisper_model_size: str = Field(
148|         default="base",
```
- **E8** `src/gobby/voice/stt.py:39-39`, excerpt_hash `315be5d6f4edd635b16832b70298d7732163d91a88cb1616d644f1a471da63b6`:

```text
39|     model: _WhisperModelProto = WhisperModel(
```

`src/gobby/voice/tts_chatterbox.py:411-411`, excerpt_hash `b9b11026cb2fcbfab462cbca3881d2b54c3904480600f4ed8820793520a79476`:

```text
411|                     model = ChatterboxTurboTTS.from_pretrained(device=device)
```
- **E9** `src/gobby/integrations/rtk.py:18-18`, excerpt_hash `4df0006b94b22b46a07ebe52dbbe76cf468c923c28c626a5ea604bf7ec5b7d57`:

```text
18| RTK_VERSION = "0.45.0"
```

`src/gobby/cli/install_setup_rtk.py:35-35`, excerpt_hash `f01705a6f02a9da04b335b2fccd5dc224212ed96a038e31a17dbb35c67e58c7f`:

```text
35| _RELEASE_BASE = f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}"
```
- **E10** `src/gobby/llm/model_registry.py:30-30`, excerpt_hash `26a7095e70aed067287762c8dc936996a0b80b61f8e7fd26e38ac8133eba5b0e`:

```text
30| OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
```
- **E11** `src/gobby/providers/capabilities/seed.py:21-21`, excerpt_hash `478972dadb15887420c0d8101a56800cd4726dc75de5ef4d7ba511f4ac1f28c0`:

```text
21|         "https://platform.claude.com/docs/en/about-claude/models/overview.md",
```

`src/gobby/providers/capabilities/seed.py:23-23`, excerpt_hash `f5a738131d7bfe34a5aaae4e99823612f434d7b04464dd09ecccd2467918daa6`:

```text
23|     ("model-config", "https://code.claude.com/docs/en/model-config.md"),
```

`src/gobby/providers/capabilities/seed.py:24-24`, excerpt_hash `bc7e335ceed35303a8334793970b6ead6179508e4865540734aba2a501256ef4`:

```text
24|     ("effort-docs", "https://platform.claude.com/docs/en/build-with-claude/effort.md"),
```

`src/gobby/providers/capabilities/seed.py:26-26`, excerpt_hash `bb7641338667bdf8141c604df2a91479bfae887c614cd6670be1252b390d53bb`:

```text
26| _DROID_SOURCES = (("factory-models", "https://docs.factory.ai/models.md"),)
```
- **E12** `src/gobby/config/skills.py:132-132`, excerpt_hash `1e9f95c77ec74eaaf8173cbfd84aa3f30e3a4a8ea9cc24e96bbbb692e2fcfe8c`:

```text
132|                 base_url="https://claude-plugins.dev",
```

`src/gobby/config/skills.py:139-139`, excerpt_hash `d8fab757552a7b846aa7a47417d8d32c6c95d36f71cc758152474197386745b0`:

```text
139|                 base_url="https://skillsmp.com/api/v1",
```

`src/gobby/skills/hubs/manager.py:62-62`, excerpt_hash `5e38de10e69fc7b807b0dce67b3db47b5f27d32b0ab64bf99cefd37f48b26ef2`:

```text
62|             "clawdhub": HubConfig(type="clawdhub", base_url="https://clawdhub.com"),
```
- **E13** `src/gobby/mcp_proxy/importer.py:35-35`, excerpt_hash `a835605f833b2c11e981199f7c8eae7f859497c0eaf50dd074c272a10be7944a`:

```text
35| GITHUB_API_BASE = "https://api.github.com"
```

`src/gobby/skills/_loader_models.py:21-21`, excerpt_hash `228d949aa215f9b31fbbd0b1d8fa08e72d2b0f0aae8da595cfbd63409415bcc5`:

```text
21|         return f"https://github.com/{self.owner}/{self.repo}.git"
```

`src/gobby/skills/hubs/github_collection.py:128-128`, excerpt_hash `e04fe6a77cf27400ff654a175a8d7ee7c4c3d475449fdd3ad1cb80e98c147ffa`:

```text
128|         url = f"https://api.github.com/repos/{owner}/{repo}/contents"
```
- **E14** `src/gobby/communications/adapters/discord.py:107-107`, excerpt_hash `4db84a286f3693556991abb33d370125d8bad3c7972856f996869da621cec948`:

```text
107|             base_url="https://discord.com/api/v10",
```

`src/gobby/communications/adapters/email.py:167-167`, excerpt_hash `5ea7d3c495b286f602719621336898bf516f56119cf8ca356c2c7fb961d948f6`:

```text
167|             "oauth2_token_url", "https://oauth2.googleapis.com/token"
```

`src/gobby/communications/adapters/slack.py:84-84`, excerpt_hash `c0bdc7120172fd0fa92ee8f44df6a55c03c75d67f3ac741c4ccc17bc9fb5bd19`:

```text
84|                 base_url="https://slack.com/api/",
```

`src/gobby/communications/adapters/sms.py:87-87`, excerpt_hash `4f3a9798bd11a2f5525fd87c0ed084cde5b34c197b193fdbc51da05fdd170b92`:

```text
87|             base_url=f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/",
```

`src/gobby/communications/adapters/teams.py:23-23`, excerpt_hash `89f498942d21895882b252f69fe6ca0f7167f9fb3f9d3733c1ec90f798b8ff92`:

```text
23|     _BOTFRAMEWORK_JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
```

`src/gobby/communications/adapters/teams.py:91-91`, excerpt_hash `c36a42a3165f96af6ac83c3f6a6a29fc26c75f26e350c1deabf3b6edcb01d7d7`:

```text
91|         token_url = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
```

`src/gobby/communications/adapters/telegram.py:263-263`, excerpt_hash `96147b31fba5c9ac18e8a4a2e14af4af7af2835051731cb8d371e944dc026814`:

```text
263|         self._api_base = f"https://api.telegram.org/bot{self._bot_token}"
```

`src/gobby/config/communications.py:21-21`, excerpt_hash `ed74b2edec609cd7790bcd8379394376ed9d49f71e5b0bea7d6d4e3e9e2c752b`:

```text
21|     enabled: bool = False
```
- **E15** `crates/gterminal/build.rs:89-89`, excerpt_hash `0dbafddfe9f83e06578b89146bcc2ae9152b00d441db7cf49209af38a002c54d`:

```text
89|         .arg("-Demit-lib-vt")
```

`crates/gterminal/build.rs:102-102`, excerpt_hash `143a9c948ff44e799ec90242ef496f38744794c87c06bfaf3141e7d8e1602e9d`:

```text
102|         command.arg("--system").arg(system_dir);
```
- **E16** `scripts/build_vendored_libghostty_vt.sh:72-72`, excerpt_hash `d4786c09a592c037ea78c204ded192f524443a0ec3d87154738f2606e2380fec`:

```text
72| "$ZIG_BIN" build --fetch --global-cache-dir "$HOME/.cache/zig"
```
- **E17** `web/package.json:18-18`, excerpt_hash `70b08e22c5052bb19f1ee537e0fb8b7febd451931ca61078911e47c76fc74d18`:

```text
18|     "postinstall": "node scripts/patch-tailwind-node-dep0205.mjs && node scripts/patch-jsx-a11y-minimatch.mjs && node scripts/copy-vad-assets.cjs && node scripts/copy-ghostty-wasm.cjs",
```

`web/scripts/copy-ghostty-wasm.cjs:11-11`, excerpt_hash `94eea2d03b281c4a2bfc28ee17fde517a7882b7e911a201bcd5c17991f9f60d9`:

```text
11|   const packageEntry = require.resolve("@wterm/ghostty");
```

`web/scripts/copy-ghostty-wasm.cjs:18-18`, excerpt_hash `2a623cd1f9f25c0541057ed23a3d6782f9fa8ad6f9f299512498a69f54048b8c`:

```text
18|   fs.copyFileSync(src, DEST);
```

`web/scripts/copy-vad-assets.cjs:28-28`, excerpt_hash `4d2acb02cf379117f91b4fbb9c27a6cc32cde324a28e448ce8ca0c9728758089`:

```text
28|     fs.copyFileSync(src, dest);
```
- **E18** `src/gobby/cli/install_setup_impeccable.py:469-469`, excerpt_hash `197c498f207745cbe5adc335e4949701b20d7aba85af0d77a56539b4872adaec`:

```text
469|                 "--ignore-scripts",
```

`src/gobby/cli/install_setup_impeccable.py:470-470`, excerpt_hash `0c19414f2e6f15dc5abd523614f85e67532bbc7c682dbb03367b6138ccf66e2c`:

```text
470|                 "--no-audit",
```

`src/gobby/cli/install_setup_impeccable.py:471-471`, excerpt_hash `9f325ea438ec67de8c7bae1fe77fd44848949d3f51cd00069fce2eb8ce25e083`:

```text
471|                 "--no-fund",
```

`src/gobby/cli/install_setup_impeccable.py:761-761`, excerpt_hash `55c47c12213ef9f811b37c1717d372cfbab0045450ec19c841804469aba18d01`:

```text
761|             [str(puppeteer), "browsers", "install", "chrome"],
```
- **E19** `src/gobby/data/postgres-pgsearch/Dockerfile:19-19`, excerpt_hash `dc39627ba17ef521de711b53c614854706fcd97a25daaec637adabf6cd30f775`:

```text
19|         "https://github.com/paradedb/paradedb/releases/download/v${PG_SEARCH_VERSION}/postgresql-18-pg-search_${PG_SEARCH_VERSION}-1PARADEDB-trixie_${arch}.deb" \
```
- **E20** `src/gobby/cli/install_release.py:57-57`, excerpt_hash `7491058fd854ab7c1e3080ef7db8bb718036a564c684f1992c0042c2764205c0`:

```text
57|         f"https://github.com/{_HELPER_RELEASE_REPOSITORY}/releases/download/"
```

`src/gobby/cli/install_release.py:73-73`, excerpt_hash `8c9eff824314363306809ebe58bded58af751d0b29bbf5213f52afc10b913abd`:

```text
73|         f"https://api.github.com/repos/{repository}/releases?per_page=100",
```

`src/gobby/install/bin_freshness_github.py:98-98`, excerpt_hash `9917138673c58c2b9847b54ee6b9efc9516717885879a1438f340bd5304d18b1`:

```text
98|         url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
```

- **V1 — direct vendor fallback:** `crates/gterminal/vendor/libghostty-vt/build.zig.zon:6,15-45,50-63,91-122`; `pkg/translate-c/build.zig.zon:15`; `pkg/fontconfig/build.zig.zon:8`; `src/build/GhosttyLibVt.zig:230`; `src/build/GhosttyZig.zig:118,132-156`; `src/build/Config.zig:224-235`; `src/build/SharedDeps.zig:414-439`. These paths are relative to the vendored root where abbreviated. Direct reads were used because gcode rejected the vendor manifest as outside the visible index. In particular, the VT root imports terminal/Unicode/graphics/SIMD code and does not add application Sentry there.

- **E21** `src/gobby/install/shared/skills/impeccable/scripts/context.mjs:965-1065`, excerpt_hash `49e89dc50f79a410375ce6037dacf7ad7c36e0eab1306bef9c41f076c33d3819`:

```text
965| function readLocalSkillVersion() {
968|     const skillMd = path.join(here, '..', 'SKILL.md');
969|     const content = fs.readFileSync(skillMd, 'utf-8');
970|     const match = content.match(/^version:\s*(.+)$/m);
971|     return match ? match[1].trim().replace(/^["']|["']$/g, '') : null;
972|   } catch {
973|     return null;
1035| function updateCheckDisabledByConfig(cwd = process.cwd()) {
1037|   for (const name of ['config.json', 'config.local.json']) {
1039|       const raw = JSON.parse(fs.readFileSync(path.join(cwd, '.impeccable', name), 'utf-8'));
1040|       if (raw && typeof raw === 'object' && typeof raw.updateCheck === 'boolean') value = raw.updateCheck;
1043|   return value === false;
1048|     if (process.env.IMPECCABLE_NO_UPDATE_CHECK) return null;
1049|     if (updateCheckDisabledByConfig()) return null;
1050|     const localVersion = readLocalSkillVersion();
1051|     if (!localVersion) return null;
1057|     if (!cache.lastCheck || now - cache.lastCheck > CHECK_INTERVAL_MS) {
1058|       const latest = await fetchLatestSkillVersion();
```

- **E22** `src/gobby/skills/materialization.py:295-390`, excerpt_hash `3f6901275209bef529552af9de26a7dde3b6f50246ee6e4f01b0f3ff3ee4ca5f`:

```text
321|     return SkillMaterializationResult(
322|         scripts_dir=(generation / "scripts").resolve(),
375| def _validate_script_path(path: str) -> None:
378|         not path.startswith("scripts/")
384|         raise SkillMaterializationError(f"Unsafe skill script path: {path}")
```

The materializer's payload-path contract excludes a sibling SKILL.md. The reviewer additionally reported that its read-only inspection of the live materialization cache found provenance.json plus scripts/ and zero SKILL.md files; that observation is reviewer evidence, not a cache inspection performed by this researcher.

- **E23** `src/gobby/install/shared/skills/impeccable/NOTICE.md:213-228`, excerpt_hash `010cadaec99e12e13a982df97b83bf5d1686e9c9a1b1a66654e6ed646bce7b30`:

```text
221| - Execution model: scripts are synced into the skill-files registry and run
222|   from the content-addressed cache returned by
223|   `materialize_skill_scripts(name="impeccable")` — never from this source tree
224|   (installed skills may have no disk tree).
```

#### Validation criteria coverage

1. This file is .gobby/plans/research/outbound-calls-borrowed-projects-2026-10.md and contains Runtime calls, Build and install fetches, Dependency defaults and Verdict.
2. Findings above identify source paths, destinations, triggers, default state and disable/avoid methods. The evidence catalog above binds the principal repository claims to gcode excerpts; explicit fallback citations cover excluded vendor/dependency source.
3. Verdict answers borrowed projects and Zig/Ghostty separately, including runtime versus build timing.
4. Only this research artifact is authored by this spike. Pre-existing foreign files are preserved. No source, task title/description/criteria/parentage or other task content is edited.
5. Send this artifact path, its SHA-256, this summary and checks to the reviewer and research lead for routing to Triage/Orchestrator. Delivery is recorded in the authenticated coordination messages, not inferred from this paragraph.

### Risks

- The operator should not be told “Gobby never phones home to borrowed projects”: Impeccable and Qdrant contradict that statement. Equally, a Ghostty dependency download should not be described as runtime terminal telemetry.
- Existing opt-outs are feature-specific. DO_NOT_TRACK does not disable the examined Impeccable roll GET. The version-check implementation also ignores it, but is dormant in Gobby's supported materialized layout; environment and config-file controls additionally exist for other layouts. HF_HUB_DISABLE_TELEMETRY does not make model loading offline.
- If eliminating unsolicited third-party reporting becomes approved implementation work, the smallest decisions concern Qdrant’s container opt-out and the bundled Impeccable selection default. The dormant version check should not be treated as an active default to disable. A fully offline product additionally needs local VAD assets, prepared model/browser/package caches and a policy for intentional remote features. No such changes are made or proposed as already authorized by this research task.
- The static audit cannot establish historical transmission or an exact installed-state destination for RTK’s compiled URL or latest container images. Those uncertainties do not weaken the positive source findings.
