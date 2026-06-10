# Review: plans + sync

- **Scope:** `src/gobby/plans/` (parser, coverage, evidence, manifest emitter/parser, coverage manifest, consumer sweep, semantic lint, bootstrap ledger, deferral, artifact refs) and `src/gobby/sync/` (tasks, memories, integrity, linear, github, export_context) — ~7,800 lines. Adjacent contract surfaces consulted: `docs/contracts/plan-coverage.md`, `src/gobby/cli/plan.py`, `src/gobby/cli/installers/git_hooks.py`, `src/gobby/integrations/linear_graphql.py`, `src/gobby/mcp_proxy/client_manager/invocation.py`.
- **Reviewer:** Claude Fable 5 (6 parallel deep-review agents; every Blocker independently re-verified against source by the synthesizer, several by live repro)
- **Commit / branch:** d690b / 0.5.0
- **Summary:** 15 Blocker · 45 Important · 20 Nit — the plan-coverage gate is green-biased exactly where the contract is strictest (deferrals are dead code, dangling covers labels vanish, matrix-file mode is trust-me, the consumer sweep can't resolve the documented symbol format), and the sync layer does not meet its "git is source of truth" contract (overwrite-no-merge export, provenance-dropping import, zero atomicity); Linear/GitHub tracker sync works only by accident of its GraphQL fallback.

## Findings — plans/ (coverage contract enforcement)

### [BLOCKER] `_fence_info` crashes with IndexError on any unlabeled fence inside a deferred or manifest section
- **Where:** `src/gobby/plans/parser.py:624-628`, reached via `_find_yaml_fence` at `parser.py:615` and `manifest_parser.py:79`
- **Failure mode:** `match.group("info").strip().split(maxsplit=1)[0]` — empty info string splits to `[]`, `[0]` raises `IndexError`. Trigger: a `kind: deferred` or `kind: manifest` section containing any bare ``` ``` ``` fence (no info string) — e.g. an author writing the deferral YAML in a plain fence instead of ```` ```yaml ````. Reproduced live: `parse_plan` raises raw `IndexError: list index out of range`, not `PlanParseError`.
- **Why it matters:** every caller (`validate_plan_file` planner gate, adversary self-check, `gobby expand`, coverage CLI) expects `PlanParseError`; this is an unhandled crash on a mainline authoring mistake. Fence tests (`tests/plans/test_parser.py:432-611`) never exercise an unlabeled fence inside these sections.
- **Minimal fix:** `info = match.group("info").strip(); return info.split(maxsplit=1)[0].lower() if info else ""`.
- **Confidence:** high — reproduced (`_fence_info('```')` → IndexError).

### [BLOCKER] Deferral gate is dead code — deferred sections emit zero coverage rows; closed/missing deferral tasks pass silently
- **Where:** `src/gobby/plans/parser.py:219-235` + `src/gobby/plans/coverage.py:321-334` (row loop) and `coverage.py:395-409` (unreachable deferral branch); `src/gobby/plans/deferral.py:41` (`validate_deferral`, never invoked in production)
- **Failure mode:** the parser populates `acceptance_items` only for `Kind.deliverable` and `deferral` only for `Kind.deferred` — mutually exclusive (`if`/`elif`, parser.py:219-228). `_evaluate_records` builds rows by iterating `section.acceptance_items`, which is `()` for deferred sections, so they produce no rows and the `section.deferral is not None and _deferral_covers_item(...)` branch inside `_evaluate_item` is unreachable on any parser-produced document. Independently confirmed by two review agents; one ran a live repro: a parsed plan whose deferred section's `task_ref: "#999"` points at a nonexistent task evaluates to `is_complete: True`. Every contract check in `validate_deferral` (task open, `deferred-from:<plan-id>:<section-id>` provenance, criteria duplication, recovery-epic dependency) never executes for real plans.
- **Why it matters:** the contract's "A closed task fails the gate" is bypassed wholesale — anyone can defer a deliverable, close the deferral task, and the gate stays green. `tests/plans/test_coverage.py:43-52` passes only because it hand-fabricates a deferred `PlanSection` with `acceptance_items` populated — a shape `parse_plan` cannot produce.
- **Minimal fix:** in `_evaluate_records` (and `_missing_rows`), iterate `section.deferral.original_acceptance_items` for deferred sections and emit a row per item with status from `_validate_deferral_status`. Add a parse→evaluate integration test using real markdown.
- **Confidence:** high — verified in source by synthesizer; runtime-proven by reviewer.

### [BLOCKER] Dangling `covers:` labels (nonexistent section/item) are silently ignored — gate passes
- **Where:** `src/gobby/plans/coverage.py:504-522` (`_matching_cover_records`), `coverage.py:306-348`
- **Failure mode:** evaluation is keyed off plan items: labels are only consulted when they exactly match an existing (section, item) pair. A label like `covers:plan:A9:A9.1` where A9 doesn't exist matches nothing and vanishes without trace; malformed `covers:` labels are skipped via `except InvalidCoversLabelError: continue` (coverage.py:512-514). `validate_covers`' `missing_section`/`missing_item` statuses (coverage.py:183, 192) are unreachable from the pipeline — its only caller pre-matched the record to an existing item.
- **Why it matters:** the contract requires the gate to FAIL on a covers label referencing a nonexistent section/item — the canonical plan-renumber/typo drift signal. If all real items are otherwise covered, the report is fully `covered` → exit 0.
- **Minimal fix:** after building rows, sweep all scoped task labels: parse each `covers:` label for this plan_id; any record not resolving in `plan_doc` adds an `invalid` row; surface grammar failures on `covers:`-prefixed labels instead of `continue`.
- **Confidence:** high — verified in source.

### [BLOCKER] Matrix-file mode trusts the matrix wholesale — hash check skippable, no completeness or row-validity cross-check
- **Where:** `src/gobby/plans/coverage.py:437-438` (hash), `:433`/`:454` (rows taken verbatim; `_missing_rows` only when rows empty), `:458-471` (`_row_from_manifest` never validates section/item existence or `plan_node_hash`)
- **Failure mode:** (1) `existing_hash = str(header_data.get("plan_hash", ""))` then `if existing_hash and ...` — a matrix omitting `plan_hash` skips the staleness check entirely (verified in source). (2) A matrix containing a strict covered subset of the plan's items yields exit 0; deleting a `missing` row flips the gate green. (3) Fabricated covered rows for nonexistent items pass; row `plan_node_hash` is parsed but never compared.
- **Why it matters:** matrix-file is a first-class `--task-tree` mode of the documented gate CLI; the gate is bypassable with a trimmed or hand-crafted YAML. `tests/plans/test_coverage_cli.py:30-46` only exercises a single-row matrix exactly mirroring a single-item plan.
- **Minimal fix:** require `header.plan_hash` in matrix mode; reconcile matrix rows against `plan_doc` items (emit `missing` for absent items, `invalid` for unresolvable rows or `plan_node_hash` mismatches).
- **Confidence:** high.

### [BLOCKER] `_emit_fresh` leaves a failed synthesized manifest in the plan file; re-run reports success on the corrupted file
- **Where:** `src/gobby/plans/manifest_emitter.py:235` (write), `:244-250` (failure path)
- **Failure mode:** `_emit_fresh` writes the synthesized manifest, and if post-write `parse_plan(parse_mode="expansion")` re-validation fails it appends a Yolo Fallbacks section and returns — it never restores `body` (verified in source; contrast `_replace_existing_manifest`, which restores `raw` at `:185`). Reviewer repro with a plan ending in an unclosed fence: emit #1 writes the manifest *inside* the open fence, fails validation, leaves the dead manifest + Yolo section; emit #2 appends a SECOND `## M1 Task Manifest` and returns `"fresh"`. Each failing retry appends another Yolo section.
- **Why it matters:** corrupts user-authored contract artifacts on a mainline emitter path, then declares success. No test covers post-write validation failure with file-state assertions.
- **Minimal fix:** in the `except PlanParseError` branch, `path.write_text(body, ...)` before `_append_yolo_fallback`, mirroring `_replace_existing_manifest`.
- **Confidence:** high — verified in source; reviewer reproduced.

### [BLOCKER] Consumer sweep symbol resolution silently no-ops for the documented module-qualified ref format — `valid=True` with zero checks
- **Where:** `src/gobby/plans/consumer_sweep.py:258-272` (`_resolve_symbols`); `src/gobby/code_index/storage.py:123-154` (`search_symbols_by_name`)
- **Failure mode:** the contract's canonical form is `symbol: gobby.module.Symbol` (`docs/contracts/plan-coverage.md:69`). The live index stores leaf-only `qualified_name` (verified at this commit: `validate_deferral`'s `qualified_name == "validate_deferral"`), so the LIKE search on the full dotted ref returns nothing — and the leaf fallback at `:268-271` filters the *same empty tuple* instead of re-querying by leaf name, so it can never recover. `_sweep_section` then hits `if not symbols: continue` and the sweep returns `valid=True, skipped=False`.
- **Why it matters:** the sweep's core promise — blocking planner/adversary spawns and `gobby plans validate` when direct consumers of changed symbols are missing from Targets — never fires for the documented ref format. Tests pass only because the fake storage keys symbols by fully-dotted `qualified_name` (`tests/plans/test_consumer_sweep.py:29-38`).
- **Minimal fix:** when the full-ref query is empty, re-query `search(leaf, ...)` with `leaf = symbol_ref.rsplit(".", 1)[-1]`, keeping the existing unique-exact-name + defined-in-targets disambiguation; add a test whose fake mirrors the real index's leaf-only shape.
- **Confidence:** high — verified against live index and source.

## Findings — sync/ (JSONL, Linear, GitHub)

### [BLOCKER] Both tracker-sync services consume `call_tool()` as if it returned dicts — it returns `mcp.types.CallToolResult` — so the entire MCP delegation path is non-functional and corrupts remote state in four places
- **Where (contract):** `src/gobby/mcp_proxy/manager.py:191-207` → `src/gobby/mcp_proxy/client_manager/invocation.py:44-58` returns `session.call_tool(...)` raw (verified in source). Sibling code proves the correct contract: `github_triage/service.py:697-705` parses `result.content[0].text`.
- **Consumers, each verified:** `sync/linear.py:334`/`:355` — `_extract_records` (`linear.py:143-152`) returns `[]` for non-list/non-dict, so `list_teams`/`list_projects` silently return empty on MCP *success* and the GraphQL fallback (exception-gated) never runs; `linear.py:371-390` `ensure_linear_project` — name match never hits, remote `create_project` succeeds, `_extract_record` returns `{}`, line 388 raises → binding never persisted → every setup retry creates another duplicate Linear project; `linear.py:660-668` — `.get("id")` AttributeError *after* remote issue creation → `linear_issue_id` never written → duplicate Linear issue per task per run; `linear.py:595-599` — `isinstance(result, dict)` always False → raises after the remote update succeeded → `sync_all` never advances the cursor → every 5-minute cron re-pushes every dirty task forever; `sync/github.py:150` — `result.get("issues", [])` unguarded AttributeError, GitHub import dead on its mainline path (no fallback); `github.py:255-259` — raises after remote update; `github.py:315-316` — `.get("number")` AttributeError after PR creation → retry creates a second PR.
- **Why it matters:** module docstrings promise "delegates all operations to the official MCP server"; that path silently returns empty, duplicates remote projects/issues/PRs, and reports failure after remote success. Every unit test stubs `call_tool` returning dicts (`tests/sync/test_linear_sync.py:39,163,176,317`; same in `test_github_sync.py`) and even the E2E stub returns dicts — the whole pyramid certifies the broken contract (same pattern as `docs/reviews/comms-integrations.md` finding #4).
- **Minimal fix:** one shared `parse_mcp_result(result)` helper (raise on `isError`, JSON-parse `content[0].text` / prefer `structuredContent`) applied at every `call_tool` site in both files; pin one test to a real `CallToolResult`. Also closes #15787's isError-as-success hole for these callers.
- **Confidence:** high.

### [BLOCKER] Linear pull clobbers newer local edits — Linear wins every conflict regardless of local `updated_at`
- **Where:** `src/gobby/sync/linear.py:807-822`
- **Failure mode:** the only guard is `linear_updated <= synced_at` against the project *cursor*; the local task's own `updated_at` is never consulted before `reconcile_task_state(title, description, priority)` overwrites it (verified in source). Cron `sync_all` runs every 300s per bound project; any Linear-side touch (comments and label changes bump `updatedAt`) pulls stale title/description/priority over a newer local edit, then `push_dirty_tasks` pushes the loss back to Linear — the local edit is destroyed on both sides, reported as `updated=1` success.
- **Minimal fix:** compare `issue.updatedAt` against the task's `updated_at` and skip/flag conflicts; at minimum reconcile only fields that differ from the last-pushed value.
- **Confidence:** high.

### [BLOCKER] Linear state changes are never pulled, and the echo-push actively reverts them; import resets remote issue states wholesale
- **Where:** `src/gobby/sync/linear.py:815-822` (pull writes only title/description/priority); `map_linear_state_to_gobby` (`linear.py:918`) has zero production callers (verified — only its definition matches in src/); echo chain: `reconcile_task_state` → unconditional `updated_at` bump (`storage/tasks/_updates.py:213-226`) → `push_dirty_tasks` → `sync_task_to_linear` pushes the *local* state
- **Failure mode:** (a) user moves a Linear issue Todo→In Progress; pull reconciles (state ignored), bumps `updated_at`; push writes `stateId` back from unchanged local state — the user's Linear change is reverted within one cron cycle. (b) Closing an issue in Linear never closes the Gobby task. (c) `import_linear_issues` (`linear.py:482-532`) ignores issue state: Done/Canceled issues import as open tasks, and the first-sync push (cursor None → push all) maps them to "Todo" — reopening completed Linear issues en masse.
- **Minimal fix:** apply `map_linear_state_to_gobby` in pull (close/reopen via proper transitions); carry imported state onto created tasks; make reconcile a no-op when values are unchanged so the echo chain dies.
- **Confidence:** high on the code chain (when `_linear_state_id_for_name` can't resolve custom team states, the revert degrades to state-never-synced instead).

### [BLOCKER] 100-issue truncation + cursor advance makes pull misses permanent and lets push overwrite unseen Linear edits
- **Where:** `src/gobby/integrations/linear_graphql.py:211-252` (`first: 100`, no `pageInfo` — inherited from #15787); consumed at `sync/linear.py:474-479` (import) and `:786-790` (pull); `issue_map` misses (`linear.py:796`) count as `skipped`, not errors (`:803-805`), so `sync_all` (`:878-884`) advances `linear_synced_at` past their `updatedAt`
- **Failure mode:** with >100 issues, linked issues beyond the page are skipped, the cursor advances past their timestamps — those updates can never be pulled — while `push_dirty_tasks` (`:830-857`) pushes locally-dirty tasks with no Linear-side recency check, overwriting the edits pull never saw.
- **Minimal fix:** paginate `list_issues` (`pageInfo { hasNextPage endCursor }`); treat "linked task's issue absent from results" as an error that blocks cursor advance.
- **Confidence:** high.

### [BLOCKER] GitHubSyncService is dead in its only production surface via the inherited availability bug
- **Where:** every method gates on `require_available()` (`sync/github.py:135,225,287,376`); `GitHubIntegration._check_availability` (`integrations/github.py:78-95`) returns False with no health entry (inherited from #15787 — Linear has a lazy-connect exception, GitHub doesn't); only production callers are CLI commands (`cli/github.py:186,211,243`) building a fresh lazy-connect `MCPClientManager` whose health map is empty
- **Failure mode:** `gobby github import/sync/pr` raise `RuntimeError("GitHub integration unavailable...")` on every invocation with a correctly configured server. If the gate were passed, the CallToolResult blocker takes over — two independent total-failure modes stacked.
- **Minimal fix:** port the Linear lazy-connect branch into `GitHubIntegration._check_availability` (root cause in integrations/); integration-test sync/github.py through a real manager.
- **Confidence:** high.

### [BLOCKER] Non-atomic in-place JSONL writes; memories merge-export silently destroys other machines' records after truncation
- **Where:** `src/gobby/sync/tasks.py:228` (`open(target_path, "w", ...)`), `src/gobby/sync/memories.py:644` (`file_path.write_text(...)`, preceded at `:640-641` by `except OSError: pass  # File unreadable — overwrite it`)
- **Failure mode:** both exports write the live file directly — no temp+`os.replace`, no fsync, no lock (verified in source). A crash mid-write truncates the JSONL. Tasks: recoverable (re-export), importer fails loudly. Memories: silent permanent loss — `_export_memories_sync` is the only place file-only records from other machines survive; malformed/truncated lines are skipped at debug level (`memories.py:573-574`) and the file rewritten without them. No inter-process locking: CLI backup, pre-push hook, and the daemon digest path can interleave read-merge-write cycles.
- **Why it matters:** this is the exact "bad export silently destroys history across machines" scenario; git only preserves what reaches a commit.
- **Minimal fix:** temp file + fsync + `os.replace`; exclusive flock around read-merge-write; escalate malformed-line skips to warnings with a count; refuse to shrink the record set without `force`.
- **Confidence:** high.

### [BLOCKER] Task export blindly overwrites tasks.jsonl with local DB state — no merge, no import-first — so the git file never converges
- **Where:** `src/gobby/sync/tasks.py:133-237` (full-DB dump, no file merge — verified); pre-push template `src/gobby/cli/installers/git_hooks.py:100-117` (export without prior import — verified)
- **Failure mode:** unlike memories (which merges file records), `export_to_jsonl` replaces the file with the local DB view. There is no auto-import on pull. Machine B pulls A's tasks, never imports, then pushes → A's tasks vanish from the canonical `.gobby/tasks.jsonl`. Each push replaces the file with the pusher's snapshot; a fresh clone gets only the last pusher's tasks.
- **Why it matters:** the stated contract is "git is source of truth for cross-machine sync"; the file is actually "last pusher's DB snapshot" — data loss for any new clone and for history.
- **Minimal fix:** mirror the memories approach (read existing records, LWW-merge by id, write), or make the pre-push hook run `import_from_jsonl` before `export_to_jsonl`.
- **Confidence:** high.

### [BLOCKER] Memory import discards id, project_id, source_session_id, and timestamps — restore rescopes everything to global and resets provenance, while reporting success
- **Where:** `src/gobby/sync/memories.py:395-400` (`create_memory(content=..., memory_type=..., tags=..., source_type=...)` — verified; export writes all dropped fields at `:596-606`); `create_memory` defaults `project_id=None` and stamps now (`storage/memories.py:136-160`)
- **Failure mode:** round trip loses `project_id` (memory becomes global), `source_session_id`, original `created_at`/`updated_at` (reset to import time); id is re-derived from content. Export sanitizes content (home dir → `~`) but the DB holds unsanitized content, so `content_exists(sanitized)` misses → restoring on the *same* machine duplicates every path-containing record as a global memory. Reset `updated_at` also makes the imported copy win later LWW merges.
- **Minimal fix:** pass `project_id`, `source_session_id`, `created_at`, `updated_at` (ideally the id) through on import; store sanitized content so content identity is stable.
- **Confidence:** high (same-machine duplicate path: med-high).

### [BLOCKER] GitHub issue import uses `gh-{number}` as the task primary key — cross-repo collisions silently overwrite other projects' tasks
- **Where:** `src/gobby/sync/tasks.py:617` (`task_id = f"gh-{issue_num}"`), `:632-637` (`SELECT 1 FROM tasks WHERE id = %s` — no project scoping; UPDATE in place) — verified
- **Failure mode:** import issues from repo X (`gh-1`…), then repo Y in another gobby project: the existence check matches repo X's task and UPDATEs its title/description/labels — destroying repo X's task content, reported as "updated existing". Inserts also skip `seq_num`/`path_cache` (NULL) and never populate the `github_issue_number`/`github_repo` columns the JSONL export round-trips.
- **Minimal fix:** key by UUID; dedupe on `(github_repo, github_issue_number)`; set those columns; let normal task creation assign seq_num/path_cache.
- **Confidence:** high.

## Findings — Important (plans/)

### [IMPORTANT] Malformed acceptance-bullet separator silently deletes the item and corrupts the previous item's artifact_ref
- **Where:** `parser.py:33-36` (`_ACCEPTANCE_BULLET_RE` requires ` - `/` — ` separator), `:505-506` (non-matching lines become continuations)
- **Failure mode:** `- 1.2: second item. file: \`src/b.py\`.` (colon separator) is appended to item 1.1's prose; the second item vanishes from coverage and the first's artifact_ref becomes garbage. Reproduced by reviewer. The gate then demands one fewer covers label and reports success — borderline Blocker per the contract-violated-while-reporting-success bar.
- **Minimal fix:** flag continuation lines matching bullet + dotted-ID shape with a wrong separator as errors.
- **Confidence:** high.

### [IMPORTANT] Unclosed fence silently swallows the rest of the document
- **Where:** `parser.py:274-291` (`_compute_fence_mask` leaves `open_fence` dangling at EOF, no diagnostic)
- **Failure mode:** a botched closer masks every subsequent line to EOF; reviewer repro: deliverable section 2 after an unclosed fence parses clean in draft mode with only section 1. The planner gate runs draft mode before every adversary spawn, so the adversary reviews a silently truncated plan.
- **Minimal fix:** return open-at-EOF state from `_compute_fence_mask` and append "unclosed fence opened at line N" to parse errors.
- **Confidence:** high.

### [IMPORTANT] Contract doc and authoring skill document a deferral shape the parser rejects (doc/code drift)
- **Where:** `parser.py:583-591` (top-level fields, no `deferral:` wrapper), `:649-651` (items must be mappings) vs `docs/contracts/plan-coverage.md:85-92` and `src/gobby/install/shared/skills/plan-draft/SKILL.md:104-111` (wrapper key + scalar `- A7.3` items)
- **Failure mode:** the documented YAML fails with "deferred YAML missing fields: task_ref, reason, owner, original_acceptance_items" (reproduced by two reviewers). Tests pin the code's shape, so docs are wrong — but SKILL.md is the authoring surface agents follow, so spec-compliant authors produce unparseable plans; the error gives no hint about the wrapper.
- **Minimal fix:** either fix both docs to the unwrapped/dict-item shape or unwrap a single `deferral:` key in `_parse_deferral` (and accept scalar item IDs). Pin one.
- **Confidence:** high.

### [IMPORTANT] Duplicate acceptance-item IDs accepted without error
- **Where:** `parser.py:461-514` (no per-section item-ID uniqueness check); downstream `coverage.py:559-563` (`_find_acceptance_item` returns first match)
- **Failure mode:** two `1.1` bullets parse into items with identical IDs; coverage accounting keyed by item_id cannot distinguish covered from uncovered work — an unimplemented item can read as covered. Reproduced.
- **Minimal fix:** track seen IDs in `_parse_acceptance_items`; error on duplicates.
- **Confidence:** high.

### [IMPORTANT] Trailing non-list prose is glued into the last acceptance item, defeating the artifact requirement
- **Where:** `parser.py:505-506`
- **Failure mode:** an item naming no artifact, followed by a paragraph after the list mentioning `` `src/c.py` ``, parses successfully with that unrelated prose as its artifact_ref (reproduced). The load-bearing "each item names at least one artifact" invariant is satisfied by accident.
- **Minimal fix:** end item collection at the list boundary (blank line + non-indented non-bullet line ends the list, or require indented continuations).
- **Confidence:** high.

### [IMPORTANT] `_ARTIFACT_RE` backtracks past quoted refs and swallows trailing prose into artifact_ref
- **Where:** `parser.py:37-40` — quoted alternatives are gated on a lookahead; when it fails, the engine backtracks to the `.*?` arm capturing backticks plus everything to EOL
- **Failure mode:** `` file: `src/a.py` and the tests pass `` yields artifact_ref `` `src/a.py` and the tests pass `` (observed in repro output). `_symbol_referenced` then matches garbage (spurious `artifact_not_referenced`), while file matching limps through fuzzy fallbacks in `_artifact_refs.py:38-40`.
- **Minimal fix:** match quoted alternatives unconditionally first; keep the lookahead only for the unquoted arm.
- **Confidence:** high.

### [IMPORTANT] Artifact-reference matching uses bare substring containment — false coverage positives
- **Where:** `src/gobby/plans/_artifact_refs.py:84-90` (`cleaned_ref in text`), `:43-49` (symbol falls back to last dotted segment), `:124-135` (last two path parts as substrings anywhere)
- **Failure mode:** verified by reviewer: symbol `gobby.plans.parser.parse` matches criteria "the parser is great"; file `test.py` matches "see latest.py notes". These gate `covers:` validation (`coverage.py:196` → `valid`) and deferral duplication checks.
- **Why it matters:** the gate reports `valid` for leaves whose validation criteria never reference the artifact; symbols ending in `run`/`main`/`parse` match almost anything.
- **Minimal fix:** boundary lookarounds on both sides (the codebase already does this in `_BARE_FILE_RE`'s prefix guard); whole-identifier match for symbol leaf segments.
- **Confidence:** high on mechanics.

### [IMPORTANT] Parser accepts item IDs the covers-label grammar cannot express
- **Where:** `parser.py:33-36`/`:533` (segments `[A-Za-z0-9]+`, prefix-only check) vs `coverage.py:25-31` (`_DOTTED_ID_PATTERN` segments `\d+[a-z]?` or `[A-Z]+[0-9]+[a-z]?`)
- **Failure mode:** `- 1.1.foo - ...` parses as a valid item whose would-be label `covers:<plan>:1.1:1.1.foo` can never match `COVERS_LABEL_REGEX` — the item is permanently uncoverable by structured labels; the failure surfaces later as an inexplicable uncovered row.
- **Minimal fix:** validate item-ID suffixes in `_build_acceptance_item` against the shared dotted-ID grammar.
- **Confidence:** high.

### [IMPORTANT] `**Acceptance:**` blocks under framing/verification/deferred sections are silently discarded
- **Where:** `parser.py:218-237` (acceptance parsed only for deliverable; no check that other kinds lack the block)
- **Failure mode:** a kind-typo (`framing` on a real deliverable) silently drops its items from coverage with zero diagnostic (reproduced).
- **Minimal fix:** run `_find_acceptance_marker` for non-deliverable kinds and error if found.
- **Confidence:** high on behavior; medium on contract intent.

### [IMPORTANT] Exit-code drift: mainline error paths escape the contract's exit-code set as tracebacks (exit 1)
- **Where:** `src/gobby/cli/plan.py:74-100` (except chain); raised from `coverage.py:566-569` (`PlanParseError`), `coverage.py:428` (missing/garbled matrix file → OSError/YAMLError; `--matrix-file` lacks `exists=True`, `cli/plan.py:57`), `cli/plan.py:206-209` (`TaskNotFoundError` on bad evidence refs)
- **Failure mode:** only the six typed errors are mapped; a malformed plan file — the most likely operator error — crashes with a traceback and exit 1, outside the documented `{0,2,3,4,5,6,7,8}`.
- **Minimal fix:** catch and map `PlanParseError`, `OSError`/`yaml.YAMLError`, `TaskNotFoundError`; add `exists=True` to `--matrix-file`.
- **Confidence:** high.

### [IMPORTANT] `--task-tree` contract drift: documented `db|jsonl|path`, implemented `db|matrix-file`
- **Where:** `cli/plan.py:54` (`click.Choice(["db", "matrix-file"])`), `coverage.py:65-67` vs `docs/contracts/plan-coverage.md:255` and CLAUDE.md
- **Failure mode:** `--task-tree jsonl` is a click usage error (exit 2 — colliding with the "missing coverage" exit-2 meaning). `_load_task_records`' `task_tree_file` parameter and trailing `return ()` (`coverage.py:605`) are vestigial; a future enum value silently produces an empty task tree (all-missing). Flagged independently by two reviewers.
- **Minimal fix:** implement jsonl/path or update both docs; delete the dead plumbing or make unknown sources raise.
- **Confidence:** high.

### [IMPORTANT] Deferral tasks outside the root-task subtree always fail as `task_missing` (latent behind the deferral Blocker)
- **Where:** `coverage.py:291` (`_filter_to_scope`), `:319` (store built from scoped records only); `deferral.py:49-51`
- **Failure mode:** deferral tasks are by definition out-of-epic; the scope filter removes them from the store, so `validate_deferral`'s first lookup yields `task_missing` before the dependency-closure check can succeed. Only deferral tasks that are *descendants* of the root can validate — exactly the shape the only test uses. Once the deferral Blocker is fixed, every legitimate deferral flunks (fail-closed but wrong), pushing operators to bypass the gate.
- **Minimal fix:** build the deferral-validation store from the full project record set; keep scope filtering for covers matching and tree hashing.
- **Confidence:** medium-high.

### [IMPORTANT] `commits:<single-sha>` evidence enumerates the entire ancestor history — N+1 git subprocesses, duplicated onto every coverage row
- **Where:** `src/gobby/plans/evidence.py:123` (`git rev-list --reverse <range_>`), `:133-138` (per-commit `diff-tree`); rows each carry the identical full evidence tuple, serialized per row by `coverage_manifest.py:344-351`
- **Failure mode:** `commits:HEAD` (plausible operator value) lists every reachable commit → tens of thousands of subprocess calls and an O(items × commits) YAML manifest committed under `.gobby/plans/coverage/`.
- **Minimal fix:** reject single-rev specs or cap rev-list with an explicit error; store the evidence bundle once at header level.
- **Confidence:** high on behavior.

### [IMPORTANT] Git argument injection in evidence refs — no `--` separator
- **Where:** `evidence.py:113-123`; `cli/plan.py:189-204` (fallback `git diff <range_>`)
- **Failure mode:** option-shaped refs are accepted as argv tokens: `--evidence commits:--all` runs `git rev-list --reverse --all`, "resolving" the whole repo as evidence — the durable audit trail can be inflated/forged via flags.
- **Minimal fix:** reject refs starting with `-` (or insert `--` carefully).
- **Confidence:** high on behavior, medium on impact.

### [IMPORTANT] `_resolve_coverage_matrix` crashes on non-dict rows
- **Where:** `evidence.py:248-250` (`cast` is a no-op; `row.get` on a scalar raises AttributeError)
- **Failure mode:** `rows: ["covered"]` → traceback exit 1 instead of `InvalidEvidenceError` → exit 3 like the adjacent handling.
- **Minimal fix:** `isinstance(row_data, dict)` guard emitting `_invalid(...)`.
- **Confidence:** high.

### [IMPORTANT] `--regenerate` preservation persists stale `covered` decisions under a fresh header — the manifest asserts false provenance
- **Where:** `coverage_manifest.py:256`, `:277-281` (prior row wins when `plan_node_hash` unchanged); `storage/plans.py:200` (system path always regenerates)
- **Failure mode:** when coverage *regresses* (leaf deleted/relabeled) but plan text didn't change, the fresh `missing` row is overwritten by the prior `covered` row, while the header updates to the new `task_tree_source_hash` — the manifest claims coverage that is provably false for that tree. Deliberate per `tests/plans/test_coverage_identity.py:97-149`, but the archive gate consumes the persisted manifest ("every row covered"), so the durable record can never downgrade.
- **Minimal fix:** preserve prior rows only when both `plan_node_hash` AND `task_tree_source_hash` are unchanged, or never preserve `status` when the fresh status is missing/invalid.
- **Confidence:** high on mechanics; medium on severity (intent vs effect).

### [IMPORTANT] `task-diff` evidence has no failure mode — always `resolved`, even with an empty diff
- **Where:** `evidence.py:165-175` (contrast `_resolve_worktree_diff`'s four invalid paths)
- **Failure mode:** a task with no commits produces a resolved audit row indistinguishable from real evidence.
- **Minimal fix:** emit `_invalid(...)` on empty diff, mirroring worktree-diff semantics.
- **Confidence:** medium (possibly intentional; asymmetry suggests omission).

### [IMPORTANT] `emit_stub_manifest(plan_kind=strategy)` writes a forbidden manifest into a strategy plan and leaves it there
- **Where:** `manifest_emitter.py:77-102` (no strategy guard), `:235`
- **Failure mode:** strategy plan with deliverable sections: draft parse succeeds, manifest appended, expansion validation fails ("strategy plans must not contain a kind: manifest section"), manifest left in file (per the no-rollback Blocker); every subsequent emit re-fails and appends another Yolo section (reviewer verified growth 2→3→4). Latent — the only production caller uses the implementation default — but public API.
- **Minimal fix:** no-op (or raise) for strategy at the top of `emit_stub_manifest`.
- **Confidence:** high.

### [IMPORTANT] One corrupted `*.coverage.yaml` anywhere under the coverage root crashes every `write_manifest` call
- **Where:** `coverage_manifest.py:192-198` (`_read_manifest`, no YAMLError/OSError handling), `:149-155` (rglob entire tree), `:131-146` (parses every candidate)
- **Failure mode:** a malformed sibling manifest in an unrelated subtree makes an unrelated `write_manifest` raise raw `yaml.parser.ParserError` (reviewer reproduced). Also O(N) full-tree YAML parsing per write.
- **Minimal fix:** `except (OSError, yaml.YAMLError): return {}` in `_read_manifest`; limit candidate scanning.
- **Confidence:** high.

### [IMPORTANT] Corrupted/headerless own manifest is an unrecoverable dead end — even with `regenerate=True` — and the non-atomic write can self-induce it
- **Where:** `coverage_manifest.py:114` (non-atomic `write_text`), `:131-146`
- **Failure mode:** an existing manifest with a missing/unreadable header raises `PathIdentityMismatchError("... already belongs to None")` before `regenerate` is consulted (reviewer reproduced both variants); recovery requires manual deletion. A crash mid-write produces exactly this state.
- **Minimal fix:** temp file + `os.replace`; allow regenerate to overwrite identity-None files with an audit line.
- **Confidence:** high.

### [IMPORTANT] Manifest parser accepts self-referencing `depends_on` and a mid-document manifest — contract drift
- **Where:** `manifest_parser.py:259-268` (`valid_section_ids` includes the entry's own section), `:43-109` (no position check)
- **Failure mode:** `depends_on: ['1.1']` on entry 1.1 — and mutual cycles — pass strict parse (reproduced); the emitter enforces this but the parser (the only gate for adversary-written manifests) does not; cycles are caught only on the expansion path. A mid-document manifest with deliverables after it also parses clean despite the contract's end-of-document requirement.
- **Minimal fix:** error on self-dependency in `_validate_manifest_invariants`; error when the manifest section is not last.
- **Confidence:** high.

### [IMPORTANT] Emitter reimplements fence/kind-directive scanning with rules that diverge from the parser in four ways
- **Where:** `manifest_emitter.py:45-49`, `:414-432`, `:435-471`, `:474-497`
- **Failure mode:** (1) any-indent fence openers vs parser's 0-3 spaces; (2) `marker[:3]` truncation lets a 3-backtick line close a 4-backtick fence; (3) `_KIND_DIRECTIVE_RE` rejects the spaced form the parser accepts; (4) `_strip_manifest_section` stops fence tracking after the manifest heading, so a column-0 `## ` line inside the manifest's own YAML is treated as section end. These decide *which lines get deleted* on the replace-malformed path — a plan with 4-backtick doc fences containing manifest-shaped examples (exactly what the contract doc contains) can have real content stripped and reported as `"replaced_malformed"` success.
- **Minimal fix:** export and reuse `_compute_fence_mask`/`_KIND_LINE_RE` from parser.py.
- **Confidence:** high on divergence; medium on data-loss reachability.

### [IMPORTANT] Literal `%s` in the consumer-sweep destructive-marker regex — RENAMED/MOVED FILE annotations never match
- **Where:** `consumer_sweep.py:27-31` (`RENAMED%s FILE|MOVED%s FILE` — typo for `\s`)
- **Failure mode:** runtime-proven: `"RENAMED FILE"` → no match, `"RENAMED%s FILE"` → match. Two of eight documented destructive markers are dead; `(MOVED FILE)` annotations never trigger the file-level consumer sweep.
- **Minimal fix:** replace `%s` with `\s+`; add a marker-matrix test over all eight phrases.
- **Confidence:** high.

### [IMPORTANT] Archiving a plan wedges close of its root task — ledger survives, manifest is deleted, verification doesn't filter archived plans
- **Where:** `bootstrap_ledger.py:196-205` (`_matching_plan_entries` has no `state = 'active'` filter); `storage/plans.py:202-265` (`archive_plan` removes the manifest at `:264`, never touches the ledger)
- **Failure mode:** after `gobby plans archive`, every `close_task` on the root (`storage/tasks/_transitions.py:624-626`) finds the surviving ledger, fails manifest load, raises `BootstrapLedgerMismatchError` — the root can never close through the normal path until the ledger is manually removed.
- **Minimal fix:** filter to active plans and/or have `archive_plan` move the ledger companion.
- **Confidence:** high on mechanics; medium on frequency.

### [IMPORTANT] Ledger content identity never cross-checked against the plan entry that located it
- **Where:** `bootstrap_ledger.py:122-126` (entry values are fallbacks, never compared), `:66`/`:87`
- **Failure mode:** a ledger found by task-200's filename whose body declares `plan_id: task-100-plan` is verified against task-100's manifest; if self-consistent, close passes with task-200's coverage never checked. Copy-paste ledger bootstrapping makes this realistic.
- **Minimal fix:** when both ledger and entry carry `plan_id`/`root_task_ref`, append a mismatch if they differ.
- **Confidence:** high on mechanics, medium on incidence.

### [IMPORTANT] Deferral ref membership compares raw strings against store-normalized `#N` refs — false rejects and a closure-exclusion bypass
- **Where:** `deferral.py:95`, `:167-168` vs normalization in `coverage.py:683-687`, `:726-771`
- **Failure mode:** YAML `task_ref: 12345` (unquoted int — the natural way to dodge YAML's `#`-comment trap) yields `"12345" ≠ "#12345"`: dependency-path acceptance falsely fails; conversely `cited-parent:12345` for a parent *inside* the closure evades the "NOT a transitive dependency" requirement. Moot until the deferral Blocker is fixed, then immediately live.
- **Minimal fix:** normalize all refs through one helper before comparison.
- **Confidence:** high on mechanics.

### [IMPORTANT] Latent: `_coerce_task_record` maps the real tasks.jsonl dict-shaped `state` to "ready" — closed tasks would pass the deferral gate from record-based trees
- **Where:** `coverage.py:696` (`_first_string(raw, "state", default="ready")`), `:748-753` (skips non-str values)
- **Failure mode:** real `.gobby/tasks.jsonl` rows store `state` as a dict (`{"is_closed": true, ...}`); `_first_string` ignores it, so every task — including closed — coerces to active `"ready"`. Only tests pass `task_records` today, but wiring the contract-promised jsonl source turns "a closed task fails the gate" into "a closed task passes".
- **Minimal fix:** derive state from the dict (`is_closed`/`is_escalated`/stage) mirroring `_live_task_state`; default to a non-active sentinel.
- **Confidence:** high on mechanics; latent.

### [IMPORTANT] `_file_destructive_intents` continuation scan diverges from `collect_target_inventory` — markers below a pathless bullet are dropped
- **Where:** `consumer_sweep.py:219-232` (breaks at first no-path continuation) vs `semantic_lint.py:168-181` (continues on bullet/backtick/slash lines)
- **Failure mode:** `Targets:` / `- src/a.py` / `- see notes` / `- src/b.py (DELETE FILE)` — inventory records `src/b.py` but the destructive scanner stops at "- see notes" and never sees DELETE FILE; the file-level sweep is silently skipped. Related: a non-empty header rest (`Targets: (DELETIONS ONLY)`) swallows the whole block in both scanners (`semantic_lint.py:161-165`).
- **Minimal fix:** share one Targets-block iterator between the two modules.
- **Confidence:** high on mechanics, medium on frequency.

### [IMPORTANT] Sync DB/file work on the daemon event loop across the plans gate surfaces (systemic)
- **Where:** `mcp_proxy/tools/internal.py:281-283` invokes sync tool funcs inline → `mcp_proxy/tools/plans/__init__.py:59,176-205` → `storage/plans.py:189-200` → `coverage.py:625-643` (full-project task pagination, synchronous); `consumer_sweep.py:262,312-316,338,370-373` called from async `spawn_agent_impl` (`mcp_proxy/tools/spawn_agent/_implementation.py:235,309-316`) via `_plan_gate.py:84-89` — additionally with no DB-error handling there (the CLI wraps in `except (OSError, psycopg.Error, ValueError)`, `cli/plans.py:243`; the spawn gate does not, so a transient PG error raises out of spawn)
- **Failure mode:** `create_plan`/`regenerate_coverage_manifest` MCP calls and every planner/adversary spawn block the proxy event loop for plan parsing, full task-tree pagination, and multiple synchronous PG round-trips.
- **Minimal fix:** dispatch via `anyio.to_thread.run_sync`; align the spawn gate's error contract with the sweep's `skipped` semantics.
- **Confidence:** high.

## Findings — Important (sync/)

### [IMPORTANT] Import round-trip drops six real task columns exported inside `state`
- **Where:** export packs `allow_automation`, `unattended`, `isolation`, `assigned_agent`, `implementation_domain`, `additional_skills` into `state` (`tasks/state_semantics.py:179-199`; export `tasks.py:167-170`); import's `synced_values` (`tasks.py:405-439`) never reads them
- **Failure mode:** fresh-machine import loses isolation mode, agent assignment, domain, and skills routing — silently changing dispatch behavior.
- **Minimal fix:** read them from `state` on import (force `allow_automation=false` if that's policy — then document it).
- **Confidence:** high.

### [IMPORTANT] Phase-2 dependency inserts can roll back the entire import
- **Where:** `tasks.py:495-497` (deps collected for every line, including lines skipped at `:306-321`), `:500-510` (inserted in the single deferred-constraints transaction; FKs surface at COMMIT)
- **Failure mode:** one dep pointing at a task absent locally and absent from the file (deps are unscoped while export is project-scoped, `tasks.py:152`) fails the FK at commit → all upserts roll back; recurs every import until hand-fixed.
- **Minimal fix:** skip deps whose endpoints won't exist post-import (check existing ∪ inserted ids), logging each skip.
- **Confidence:** high.

### [IMPORTANT] LWW UPDATE writes the file's seq_num with no collision handling — one divergent seq aborts every future import
- **Where:** `tasks.py:435` (incoming seq preferred), `:486-492` (UPDATE); collision avoidance exists only on INSERT (`:441-459`); unique index `(project_id, seq_num)`
- **Failure mode:** a shared task carrying seq 100 in the file while a different local task owns 100 → unique violation at commit → whole-import rollback, recurring.
- **Minimal fix:** keep local seq/path_cache on update (they're machine-local identifiers).
- **Confidence:** high on mechanism, medium on frequency.

### [IMPORTANT] path_cache rebuild is file-order dependent — children imported before parents get truncated paths
- **Where:** `tasks.py:460-477` (parent looked up in DB mid-insert; export sorts by UUID at `:163`; JSONL `path_cache` unconditionally discarded for new tasks)
- **Failure mode:** fresh-machine import corrupts hierarchy paths for any child sorted before its parent. `path_cache` is load-bearing: dispatch ref resolution (`dispatch/context.py:120`) and coverage scoping (`coverage.py:800-801`) use it. No repair job exists.
- **Minimal fix:** two-pass import (insert all, then compute path_cache), or trust JSONL `path_cache` when seq was preserved.
- **Confidence:** high.

### [IMPORTANT] Memory LWW compares timestamps as strings without offset normalization — wrong winner for non-UTC offsets
- **Where:** `memories.py:39-46` (`_parse_updated_at` preserves the original offset), consumed by `_merge_memory_records` (`:129-152`)
- **Failure mode:** `"...12:00:00+02:00"` (10:00 UTC) string-compares newer than `"...11:30:00+00:00"` (11:30 UTC) → merge keeps the older content. Hub rows are serialized with the session timezone offset (`storage/hub/postgres.py:375-378`), so non-UTC strings are realistic.
- **Minimal fix:** parse, coerce to UTC, compare datetimes — reuse `_parse_timestamp` from sync/tasks.py.
- **Confidence:** medium-high.

### [IMPORTANT] `import_sync` gates restore on a raw line-count heuristic and swallows all errors
- **Where:** `memories.py:294-305` (`if not force and file_count <= db_count: return 0`), `:311-313` (catch-all → 0); same pattern in `backup_sync` (`:265-269`) and `_export_memories_sync` (`:646-649`)
- **Failure mode:** a project file with 300 records on a machine whose DB has 500 unrelated memories silently restores nothing; any failure returns 0, indistinguishable from no-op — `gobby memory restore` exits 0. (Independently flagged in `docs/reviews/cli-core.md:116-118`; still unfixed at d690b.)
- **Minimal fix:** drop the count gate (dedup already makes import idempotent); return typed errors so the CLI can exit nonzero.
- **Confidence:** high.

### [IMPORTANT] Integrity check reports "clean" when git commands fail or time out
- **Where:** `integrity.py:132-160`; `run_git_command` returns None on failure/timeout and `""` on clean (`utils/git.py:44-86`) — treated identically after `checked=True` is already set (`:127-128`)
- **Failure mode:** a failed/timed-out `git diff` yields `checked=True, all_clean=True` → `gobby sync` prints "All bundled content is clean" and syncs potentially tampered bundled content — defeating the documented fail-closed design.
- **Minimal fix:** distinguish None from `""`; on None set `checked=False` (existing block-all path) and record an error.
- **Confidence:** high.

### [IMPORTANT] Pre-push hook's sync commit is never included in the push it runs under, and it sweeps the user's staged changes into the sync commit
- **Where:** `cli/installers/git_hooks.py:100-117` (verified: `git add` + bare `git commit -m "gobby: sync tasks/memories" --no-verify`); legacy copy `hooks/git/pre-push:22-38`
- **Failure mode:** git resolves push refs before pre-push runs — the commit created inside the hook is not pushed; the remote JSONL permanently lags one push behind and the local branch ends ahead-by-1 after every push. The bare `git commit` also commits the entire index, silently folding the user's unrelated staged files into the sync commit.
- **Minimal fix:** `git commit --only -- .gobby/tasks.jsonl .gobby/memories.jsonl`; print that a second push is required (or re-exec the push).
- **Confidence:** high.

### [IMPORTANT] No deletion propagation anywhere — deleted tasks, dependencies, and memories resurrect
- **Where:** task import is pure upsert (`tasks.py:239-526`; deps `ON CONFLICT DO NOTHING`); memories export re-merges file-only records (`memories.py:560-577`)
- **Failure mode:** delete a task on machine A → B still has it → B's export re-adds it → A imports → resurrected. Deleted memories resurrect on the same machine's next export because the file copy is merged back.
- **Minimal fix:** tombstones (e.g. `deleted_at` retained in JSONL for N days) honored by merge; at minimum document that deletion does not sync.
- **Confidence:** high.

### [IMPORTANT] Import LWW snapshot read outside the transaction — concurrent local edits silently overwritten
- **Where:** `tasks.py:264-296` (`existing_tasks` and seq maps loaded via `fetchall` before `with self.db.transaction()`)
- **Failure mode:** a task updated by the daemon between snapshot and commit is clobbered by a file row that beat only the stale snapshot's timestamp (TOCTOU LWW); a concurrent same-id insert aborts the whole import on PK violation.
- **Minimal fix:** move snapshot queries inside the transaction, or compare-and-set per row (`UPDATE ... WHERE updated_at <= %s`).
- **Confidence:** high on mechanism; window is small.

### [IMPORTANT] Export caps at 100,000 tasks with silent truncation
- **Where:** `tasks.py:148` (`list_tasks(limit=100000, ...)`)
- **Failure mode:** past the cap, tasks silently vanish from the exported file — and per the overwrite-export Blocker, from the canonical git file. This repo already carries 15k+ tasks.
- **Minimal fix:** page through results, or fail loudly when `len(tasks)` hits the limit.
- **Confidence:** high.

### [IMPORTANT] GitHub import's update path unconditionally clobbers local task fields and wipes labels
- **Where:** `sync/github.py:163-173` (`update_task(..., labels=issue_labels or None)` with no recency guard; `None` → `"[]"` per `storage/tasks/_updates.py:87-92`)
- **Failure mode:** every re-import destroys local title/description edits, and an unlabeled GitHub issue *clears* local labels. (Linear import has the same unconditional overwrite at `linear.py:512-521`.)
- **Minimal fix:** reconcile with change detection; never map "no labels" to "clear labels".
- **Confidence:** high.

### [IMPORTANT] GitHub issue state is never mapped in either direction; label-mapping helpers are dead code
- **Where:** `github.py:154-183` (import ignores `issue["state"]` — closed issues import as open tasks), `:242-252` (push sends only title/body — closing a task never closes the issue); `map_gobby_labels_to_github`/`map_github_labels_to_gobby` (`:327-351`, `:400-424`) have zero production callers
- **Failure mode:** the docstring claims bidirectional sync; status — the load-bearing field — is wholly unsynced.
- **Minimal fix:** map state both ways; wire or delete the label helpers.
- **Confidence:** high.

### [IMPORTANT] No pagination on GitHub import — first page only, reported as success
- **Where:** `github.py:144-150` (single `list_issues` call, no page loop; GitHub default page size 30)
- **Minimal fix:** loop on `page` until a short page.
- **Confidence:** high that no pagination exists.

### [IMPORTANT] Linear cron handler converts all failures into success — scheduler backoff never engages
- **Where:** `linear.py:957-968` (`except Exception → return f"Linear sync failed: {e}"`); `scheduler/executor.py:66-83` marks any normal return completed; partial-error results also flow into a success string (`:961-965`)
- **Failure mode:** persistent failures run hot every 300s per project forever, recorded as successful runs — same class as the github_triage cron finding in #15787.
- **Minimal fix:** re-raise after logging, or return a failure status the executor honors.
- **Confidence:** high.

### [IMPORTANT] Sync cursor captured after pull+push — edits during the sync window are permanently skipped; cursor advances despite push errors; lexicographic timestamp compare
- **Where:** `linear.py:882-884` (`synced_at = now()` after both phases), `:728-729` (`sync_active_forward` advances unconditionally even with `push_stats["errors"] > 0`), `:810` (string compare between Linear `...Z` and local `+00:00` formats, trusting local wall clock)
- **Failure mode:** a local edit made after push's SELECT but before the cursor write is never pushed; a Linear edit during the window is never pulled; format mismatch breaks ordering inside equal-second boundaries.
- **Minimal fix:** capture the cursor before pulling; advance from observed timestamp maxima; parse with `datetime.fromisoformat`.
- **Confidence:** high on cursor placement.

### [IMPORTANT] Linear availability gate checks the broken path and ignores the working one
- **Where:** `linear.py:954-955` (cron skips unless the Linear MCP server is available) and `require_available()` at the head of every method, while all working operations go through the GraphQL client needing only `linear_api_key`
- **Failure mode:** API-key-only setups never sync ("Linear MCP server unavailable" forever); MCP-only setups pass the gate into the CallToolResult Blocker.
- **Minimal fix:** `is_available()` = `mcp_available or graphql_client_configured`.
- **Confidence:** high.

### [IMPORTANT] Priority scales mis-mapped in both directions — Gobby "critical" becomes Linear "No priority"; Linear "No priority" imports as Gobby "critical"
- **Where:** raw passthrough at `linear.py:572/583/627/649` (push) and `:496/815` (import/pull). Gobby: critical=0…backlog=4 (`storage/tasks/_models.py:21`); Linear: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low
- **Failure mode:** the most urgent work renders unprioritized in Linear; untriaged Linear issues land as critical Gobby tasks (which dispatch ordering consumes); every other level is off by one.
- **Minimal fix:** explicit translation tables both ways.
- **Confidence:** high.

### [IMPORTANT] Unscoped team-wide import + seq-title matching can hijack foreign issues onto the wrong task
- **Where:** `linear.py:418-432` (`projectId` filter only when a binding exists) + `:498-510` (a Linear title matching `^#(\d+):` links to whatever local task has that seq)
- **Failure mode:** with no project binding, import ingests every issue in the Linear team — including issues pushed by other Gobby projects whose `#N:` titles collide with this project's seq numbers → `linear_issue_id` written onto an unrelated task → subsequent pushes overwrite the foreign Linear issue.
- **Minimal fix:** require the project binding before import; scope seq-matching to the bound project.
- **Confidence:** med-high.

### [IMPORTANT] Sync DB, secret, and crypto work on the daemon event loop; per-task redundant network/secret fetches
- **Where:** blocking psycopg calls inside async methods throughout `linear.py` (`:488,501,506,514,683,708,761,844` …); `_get_graphql_client()` (`:233-234`) does a synchronous `SecretStore.get` (DB + Fernet) re-invoked per task in `_push_task_rows`, plus a `list_team_states` GraphQL round-trip per pushed task (`:563-567`) with no caching
- **Failure mode:** N dirty tasks every 300s = N secret reads + N state queries + N mutations serialized on the shared daemon loop.
- **Minimal fix:** resolve client and team-state map once per run; wrap storage calls in `asyncio.to_thread`.
- **Confidence:** high.

### [IMPORTANT] No rate-limit handling anywhere; the typed rate-limit/not-found exception taxonomy is dead code
- **Where:** `LinearRateLimitError`/`LinearNotFoundError` (`linear.py:112-140`) and `GitHubRateLimitError`/`GitHubNotFoundError` (`github.py:35-63`) raised nowhere; no 429-specific behavior; Linear mutations non-idempotent under generic retry
- **Minimal fix:** raise them at classified call sites or delete them.
- **Confidence:** high.

## Findings — Nits

### [NIT] plans/: parser and matcher polish
- `parser.py:161-163` — decode with `utf-8-sig` (BOM breaks line-1 anchored regexes); wrap decode errors in `PlanParseError`.
- `parser.py:686-690` vs `_artifact_refs.py:146-147` — two divergent `_clean_ref` implementations; behavior differs exactly on corrupted-ref inputs.
- `parser.py:61-63` — `strip_section_dependencies` leaves doubled whitespace in titles (consumed by expansion title building).
- `parser.py:409-415,429` — invalid `kind:` value reported as "missing kind:"; non-canonical-heading message hardcodes "framing".
- `coverage.py:376-384` — a valid leaf masks invalid covers records for the same item (audit loses the bad labels).
- `coverage.py:25-31` — `COVERS_LABEL_REGEX` grammar is looser than the canonical heading grammar (e.g. `A1.B2`); such labels can never resolve and are then silently dropped.
- `evidence.py:253-257` — an embedded evidence list whose entries are all non-dict yields zero rows silently.
- `evidence.py:336-347` — `_parse_diff_files` mis-parses paths with spaces or git-quoted names (audit-only).
- `coverage.py:402-409` — deferral-invalid rows carry no reason; `_validate_deferral_status`' rich status string is dropped (manifest shows bare `invalid`).

### [NIT] plans/: manifest and emitter polish
- `manifest_emitter.py:121-137` — dead branch: the expansion re-parse after a successful draft parse with entries can never fail; `"replaced_malformed"` only arises via the draft-failure path.
- `coverage_manifest.py:100-103` — unreachable identity re-check (already raised inside `_ensure_path_identity`).
- `coverage_manifest.py:108` vs `:114` — regenerate audit logged before the write it describes; a failed write leaves a false audit line.
- `manifest_parser.py:96-98,122-128,229` — every entry shares one `source_line` (the fence start); unquoted `source_section: 1.1` (YAML float) reported as "missing fields" rather than a type error.
- `manifest_emitter.py:99-101`/`:500-517` — `emit_stub_manifest` on a nonexistent path creates a phantom file containing only a Yolo Fallbacks section.
- `coverage_manifest.py:256,259-283` — regenerate's plan-side-only stability key is deliberate (tested) but means deleted leaf tasks can't invalidate covered rows; deserves an inline design note.

### [NIT] plans/: sweep/lint/ledger polish
- `consumer_sweep.py:291-293,325-327` — production SQL branch untested; `CodeIndexStorage` implements neither `find_direct_callers` nor `find_direct_file_consumers`, so the tested interface is fake-only; `_module_candidates` is dead weight in production.
- `bootstrap_ledger.py:257` — `set(actual) != set(expected)` collapses duplicate titles (2 expected leaves sharing a title vs 1 actual passes); `:244-271` ignores manifest row `status`; `:79-107` error anchors point at the last iterated plan, not the mismatching one; `:335-345` ambiguous task-ref prefixes silently resolve to first match; only 2 tests total.
- `semantic_lint.py:32-60` — `_WORK_TABLE_HEADERS` misses "description"/"summary"/"action"/"phase", so `| ID | Description |` work tables never count (weakens the table-row decomposition contract); `:372-374` a pipe-less `---` counts as a table separator (phantom tables); `:16-21` vs `:61-85` suffix lists disagree (java/php/rb); `:298-312` change-verb must precede the path on the same line; `:193-196` re-reads the plan from disk per section when `source_lines` is empty.
- `deferral.py:21-23` — `_ACTIVE_TASK_STATES` substitutes "ready" for the documented "open"; harmless today, foot-gun for future `TaskStoreProtocol` impls.

### [NIT] sync/: polish
- `linear.py` at 970 lines — 30 under the monolith cap; one class owns discovery, binding, import, push, pull, orchestration, state mapping, and cron wiring. The next feature crosses 1,000; a pre-emptive split (mapping/import/push-pull) is warranted.
- `github.py:138` — `repo.split("/")[1]` IndexError on malformed repo; `parse_github_repo` is already imported and used elsewhere in the file.
- `github.py:201-205` — `db.execute(...).fetchone()` fetches from a cursor whose transaction context already exited; works only because psycopg buffers client-side; use `db.fetchone`.
- `linear.py:266-269` — `title.startswith(ref)` lacks a boundary: `#4` matches `#42:`; compare `f"{ref}:"`. `:262-264` — `seq_num == 0` falls through to UUID ref (truthiness).
- `linear.py:155-161` — `_extract_record` returns the whole result dict as fallback, masking shape errors; `:578-580` sends both `"id"` and `"issueId"` speculatively; `:534` "Imported N" counts updates.
- `linear.py:101-103` — module-global mutable failure limiter shared across all projects' cron handlers.
- `linear.py:907` — `escalated → "Canceled"`: an attention-needed task reads as canceled in Linear.
- `tasks.py:565` — `repo.rstrip(".git")` strips a character set (`"audit"` → `"aud"`); use `removesuffix(".git")`.
- `tasks.py:528-536` — `get_sync_status` checks the daemon-cwd-relative `self.export_path` instead of `_get_export_path(project_id)`; reports `no_file` even when the project file exists.
- `memories.py:204-224` — daemon-side export path can resolve from the daemon's cwd; pass the project repo_path explicitly as tasks' helper does.
- `memories.py:470-471` — punctuation/emoji-only memories normalize to empty and are silently dropped from both import and export merge.
- `src/gobby/hooks/git/post-merge:8` — legacy uninstalled hook passes `--auto`, which `sync_tasks` does not define (Click exit 2 if ever run); nothing installs `src/gobby/hooks/git/*`; delete or align the directory.

## Systemic patterns

1. **Fail-open leniency in gates.** The plan parser degrades malformed grammar-adjacent input (separator typos, unclosed fences, trailing prose, Acceptance under wrong kinds) into prose/truncation instead of errors; the coverage evaluator treats absent data as fine (missing matrix hash → skip check, dangling labels → vanish, empty embedded evidence → vanish); the sweep/lint/ledger layer degrades every missing-scope condition to `valid` with at most a `skip_reason` callers ignore. For contract-enforcement code, every silent degradation is a hole in the gate — roughly a third of all findings are instances of this one pattern.
2. **Fixture–reality drift.** Both deferral Blockers, the consumer-sweep Blocker, and the CallToolResult Blocker share one root: test doubles model shapes production never produces (hand-fabricated deferred sections with acceptance items, fully-dotted `qualified_name`, `find_direct_*` methods that don't exist on real storage, `call_tool` stubs returning dicts at every level including E2E). Nothing integration-tests the parse→evaluate, sweep→real-index, or sync→real-manager seams.
3. **Reimplemented grammar/logic drifting.** Two `_clean_ref`s, two fence scanners (emitter vs parser, 4 divergences), two ID grammars (parser bullets vs covers labels — already drifted into a real bug), two Targets-block iterators (sweep vs lint), scattered ref normalization (coverage adds `#`, ledger strips it, deferral compares raw).
4. **Write-then-validate without rollback / no write atomicity.** `_emit_fresh` mutates the plan file before validating and doesn't restore; `coverage_manifest.write_text` and both JSONL exporters write in place with no temp+rename, no fsync, no locks; append-only Yolo/audit sections grow unboundedly and log before the writes they describe.
5. **External-wins, write-unconditionally sync.** No per-task conflict detection anywhere in tracker sync; reconcile bumps `updated_at` even for identical values, turning every pull into an echo push; LWW has no tombstones, so deletions cannot propagate; "bidirectional" state sync is aspirational in both docstrings (Linear state pull and all GitHub state/label mapping are dead code).
6. **Errors counted, never typed, success defaulted.** Memories sync swallows everything to `return 0`; the Linear cron converts total failure into a successful run; integrity reports clean on git failure; the coverage CLI leaks tracebacks outside its documented exit-code set; typed exception taxonomies exist but are raised nowhere.
7. **Doc/code contract drift.** The canonical deferral YAML example doesn't parse; `--task-tree db|jsonl|path` is actually `db|matrix-file`; the documented symbol-ref format can't be resolved by the sweep; CLAUDE.md's `$N` placeholder rule remains stale against the psycopg `%s` reality.

**Health read:** the structural parser core and the db-path happy case of the coverage gate are well-built and well-tested, but every qualitative enforcement edge the contract actually depends on — deferrals, dangling labels, matrix trust, consumer sweep, bootstrap ledger — is either dead code, bypassable, or fail-open at this commit, while green tests validate shapes production never produces. The sync layer is a backup format wearing a sync costume: single-machine disaster recovery works; the cross-machine "git is source of truth" contract and both external-tracker integrations do not survive contact with their mainline paths.
