# Ask Contract

Ask is a durable, source-bound question-answering pipeline. Public CLI, MCP, and
HTTP entry points are adapters around one daemon-owned `AskService`; they do not
run a model directly or implement a second workflow engine.

## Run Request And Immutable Binding

A start request contains:

- `question` and `project_id`
- optional `project_path`, selecting the registered caller checkout or worktree
- `timeout_seconds`, default `600`
- `retrieval_mode`, either `deterministic` or user-facing `hybrid`
- the fixed profile identifiers `ask-investigator` and `ask-reviewer`
- an optional `idempotency_key`

The service resolves the request once and records an immutable binding. The
binding contains the authorized project, canonical repository root, HEAD commit and
tree OIDs recorded at admission, absolute `deadline_at`, and canonical retrieval
mode. Commit and tree OIDs are provenance; they do not create a checkout or index.
Ask uses the caller’s existing live index, including ordinary worktree overlay
visibility. User-facing `hybrid` maps to `audited_hybrid`. Resume, retries, review,
and repair retain the root, recorded provenance, retrieval identity, and deadline.
Recovery advances a generation with compare-and-swap fencing and rotates managed
credentials; stale generations cannot admit evidence. Binding probes are bounded
to five seconds for configuration and ten seconds for search. Ask never invokes
the indexer during binding.

Each profile is snapshotted at admission. A profile identity records its
registry identifier, definition ID and update time, effective merged content,
and content hash. These snapshots, not mutable registry rows, govern the run.
The live definition still comes from the database registry: bundled YAML under
`src/gobby/install/shared/` is installation input, not proof of what the daemon
is serving.

## Durable Run Result

All public surfaces return the same canonical run record. It exposes, directly
or in nested canonical objects:

- `run_id`, pipeline `status`, and `current_stage`
- `answer_outcome`: `complete`, `partial`, `unknown`, or null before an answer
- `typed_error`, null when no typed failure is present
- the original `deadline_at`
- source `binding`, profile identities, tool identities, attempt count, and
  repair count
- evidence and result artifact references, usage, and the immutable publication
  manifest reference when published

Queued and running records are nonterminal. Complete, partial, and unknown are
distinct successful answer outcomes. Failed and cancelled are durable terminal
pipeline statuses, not transport errors. Adapters preserve these distinctions
and do not infer an outcome from prose.

## Evidence And Provenance

An `EvidenceManifest` is versioned and bound to one run and repository binding.
It records the authorized project separately from the caller’s index identity
(which may be an overlay), and every admitted evidence request and response,
including invocation, request, response, and record hashes. Pagination uses an
opaque continuation bound to the canonical request. There is no hidden query,
turn, or item-count cap; byte and traversal bounds are explicit in evidence
responses.

Source evidence citations contain the run and evidence IDs, safe repository
path, exact content/excerpt hashes, and line and byte bounds. They may also
carry a qualified symbol name. Graph citations additionally record the source
evidence, relation, direction, endpoints, owner path and content hash, and
extraction provenance. Git metadata citations record the commit, parents,
comparison, changed-path digest/count, optional changed path, and record hash;
they deliberately do not pretend to have source line bounds.

Indexed facts locate evidence but are not publication authority. Source bytes
are read from the caller’s working tree and checked against the indexed content
hash before admission. Ordinary freshness checks govern the index; changes after
indexing produce a stale-range error instead of a mismatched citation. Indexed
dirty and untracked nonignored files are citable. Git metadata queries use the
recorded commit for provenance.
Deterministic retrieval does not use embeddings. Audited hybrid retrieval is an
explicit opt-in and records the verified semantic model, dimension, endpoint,
and vector-index identity; failure never falls back silently to lexical output.

## Claims, Answers, And Review

Each answer claim is classified as `direct`, `inferred`, or `unknown`:

- A direct claim has one or more citations that support the statement.
- An inferred claim has citations, explicit premises, and a rationale connecting
  those premises to the statement.
- An unknown claim has no citations or premises and states the evidence limit
  that prevented an answer.

Assertions are also typed. A positive assertion cites its support. Negative or
exhaustive assertions include an `EvidenceScope` describing the search and its
admitted evidence and invocation IDs; an empty search result alone is never
repository-wide proof.

An `AnswerDraft` is versioned and records the run, investigator, original
question, question parts, claims, and answer sections. Claim IDs are unique and
each claim appears in exactly one section. A `ReviewerResult` records the run,
reviewer, exact draft and evidence-manifest hashes, per-claim verdicts, missing
question parts, and rationale. Submissions include their declared hashes, and
the service reserializes and verifies them at the durable attempt boundary.

The publication bundle contains the validated answer JSON, rendered Markdown,
evidence manifest, and a content-addressed manifest. Replay recomputes the
answer, Markdown, and manifest hashes; mismatches make the publication invalid.

## Native-Agent Ownership

The owning pipeline moves through prepare, evidence seed, investigator spawn and
validation, reviewer spawn and validation, optional repair admission, and
publication. Workflow rules are authored against semantic events such as
`turn_start` and `turn_end`; provider/runtime events such as `before_agent`,
`after_agent`, and `stop` are implementation details rather than the authoring
API.

At most one native child principal is active for a stage attempt. Internal stage
operations require the owning pipeline's project, run, stage, and attempt.
Agent-facing operations require that active child principal. The canonical
agent operations are `query_evidence`, `read_evidence`, `submit_answer`, and
`submit_review`; there are no direct-model, `search_evidence`, or
`submit_draft` fallbacks. Completing or rejecting a submission does not end the
agent process by implication: the child must separately call `end_agent_run`.

## Deadline, Review, And Repair

`deadline_at` is absolute and immutable. The pipeline reserves enough of the
original budget for validation and review instead of allowing investigation to
consume the entire deadline. Wait timeouts only bound the caller's wait; they do
not replace or extend the run deadline.

A rejected investigator answer may enter one explicitly admitted repair
attempt. Repair is bounded to that one attempt, uses the same source, profiles,
evidence manifest lineage, and absolute deadline, and is followed by validation
and review. No unbounded agent retry loop is part of the contract.

## Wait, Resume, Cancel, And Recovery

Wait subscribes to the daemon's completion event before re-reading durable
state, closing the lost-wakeup window. It returns the canonical durable record.
A client timeout or disconnect does not cancel or delete the run. The CLI emits
the durable `run_id` and `gcode ask --resume <RUN_ID>` recovery command when its
wait connection fails.

Resume operates on the existing record and original deadline. It may continue
an interrupted eligible stage; it never creates a replacement identity. Cancel
marks the run cancelled and terminates its active native child while retaining
the binding, evidence, submissions, and other durable artifacts for audit.

## Public Adapters

### CLI

```text
gcode ask "<QUESTION>" [--timeout-seconds N]
  [--retrieval deterministic|hybrid] [--background]
gcode ask --status RUN_ID
gcode ask --resume RUN_ID
gcode ask --cancel RUN_ID
gcode ask --export RUN_ID --output DIR
```

Global `--project` selects a registered project by root or name. Start defaults
to foreground event-driven waiting. Explicit `--background` returns after the
durable run is created. Text is the default; `--format json` returns the stable
run object. Only explicit export creates a local file.

### MCP

The `gobby-ask` registry exposes public tools:

- `start_ask_run`, `get_ask_run`, `wait_for_ask_run`
- `resume_ask_run`, `cancel_ask_run`, `export_ask_run`

Public MCP tools bind `project_id` from the active registry and caller identity
from the verified session context. Callers cannot supply a foreign project to
these tools. Export verifies the publication by replay and returns the run,
manifest SHA-256, and authenticated HTTP download URL.

Pipeline-only tools are `prepare`, `seed`, `spawn`, `validate`, `admit_repair`,
and `publish`. Agent-only tools are `query_evidence`, `read_evidence`,
`submit_answer`, and `submit_review`. These names are exact and discoverable;
authorization remains service-owned even when an adapter schema includes an
explicit project, stage, or attempt argument.

### HTTP

Authenticated local-daemon routes are:

- `POST /api/ask/runs` — start, returning HTTP 202
- `GET /api/ask/runs/{run_id}?project_id=...` — durable status
- `GET /api/ask/runs/{run_id}/wait?project_id=...&timeout_seconds=...` — wait
- `POST /api/ask/runs/{run_id}/resume?project_id=...`
- `POST /api/ask/runs/{run_id}/cancel?project_id=...`
- `GET /api/ask/runs/{run_id}/export?project_id=...`

Start accepts the public request fields and resolves the registered checkout
for its project. An explicit `project_path` must be that primary checkout or a
registered worktree/clone owned by the same project and machine. Mutating calls pass the verified caller session to the service.
Export streams `application/x-tar` from the service-verified immutable
publication root and does not create a server-local export copy.

## Authorization And Security

Ask is available only through the authenticated local daemon. Every operation
is authorized against the requested run and registered project. Stage and agent
operations additionally validate the owning pipeline, active stage and attempt,
and active child principal. Adapters delegate these guards to the shared service
and never trust a caller-supplied project identity over ambient authorization.

Source paths are repository-relative and safe; source and evidence hashes,
profile/tool identities, submissions, and publication artifacts are verified at
their boundaries. The ordinary index’s ignore policy controls which files are
searchable. Indexed excerpts remain exact, including documentation and credential
fixtures; runtime credentials are rejected or redacted. The investigator cannot
read or write the repository directly: its sandbox denies the source root and
uses disjoint runtime scratch, with evidence as the only source access. Managed
grants bind to the real repository root and subprocesses use an isolated
`GOBBY_HOME`, per-run identity, and a scrubbed environment. There is no direct
model fallback or adapter path that mutates the source checkout.

End-to-end acceptance requires the shared runtime service, installed native
binary, and installed database-backed workflow/profile definitions. Unit tests
with adapter fakes establish surface fidelity only; they do not establish those
runtime, authorization, installation, or security prerequisites.
