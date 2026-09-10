Plan artifact: `.gobby/plans/wiki-code-bakeoff.md`

# Code Wiki Bakeoff — Reproducible Evidence for Gobby

**Plan ID:** wiki-code-bakeoff

## Overview
`kind: framing`

Run a fresh, isolated code-wiki bakeoff against the entire Game Goblins repository. Preserve complete native outputs, reproducible execution records, per-test measurements, and evidence comparing Graphify with current gcode.

This epic supplies the subsequent analysis epic: reviewing outputs, identifying actionable gcode gaps, and establishing the canonical specification and example files for Gobby's wiki. No gwiki or gcode implementation, canonical Gobby output design, general-knowledge bakeoff, or custom UI work belongs in this epic.

## Constraints
`kind: framing`

**Frozen inputs**

Game Goblins baseline: `0216f1e33f05962d49467d95fe84609041c6dba8`. Committed change: `8b24ac26699aac8b24254a647aa70b208287b492`, adding name-prefix store-minimum behavior. Include shared platform, Restocks, Buylist, tests, manifests, configuration examples, and relevant documentation. Plans/historical documents are intent/history evidence; verify implemented behavior against source. Exclude untracked files, ZIP downloads, runtime data, credentials, generated wikis, and operational Gobby connection/project metadata. Review retained configuration examples for secrets before model access and preserve an explicit exclusion inventory. Ground-truth answer keys stay outside comparator input directories.

Comparator versions are frozen:

- Graphify: `Graphify-Labs/graphify`, `c9f99018774e2e0380e9f65b3959944559a0d5f6`, v8 / package 0.9.55.
- Understand Anything: `Egonex-AI/Understand-Anything`, `07edf82a04371b6f69779b067bdc8a1a8753a9db`.
- Archify: `tt-a1i/archify`, `c6519401f7b91b9d43011657880893b0a8955548`.
- CodeWiki: `FSoft-AI4Code/CodeWiki`, `2584854d7538dc3e3e8e6839cf8590b0cd12a431`.
- OpenDeepWiki: `AIDotNet/OpenDeepWiki`, `75840e5e86213ca40ace9d5036b1f52603f8d038`.
- Grok Wiki: `AsyncFuncAI/grok-wiki`, release 0.0.38 Apple Silicon, not the old DeepWiki-Open product. DMG SHA-256: `bbcccef258102a1f32e36947b1a6da059a828bf1cea552dfdb58ca35ab562e09`.
- gcode: build from Gobby `7394b97c1d88c82f685e788e798de2cfd728ad15`, version 1.7.0.

Record executable hashes, dependency locks, and container image digests before runs; no silent version updates.

**Isolation and artifact ownership**

Runtime root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`. Tracked evidence root: `docs/evidence/wiki-bakeoff-code-2026-09/`. Give each comparator independent source copies, outputs, caches, and state. Preserve complete native artifacts under the runtime root; tracked reports contain inventories, hashes, reproducible commands, and exact artifact locations.

Leave the source repository, live Gobby stores/vaults, global CLI configuration, and existing application state unchanged. Run gcode against isolated PostgreSQL, Qdrant, FalkorDB, and an isolated daemon using the normal signed-grant contract. Validate rendered container configuration before launch; reject shared production container names, volumes, connections, or non-loopback bindings. Do not run Game Goblins operational jobs, Lightspeed/Slack integrations, or the live application database. No public publishing.

Leave the old bakeoff and discussion checkpoint untouched; do not use old outputs as new results. Scoped retirement is separate work. This epic is manually activated and does not alter unrelated grant reservations or roadmap work.

**Models, scheduling, and measurement**

Use `gpt-5.6-terra` with medium reasoning for the hosted baseline through actual Codex subscription paths. Run one small common `gpt-5.6-luna`/medium calibration; after calibration Luna may handle bounded evidence packaging, not a duplicate full matrix. Use local `qwen/qwen3.8-27b` with xhigh reasoning for local compatibility and OpenDeepWiki generation, subject to adapter verification. Do not disable thinking to manufacture compatibility. Embeddings: `text-embedding-nomic-embed-text-v1.5@f16`; verify 768 dimensions, prefixes, and context limits on adapters that use embeddings.

At most two independent hosted generator runs concurrently; local-model runs are serial. Record native child fan-out and use conservative internal concurrency. Probe deadline: 15 minutes; hosted generation: 60 minutes; local generation: 120 minutes. Allow one diagnosed whole-run retry, preserving failed attempts separately. Further repeats or extensions require an explicit continuation decision.

Measure every case: requested/effective provider, model and reasoning; prompts; model calls; available token counters; retries; start/end times; output counts; changed files; warnings; observable subscription quota. Mark unavailable counters unknown. Silent model fallback invalidates a baseline. Do not convert API prices into subscription quota estimates.

The coordinator uses Gobby-managed Codex workers with model `gpt-5.6-sol`, reasoning `xhigh`, and required reasoning. Workers operate the experiment; they do not replace comparator-generation models. Use Gobby agents and existing pipelines, scheduling, cancellation, and recovery; no separate agent runtime or wiki queue.

All deliverable targets are new evidence documents, not production symbol changes, consumer migrations, or generated production carriers. Runtime artifacts belong to their corresponding leaf and must remain inventory-addressable even when too large to commit.

## P1: Establish the test contract and environment
`kind: framing`

### 1.1 Create the matrix and Graphify/gcode casebook [category: docs]
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`

Define common questions, source inventory, controlled fixtures, run profiles, and measurement format before generation. Cover purpose/ownership; synchronization/normalization; daily reporting; forecasting/weekly planning; approval/live-write boundaries; workbooks; recovery; Restocks; Buylist; and implemented versus proposed behavior.

Every question has a commit-pinned, directly verified source answer or explicit ambiguity. Verify against source independently so neither index defines its own ground truth. Keep answer keys outside comparator input directories.

The Graphify/gcode casebook separates deterministic extraction from model-derived semantics. Compare calls, imports, paths, communities, relationship provenance, comments/docstrings, rationale/document references, and absent evidence versus retrieval failure or source ambiguity. Credit already-delivered gcode functionality.

Define C0 compatibility/effective configuration; C1 cold baseline; C2 unchanged rerun; C3 frozen committed change; C4 rationale/comment change, C5 rename, and C6 removal as isolated evidence-index fixtures; C7 bounded interruption/recovery where native workflow exists; C8 native retrieval/Ask; and C9 native export/presentation. State each case's inputs, expected observations, measurements, and pass/fail or demonstrated-unsupported criteria. This is an evidence contract, not canonical Gobby wiki design.

**Acceptance:**

- 1.1.1 - Every case specifies inputs, expected observations, metrics, and pass/fail or unsupported criteria. file: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`.
- 1.1.2 - Ground truth covers the shared platform, Restocks, and Buylist, distinguishing direct support, inference, and ambiguity with commit-pinned evidence. file: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`.
- 1.1.3 - The index casebook credits existing gcode capabilities and requires reproducible evidence for every difference. file: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`.

### 1.2 Provision the isolated environment [category: test] (depends: 1.1)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`

Read the frozen matrix. Provision independent corpus copies and pinned installations under the runtime root using project-local skills/tools, not global installers replacing user configuration. Reuse existing Gobby isolation patterns and service images with independently owned paths, credentials, databases, volumes, ports, and daemon state. Build pinned gcode without replacing the shared binary and exercise the normal signed-grant path.

Use Grok Wiki's documented separate storage root. Give OpenDeepWiki isolated state, English-only output, one local generation slot, and disable unrelated scheduled/optional generation. Record commands, dependency locks, image digests, rendered configuration checks, ports, paths, and teardown ownership. Stop only recorded owned processes/containers; retain evidence. Missing installation is an explicit reproducible blocker, not cohort substitution.

**Acceptance:**

- 1.2.1 - Configuration and before/after observations prove the source repository, live stores, global configuration, and existing application state are outside the write scope. file: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`.
- 1.2.2 - Every comparator has a verified pinned installation or explicit reproducible installation blocker. file: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`.
- 1.2.3 - gcode uses the isolated daemon, signed grants, PostgreSQL, Qdrant, and FalkorDB; service identity and containment checks pass. file: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`.

### 1.3 Capture calibration and provider compatibility [category: test] (depends: 1.2)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/compatibility.md`

Read the matrix and environment report. Run the same small synthetic evidence task through Terra/medium and Luna/medium: tool use, source attribution, persisted artifact, and completion. For every comparator, test its actual provider adapter, effective configuration, and fallback behavior. Run a managed Codex/Qwen xhigh bootstrap/tool-use/completion probe with the 15-minute deadline. Verify OpenDeepWiki container-to-LM-Studio catalog/content requests. Check embeddings only for applications that use them.

Earlier protocol checks are background evidence, not application certification. Surface unsupported reasoning, missing telemetry, timeouts, and provider failures. Record all attempts, timings, commands, and responses without secrets. Full generation requires verified requested configurations; unsupported required baselines remain visible blockers.

**Acceptance:**

- 1.3.1 - Every application has a compatibility disposition supported by commands, responses, effective configuration, and per-case measurements, including calibration and the managed Qwen test. file: `docs/evidence/wiki-bakeoff-code-2026-09/compatibility.md`.
- 1.3.2 - Full generation proceeds only on verified configurations; blocked required baselines are recorded without silent provider/model substitutions. file: `docs/evidence/wiki-bakeoff-code-2026-09/compatibility.md`.

## P2: Capture Graphify and gcode evidence
`kind: framing`

### 2.1 Execute the evidence-index comparison [category: test] (depends: 1.3)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`

Read matrix, environment, and compatibility reports. Run both indexes against the same corpus in separate copies. Capture Graphify deterministic code separately from semantic/document analysis, including native graph, report, viewer, provenance, and queries. Capture corresponding gcode structure, graph, content, and retrieval evidence. Measure indexing separately from query cost; distinguish deterministic, embedding, and generation work.

Execute cold, unchanged, committed-change, rationale/comment, rename, removal, and supported recovery/retrieval/export cases from the index casebook. Preserve before/after artifacts and exact queries. Report observable differences and candidate missing capabilities only; prioritization, adoption, and implementation belong to later analysis/gcode epics.

**Acceptance:**

- 2.1.1 - Every index case has results from both tools or a demonstrated unsupported disposition. file: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`.
- 2.1.2 - Every difference includes source evidence and reproduction, separating extraction/index gaps, retrieval failures, and interpretation differences. file: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`.
- 2.1.3 - Complete native artifacts and per-test measurements are preserved with inventories and hashes. file: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`.

## P3: Capture native wiki outputs
`kind: framing`

Leaves may run concurrently after P2 within two-hosted/one-local generator limits. All required baselines must complete; a failure report cannot substitute for full native output.

### 3.1 Capture CodeWiki output [category: test] (depends: 2.1)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/codewiki.md`

Read frozen matrix, environment, compatibility, and index-comparison reports. Use pinned CodeWiki, its own corpus copy, and verified Codex/Terra medium subscription adapter. Run cold generation, unchanged rerun, and native incremental comparison of frozen commits with separate output directories. Preserve every native file: overview, modules, hierarchy, metadata, and diagrams.

Execute supported recovery, native retrieval/Ask, and export/presentation cases without implementing missing features. Ask the common approval-boundary, weekly-planning/workbook, and Buylist-ownership questions, recording whether answers use wiki retrieval, fresh source, or both. Inventory/hash complete artifacts and record effective settings, prompts, calls, timings, available tokens, retries, counts, citation checks, and change observations per attempt. Demonstrate unsupported capabilities explicitly. Keep this leaf open if its required baseline is incomplete.

**Acceptance:**

- 3.1.1 - Complete native CodeWiki outputs, supported retrieval/export/update observations, citation checks, inventories, hashes, and per-test measurements are accessible; unsupported capabilities are explicit. file: `docs/evidence/wiki-bakeoff-code-2026-09/codewiki.md`.

### 3.2 Capture OpenDeepWiki output [category: test] (depends: 2.1)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/opendeepwiki.md`

Read frozen matrix, environment, compatibility, and index-comparison reports. Import its own frozen local corpus into isolated OpenDeepWiki state. Use verified Qwen catalog/content adapters, English-only output, and one local generation slot. Preserve catalog, articles, references, diagrams, and native records. Disable translation, public publishing, and optional duplicate Graphify generation.

Run cold generation, unchanged rerun, supported incremental updates, and native recovery/retrieval/MCP/export cases. Ask common approval-boundary, weekly-planning/workbook, and Buylist-ownership questions, identifying wiki retrieval versus fresh source versus both. Preserve all native files, inventories, hashes, and per-attempt measurements. Record unsupported capabilities without implementing substitutes. Keep this leaf open if its required baseline is incomplete.

**Acceptance:**

- 3.2.1 - Complete native OpenDeepWiki outputs are accessible with inventories, hashes, and measured generation/retrieval/export/update observations; effective adapters and unsupported capabilities are explicit. file: `docs/evidence/wiki-bakeoff-code-2026-09/opendeepwiki.md`.

### 3.3 Capture Grok Wiki output [category: test] (depends: 2.1)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/grok-wiki.md`

Read frozen matrix, environment, compatibility, and index-comparison reports. Run the pinned Grok Wiki release against its own corpus copy and documented isolated storage root using Codex/Terra medium, English, and concurrency one. Use native first-30 style, automatic page selection, and ceiling 12. Preserve saved records, pages, sources, and native Markdown/Obsidian exports.

Run cold generation, unchanged rerun, and supported incremental/recovery cases. Verify effective model and detect native fallback. Exercise native Ask with common approval-boundary, weekly-planning/workbook, and Buylist-ownership questions; record wiki retrieval versus fresh source versus both. Record whether incremental support exists. Preserve all artifacts, inventories, hashes, and per-test measurements; no substitutes for missing capabilities. Keep this leaf open if its required baseline is incomplete.

**Acceptance:**

- 3.3.1 - Complete native Grok Wiki outputs/exports are accessible, model identity is verified, and every case has measurements or demonstrated unsupported evidence. file: `docs/evidence/wiki-bakeoff-code-2026-09/grok-wiki.md`.

### 3.4 Capture Understand Anything output [category: test] (depends: 2.1)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/understand-anything.md`

Read frozen matrix, environment, compatibility, and index-comparison reports. Use pinned project-local Codex plugin, its own corpus copy, Terra medium, and English output. Run repository analysis, domain analysis, and onboarding/tours. Preserve the full native data directory, graph, summaries, domain flows, tours, configuration, and dashboard evidence.

Run cold generation, unchanged rerun, native incremental/diff, supported recovery, retrieval/chat, and presentation cases. Disable automatic commit hooks. Ask common approval-boundary, weekly-planning/workbook, and Buylist-ownership questions, distinguishing wiki retrieval from fresh source. Preserve every artifact and per-test measurement with hashes/inventories. Record unsupported capabilities without substitutes. Keep this leaf open if its required baseline is incomplete.

**Acceptance:**

- 3.4.1 - Complete native structure, domain, tour, and change outputs are accessible with inventories, hashes, and per-test measurements; retrieval/presentation and unsupported capabilities are explicit. file: `docs/evidence/wiki-bakeoff-code-2026-09/understand-anything.md`.

### 3.5 Capture Archify output [category: test] (depends: 2.1)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/archify.md`

Read frozen matrix, environment, compatibility, and index-comparison reports. Use pinned project-local Archify skill, its own corpus copy, and Codex/Terra medium. Generate five source-grounded diagrams: repository architecture/application boundaries; weekly-planning approval workflow; synchronization sequence; report/forecast data flow; operational-run lifecycle/recovery.

Preserve native JSON, HTML/SVG, validation/delivery receipts, and screenshots. Run unchanged and committed-change comparisons plus invalid-candidate/last-good behavior through the native bounded correction workflow. Execute supported retrieval/export cases, including common approval-boundary, weekly-planning/workbook, and Buylist-ownership questions where native support exists. Distinguish diagram/wiki retrieval from fresh source. Record unsupported capabilities without substitutes. Inventory/hash all files and record per-attempt measurements. Keep this leaf open if its required baseline is incomplete; never publish private source to satisfy a reference checker.

**Acceptance:**

- 3.5.1 - All five diagram types have accessible native/rendered output, source grounding, validation receipts, inventories, hashes, and per-test measurements. file: `docs/evidence/wiki-bakeoff-code-2026-09/archify.md`.
- 3.5.2 - Committed-change and invalid-candidate/last-good behavior have preserved before/after evidence and measurements. file: `docs/evidence/wiki-bakeoff-code-2026-09/archify.md`.

## P4: Deliver the evidence package
`kind: framing`

### 4.1 Assemble the analysis handoff [category: docs] (depends: P3)
`kind: deliverable`

Target: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`

Read every preceding evidence report. Build a complete index without rewriting native outputs. Include frozen identities, case dispositions, artifact locations/hashes, aggregate measurements, reproducible commands, limitations, and execution blockers. Verify readers can browse every output; optional viewers bind only to loopback. Present native Markdown exports in a disposable Obsidian vault without repairing/normalizing them into a Gobby format.

Prepare inputs for the separate analysis epic's file-by-file review, capability selection, canonical specifications/examples, and gcode gap assessment. Do not make adoption/design decisions here. Reconcile measurements across successful/failed attempts, verify hashes/links, and preserve full reproducible comparison evidence.

**Acceptance:**

- 4.1.1 - Every required case and comparator is accessible through the index with no hidden omissions. file: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`.
- 4.1.2 - Artifact hashes verify, metrics reconcile across attempts, and reproduction specifies exact versions and effective settings. file: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`.
- 4.1.3 - Required baselines are complete; handoff separates observations, unsupported capabilities, and unresolved failures. file: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`.

## V2 Completion checks
`kind: verification`

Verify source/live-state isolation, complete native inventories and valid links, effective models and accounting for every attempt, unsupported capabilities without synthetic substitutes, no engine implementation/canonical output specification, and preservation of analysis inputs. Follow normal Gobby scoped-commit, validation, bounded review, and task-close gates. Do not close with incomplete required native baselines.

Subsequent separately approved epics: analysis, gcode planning, justified gcode implementation, gwiki planning, gwiki implementation, UI planning. General-knowledge work follows the code-wiki work and is not activated by this plan.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Create the matrix and Graphify/gcode casebook
  category: docs
  task_type: task
  depends_on: []
  validation_criteria: '1.1.1: Every case specifies inputs, expected observations,
    metrics, and pass/fail or unsupported criteria. file: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`.

    1.1.2: Ground truth covers the shared platform, Restocks, and Buylist, distinguishing
    direct support, inference, and ambiguity with commit-pinned evidence. file: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`.

    1.1.3: The index casebook credits existing gcode capabilities and requires reproducible
    evidence for every difference. file: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`.'
  labels:
  - covers:wiki-code-bakeoff:1.1:1.1.1
  - covers:wiki-code-bakeoff:1.1:1.1.2
  - covers:wiki-code-bakeoff:1.1:1.1.3
  tdd: false
  source_section: '1.1'
  assigned_agent: tech-writer
- title: Provision the isolated environment
  category: test
  task_type: task
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Configuration and before/after observations prove the
    source repository, live stores, global configuration, and existing application
    state are outside the write scope. file: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`.

    1.2.2: Every comparator has a verified pinned installation or explicit reproducible
    installation blocker. file: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`.

    1.2.3: gcode uses the isolated daemon, signed grants, PostgreSQL, Qdrant, and
    FalkorDB; service identity and containment checks pass. file: `docs/evidence/wiki-bakeoff-code-2026-09/environment.md`.'
  labels:
  - covers:wiki-code-bakeoff:1.2:1.2.1
  - covers:wiki-code-bakeoff:1.2:1.2.2
  - covers:wiki-code-bakeoff:1.2:1.2.3
  tdd: false
  source_section: '1.2'
  assigned_agent: backend-developer
- title: Capture calibration and provider compatibility
  category: test
  task_type: task
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Every application has a compatibility disposition supported
    by commands, responses, effective configuration, and per-case measurements, including
    calibration and the managed Qwen test. file: `docs/evidence/wiki-bakeoff-code-2026-09/compatibility.md`.

    1.3.2: Full generation proceeds only on verified configurations; blocked required
    baselines are recorded without silent provider/model substitutions. file: `docs/evidence/wiki-bakeoff-code-2026-09/compatibility.md`.'
  labels:
  - covers:wiki-code-bakeoff:1.3:1.3.1
  - covers:wiki-code-bakeoff:1.3:1.3.2
  tdd: false
  source_section: '1.3'
  assigned_agent: backend-developer
- title: Execute the evidence-index comparison
  category: test
  task_type: task
  depends_on:
  - '1.3'
  validation_criteria: '2.1.1: Every index case has results from both tools or a demonstrated
    unsupported disposition. file: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`.

    2.1.2: Every difference includes source evidence and reproduction, separating
    extraction/index gaps, retrieval failures, and interpretation differences. file:
    `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`.

    2.1.3: Complete native artifacts and per-test measurements are preserved with
    inventories and hashes. file: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`.'
  labels:
  - covers:wiki-code-bakeoff:2.1:2.1.1
  - covers:wiki-code-bakeoff:2.1:2.1.2
  - covers:wiki-code-bakeoff:2.1:2.1.3
  tdd: false
  source_section: '2.1'
  assigned_agent: backend-developer
- title: Capture CodeWiki output
  category: test
  task_type: task
  depends_on:
  - '2.1'
  validation_criteria: '3.1.1: Complete native CodeWiki outputs, supported retrieval/export/update
    observations, citation checks, inventories, hashes, and per-test measurements
    are accessible; unsupported capabilities are explicit. file: `docs/evidence/wiki-bakeoff-code-2026-09/codewiki.md`.'
  labels:
  - covers:wiki-code-bakeoff:3.1:3.1.1
  tdd: false
  source_section: '3.1'
  assigned_agent: backend-developer
- title: Capture OpenDeepWiki output
  category: test
  task_type: task
  depends_on:
  - '2.1'
  validation_criteria: '3.2.1: Complete native OpenDeepWiki outputs are accessible
    with inventories, hashes, and measured generation/retrieval/export/update observations;
    effective adapters and unsupported capabilities are explicit. file: `docs/evidence/wiki-bakeoff-code-2026-09/opendeepwiki.md`.'
  labels:
  - covers:wiki-code-bakeoff:3.2:3.2.1
  tdd: false
  source_section: '3.2'
  assigned_agent: backend-developer
- title: Capture Grok Wiki output
  category: test
  task_type: task
  depends_on:
  - '2.1'
  validation_criteria: '3.3.1: Complete native Grok Wiki outputs/exports are accessible,
    model identity is verified, and every case has measurements or demonstrated unsupported
    evidence. file: `docs/evidence/wiki-bakeoff-code-2026-09/grok-wiki.md`.'
  labels:
  - covers:wiki-code-bakeoff:3.3:3.3.1
  tdd: false
  source_section: '3.3'
  assigned_agent: backend-developer
- title: Capture Understand Anything output
  category: test
  task_type: task
  depends_on:
  - '2.1'
  validation_criteria: '3.4.1: Complete native structure, domain, tour, and change
    outputs are accessible with inventories, hashes, and per-test measurements; retrieval/presentation
    and unsupported capabilities are explicit. file: `docs/evidence/wiki-bakeoff-code-2026-09/understand-anything.md`.'
  labels:
  - covers:wiki-code-bakeoff:3.4:3.4.1
  tdd: false
  source_section: '3.4'
  assigned_agent: backend-developer
- title: Capture Archify output
  category: test
  task_type: task
  depends_on:
  - '2.1'
  validation_criteria: '3.5.1: All five diagram types have accessible native/rendered
    output, source grounding, validation receipts, inventories, hashes, and per-test
    measurements. file: `docs/evidence/wiki-bakeoff-code-2026-09/archify.md`.

    3.5.2: Committed-change and invalid-candidate/last-good behavior have preserved
    before/after evidence and measurements. file: `docs/evidence/wiki-bakeoff-code-2026-09/archify.md`.'
  labels:
  - covers:wiki-code-bakeoff:3.5:3.5.1
  - covers:wiki-code-bakeoff:3.5:3.5.2
  tdd: false
  source_section: '3.5'
  assigned_agent: backend-developer
- title: Assemble the analysis handoff
  category: docs
  task_type: task
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  validation_criteria: '4.1.1: Every required case and comparator is accessible through
    the index with no hidden omissions. file: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`.

    4.1.2: Artifact hashes verify, metrics reconcile across attempts, and reproduction
    specifies exact versions and effective settings. file: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`.

    4.1.3: Required baselines are complete; handoff separates observations, unsupported
    capabilities, and unresolved failures. file: `docs/evidence/wiki-bakeoff-code-2026-09/README.md`.'
  labels:
  - covers:wiki-code-bakeoff:4.1:4.1.1
  - covers:wiki-code-bakeoff:4.1:4.1.2
  - covers:wiki-code-bakeoff:4.1:4.1.3
  tdd: false
  source_section: '4.1'
  assigned_agent: tech-writer
```
