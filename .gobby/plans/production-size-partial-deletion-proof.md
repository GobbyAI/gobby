# Partial-Deletion Proof for production-size-growth

Plan artifact: `.gobby/plans/production-size-partial-deletion-proof.md`

**Plan ID:** production-size-partial-deletion-proof

## Overview
`kind: framing`

Task #22727 (Plan a safe partial-deletion exemption for production-size-growth)
under the Lane 7 planning epic. It was found during #22720, while folding the
Chrome council findings. A Chrome plan deliverable only had to drop the
`UNNAMED_PANE` re-export from a 927-line Rust module. It still tripped
`production-size-growth`, so that plan added an unrelated pane-accessor split
to pass validation.

The fix has two halves, as Josh chose (option B):
- **Preflight (1.1).** A plan can declare a typed `operation: delete-lines`
  proof on a Targets entry. The proof binds the file's current Git blob and
  lists the exact line ranges to delete. Plan validation recomputes the result
  and accepts the entry in place of a split, but only when the result's
  production line count does not grow and stays under 1,000.
- **Committed result (2.1).** When the leaf compiled from that deliverable
  closes, gate 8 checks the close candidate. The file at that commit must be
  byte-identical to the base minus the declared lines. No added line can hide
  behind the proof.

The adversarial review also found a generic close-finalization race:
`commit_close` adopts a re-fetched task row after its last gate-input check.
The Orchestrator placed the fix in this plan as 2.2 (19:44 CT, 2026-10-05),
because it is what keeps 2.1's proof binding intact.

Anything the grammar cannot express exactly fails closed: mixed add/delete
edits, in-file moves, renames, malformed or stale proofs. Those keep today's
split requirement. Whole-file deletion (`operation: delete`) is unchanged and
stays a separate path.

This plan does not depend on the Chrome plan or its workaround. It does not
touch gclient. The pane-accessor split that plan already carries stays as it
is.

Ownership: all three leaves are Python daemon work. The Orchestrator routes
them to developer seats.

## Decision Record
`kind: framing`

1. **Proof model (confirmed by Josh).** Josh chose option B about 10:11 CDT
   on 2026-10-03, verbatim selection "B: preflight + committed
   (Recommended)". The exemption requires a preflight typed deletion proof
   and a committed-result proof. The Orchestrator recorded this in the #22727
   description on 2026-10-05. Option A (preflight only) was rejected: nothing
   would stop the implementation from adding lines that the plan never
   declared.
2. **Grammar.** One Targets bullet carries the proof as a fixed-order `::*`
   entry: `operation: delete-lines`, `base-blob`, `lines`, then
   `scope-reason`. Deliverable 1.1 holds the exact grammar, path rule,
   scope-reason rule and candidate rule under "Proof contract", so its
   compiled leaf carries them. The design choices behind them:
   - `scope-reason` comes last, because `symbol_targets.parse_target_line`
     consumes everything after it.
   - Metadata carries no backticks. A second backticked span would parse as
     another target token.
   - The target is always `::*`, because the committed check constrains the
     whole file's bytes.
   - Ranges must leave at least one line. Deleting every line is whole-file
     deletion.
   - Candidate detection reads the entry's ` — `-separated metadata
     segments. It reads the target token only to check whether it is
     literally `operation: delete-lines`, so a file named `delete-lines.py`
     stays an ordinary target.
   - A malformed candidate is an error, never an ordinary target. That
     includes a candidate whose path cannot be recovered.
3. **Exclusivity.**
   - A proof is the only Targets entry for its path in its deliverable.
   - Every other deliverable that targets the same path must transitively
     depend on the proof owner. An earlier editor would make the base stale
     before the proof leaf starts.
   - A proof entry never satisfies whole-file deletion
     (`_is_whole_file_deletion` requires exactly `operation: delete`). An
     entry carrying both operations fails the proof grammar.
   - Exclusivity is a plan-validation rule only. The close gate does not
     re-check it: two same-path proofs with different results cannot both
     match the candidate's bytes, so byte equality already binds the file.
4. **Line model and counting.** Deliverable 1.1 holds the exact line model
   and count rule under "Proof contract". In short:
   - the projection keeps the retained lines byte for byte, terminators
     included;
   - the base must be strict UTF-8 with no lone `\r`, so the `\n` line model
     and the universal-newline count agree;
   - base and projection are both counted with today's production rule, and
     the projected count must be at most the base count and below 1,000.
   A base already at or above 1,000 needs no separate guard. It passes only
   when the deletion brings the projection below 1,000, because shrinking a
   file into compliance should not force a split. Deleting a Rust
   `#[cfg(test)]` line that exposes test lines fails. Deleting only
   test-tail lines passes.
5. **Fail-closed preflight.**
   - A proof is validated whenever it is present, whatever the file's size.
   - It applies only to a hand-maintained production file that is neither a
     symlink nor reached through one.
   - When a deliverable carries a proof for a file, the split heuristic is
     never consulted for that file. A stale or invalid proof is an error,
     even beside a valid split paragraph. Prose never exempts.
6. **Issue codes.**
   - Every proof failure reports code `production-size-growth`, with the
     reason in `details["proof_error"]`. `gobby plans validate` falls back to
     DB-backed validation only when every structural issue has that code
     (`src/gobby/cli/plans.py:407-416`). A new code would break validation of
     every landed plan whose proof base has since moved.
   - The ordering rule in item 3 reports `shared-target-ordering`. It is
     structural and independent of landing state.
7. **Committed-result gate.**
   - It runs inside close gate 8 (`task_scope`) under the new error
     `size_proof_mismatch`. `scope_justification` never cures it.
   - It runs for `completed` and `already_implemented`, the two reasons that
     mark a section delivered (`src/gobby/tasks/expansion/_validate.py:131`).
     Other no-work reasons never mark the section complete, so they owe
     nothing.
   - A proof-bearing close needs an explicit candidate commit. HEAD is never
     inferred, per `select_close_candidate`.
   - The gate reads the base object with `git cat-file blob` and projects it
     with the same function the preflight uses. The candidate's entry at the
     path must be exactly one `blob` with mode `100644` or `100755` whose
     object ID equals the projected blob ID.
   - Rename, removal, symlink (`120000`) and gitlink (`160000`) fail.
   - An exec-bit change leaves the bytes untouched and cannot grow the file,
     so the size proof does not cover it.
   - Gate 8 still records exactly one result. A proof failure takes
     precedence as `size_proof_mismatch` and carries any simultaneous scope
     diagnostic. A scope evaluation error never skips the proof check, and a
     deliberate close of an escalated task never waives it.
   - A Git error, timeout or parse error during the check is a failure. Only
     cancellation escapes the check.
8. **Where the gate runs.**
   - The close path is the only enforcement point. `_task_has_landed_commit`
     marks a section complete as soon as a tagged commit reaches HEAD, before
     the close, and plan lint then skips it.
   - The gate-order test pins items 1..13 with unique names, so the check
     joins gate 8 rather than becoming a 14th gate.
   - The gate-8 block stays in `_evaluate_close`, because many tests patch
     `_lifecycle_close.evaluate_task_scope`. The new logic lives in a new
     module, and `_evaluate_close` gains only the call and the result wiring.
   - The proof lines are bound for the whole close. The task description is
     mutable during the bounded review, while the candidate and object store
     are not. `CloseEvaluationFingerprint` therefore captures the
     description's proof lines, and finalization's existing fingerprint
     recheck turns a proof edited after evaluation into a stale close. A
     description edit outside the proof lines still closes.
   - That recheck runs before `link_close_commit_shas` re-fetches the row,
     and it is the re-fetched row's `updated_at` that the close transition
     uses as its compare-and-set. 2.2 repeats the comparison on that linked
     row, so the transition adopts only a row whose gate inputs match the
     evaluation. A later change fails the compare-and-set with
     `TaskStaleStateError`.
   - Finalization's scope recheck also runs before that fetch, and scope
     reads the row's declared Targets. 2.2 therefore adds the description's
     normalized declared Targets paths to the fingerprint. An edit to a
     Targets entry's symbol or scope-reason that keeps its path still
     closes.
9. **Base drift.** The base binding is exact by design. When another change
   moves the file after the plan is validated, the close fails closed. To
   recover:
   - refresh `base-blob` and `lines` in the canonical plan and revalidate it;
   - re-derive M1 through the stale-manifest route;
   - have the Orchestrator update the leaf's Targets entry to the refreshed
     line.
10. **Rejected alternatives.**
    - Exempting prose that contains "delete" lets a mixed edit pass.
    - A deletion-only diff check at close is weaker. It cannot bind the
      result to the reviewed base and ranges, and it needs patch parsing.
    - A generic proof framework is out of scope.
    - A new lint code breaks the CLI fallback (item 6).
    - Moving the gate-8 block out of `_evaluate_close` churns many test
      patch sites.
    - Re-running the Git check in finalization or in the review gate is not
      needed. The candidate and object store are immutable, and the
      fingerprint already binds the proof lines (item 8).
    - Fingerprinting the whole description would stale a close on any
      unrelated description edit. The fingerprint holds only the parts the
      gates read: the proof lines (2.1) and the declared Targets paths (2.2).
    - A proof-only recheck on the linked row would leave the same window
      open for every other gate input, such as `validation_criteria`. 2.2
      reuses the full fingerprint comparison.
    - Re-running scope evaluation on the linked row repeats its Git work.
      The declared Targets are the only row-held scope input the
      fingerprint lacks; `validation_criteria` is already in it.

## As-Is Facts
`kind: framing`

Source read at `0b676c6453`. Each excerpt is `gcode evidence` complete with no
warnings. The `semantic_lint.py` hashes match Adv2's prep message.

- `src/gobby/plans/semantic_lint.py:582-637` (`abaaeed667dbf01dc6fdca9db9c31e102031adb308013ace2bcaf75b42ab50a7`):
  - `_lint_production_size_growth` skips files below 850 lines (`594`).
  - It exempts a file only by `_is_whole_file_deletion` or a matched split
    paragraph (`597`).
  - `_is_whole_file_deletion` requires every entry for the file to be `::*`
    with exactly `operation: delete` (`635`).
- `semantic_lint.py:783-794` (`f372558d7217a672fc8163358d3087a3825fed88584bf638a9b1ce2ee4e13c31`):
  - `_line_count` reads with `errors="ignore"` and stops a `.rs` file at
    `_RUST_TEST_MODULE_RE` (`789-790`).
  - An `OSError` counts as 0.
- `semantic_lint.py:185-210` (`ef32f472e042628a82198d466fc6e08fa22d43b03a7ce51dd705d8f66ad0617d`):
  `lint_plan_document` skips the size lint for sections in
  `completed_section_ids` (`203`).
- `src/gobby/tasks/expansion/_validate.py:125-134` (`a6e3d88c51c5e03384d43637c1e87cd83dd98103378a22a8931dd69e2ae166c7`):
  a section is complete when its task closed `completed` or
  `already_implemented`, or when it has a landed tagged commit (`131-133`).
- `src/gobby/cli/plans.py:407-416` (`b7b3266433b142f75b0bfc5c6d53d13b27772a95814a1955e9c65fc54823749b`):
  a structural failure proceeds to DB-backed validation only when every
  issue's code is `production-size-growth`.
- `semantic_lint.py:551-579` (`1cad6dd6c41af50a413aed961b66ddce02ce72a4a076283e993fd12ebab859bb`):
  `_lint_shared_target_ordering` builds `_dependency_graph` and requires a
  path between every pair of owners of a primary path.
- `semantic_lint.py:299-331` (`462be57bed83555270c7f27ed44ff54008f5d991fbd7d63c41dc15bb51696a8d`):
  `_iter_inventory_lines` does not skip fenced code. Plan-side parsing skips
  fences through `section_body_lines(skip_fenced=True)`.
- `src/gobby/tasks/expansion/_common.py:298-310` (`d39e3ecd8838ea3b5c47efec199452c084e1bc98c8ff4c18a02b0f181144cd0d`):
  `_contract_section_body` copies the section body up to `**Acceptance:**`
  verbatim. A compiled leaf's description therefore carries its Targets
  lines, metadata included.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:480-527` (`6cad74c109b465413207d2b29ca3b5a203348b5f9315b307d168ef6277867fd7`):
  - Gate 8 calls `evaluate_task_scope`.
  - It records `task_scope_mismatch` with a `scope_justification` action, or
    a pass.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py:423-436` (`29ac95d9f960bd5ef330fedb0a9ab759cd1a35160670b9ca0a045bd769dc7609`):
  `select_close_candidate` returns `(None, None)` when no commits and no
  `commit_sha` exist. Otherwise it accepts only an explicit linked candidate.
- `_foreign_close_commit` (same file, `382-409`) accepts a commit tagged for
  another task once `link_commit` links it explicitly.
- Line counts at `0b676c6453`:
  - `semantic_lint.py` has 934 lines and `_lifecycle_close.py` has 910, so
    both trip the 850 heuristic.
  - `symbol_targets.py` has 860 and imports `has_generated_header` from
    `semantic_lint.py`. This plan leaves both of those unchanged.
- `.gitattributes` declares no filter or eol conversion for production
  suffixes, only `*.sql text eol=lf`. A working-tree file's raw-byte blob ID
  therefore equals its committed blob ID.

Reproduction of the diagnostic (criterion 1), run at `0b676c6453` against an
isolated temporary project root with the test database
(`DATABASE_URL=...gobby_test GOBBY_TEST_PROTECT=1 uv run python <script>`):

```text
Fixture:  crates/gclient/src/app/mod.rs, 927 lines, first line
          "pub use pane::UNNAMED_PANE;", then 926 "pub fn item_N() {}" lines.
Plan:     one deliverable; Targets entry
          `crates/gclient/src/app/mod.rs::*` — scope-reason: remove the UNNAMED_PANE re-export
          body names only that re-export; nothing is added.
Result:   [('production-size-growth', 'target crates/gclient/src/app/mod.rs has
          927 lines and is already near the 1,000-line production ceiling;
          target a new same-extension file and name the split or move in this
          deliverable. missing split path. Candidate split targets: none; use
          an unambiguous qualified path')]
Control:  same plan with `::*` — operation: delete — scope-reason: retire the file
          returns [] (whole-file deletion exempts the file).
```

## Constraints
`kind: framing`

- The monolith ceiling holds. Hand-maintained production files stay below
  1,000 lines. `semantic_lint.py` and `_lifecycle_close.py` must not grow
  materially, and each owning deliverable moves its new logic into a new
  module.
- Import direction: `src/gobby/plans/production_size.py` imports nothing from
  `semantic_lint.py`, which imports it. The gate module imports both. A
  reverse import is a cycle and fails at import time.
- 0.5.0 keeps no backward compatibility, and no existing plan uses
  `delete-lines`. No shim or alias is added.
- Skill reference files under `src/gobby/install/shared/skills/` are templates
  that sync to the database at daemon start. A doc edit there is live only
  after the next daemon restart, which only the Orchestrator runs.
- Consumer sweeps at `0b676c6453`. `gcode grep -w` covered
  `has_generated_header|PRODUCTION_SIZE_GROWTH_THRESHOLD|PRODUCTION_SIZE_CEILING|_line_count|_is_hand_maintained_production_path|_RUST_TEST_MODULE_RE|_GENERATED_HEADER_RE|_PRODUCTION_SUFFIXES|_NON_PRODUCTION_PARTS`:
  - every hit is inside `semantic_lint.py`, except `has_generated_header`,
    which `symbol_targets.py:19` imports;
  - the unrelated `_line_count` in `monolith_guard.py` and in one compliance
    test is a different function.
  `gcode grep -w '_lint_shared_target_ordering|_lint_production_size_growth|_is_whole_file_deletion' tests`
  returns nothing. `gcode grep -w _evaluate_close src tests` returns
  `_lifecycle_close_tool.py` and the seven test files listed in 2.1.

## P1: Preflight Proof
`kind: framing`

**Goal:** plan validation accepts a typed partial-deletion proof that provably
does not grow a large file, and rejects every other partial-edit claim exactly
as today.

### 1.1 Typed delete-lines proof in plan validation [category: code]
`kind: deliverable`

Targets:
- `src/gobby/plans/production_size.py`
- `src/gobby/plans/semantic_lint.py::PRODUCTION_SIZE_GROWTH_THRESHOLD`
- `src/gobby/plans/semantic_lint.py::PRODUCTION_SIZE_CEILING`
- `src/gobby/plans/semantic_lint.py::_PRODUCTION_SUFFIXES`
- `src/gobby/plans/semantic_lint.py::_NON_PRODUCTION_PARTS`
- `src/gobby/plans/semantic_lint.py::_RUST_TEST_MODULE_RE`
- `src/gobby/plans/semantic_lint.py::_is_hand_maintained_production_path`
- `src/gobby/plans/semantic_lint.py::_line_count`
- `src/gobby/plans/semantic_lint.py::_lint_production_size_growth`
- `src/gobby/plans/semantic_lint.py::_lint_shared_target_ordering`
- `tests/plans/test_production_size.py`
- `tests/plans/test_semantic_lint.py::*` — scope-reason: add the delete-lines proof matrix beside the existing size-growth and ordering tests
- `docs/contracts/plan-coverage.md`
- `src/gobby/install/shared/skills/gobby/references/plan/coverage.md`

Move the production-size primitives out of `src/gobby/plans/semantic_lint.py` into the new module `src/gobby/plans/production_size.py`, and put the typed proof there beside them: the two size constants, the production-path suffix and part rules, the Rust test-module pattern and the production line count all leave `semantic_lint.py`, which keeps only the lint wiring.

**Research context:**
- Current behavior is in As-Is Facts:
  - `_lint_production_size_growth` exempts a 850+ line file only by
    whole-file deletion or a split paragraph;
  - `_line_count` counts universal-newline lines and stops a `.rs` file at
    the first `#[cfg(test)]` line;
  - completed sections skip the lint;
  - the CLI fallback keys on the `production-size-growth` code.
- The 927-line reproduction above is the motivating case.
- Excerpt hashes: `abaaeed6…` (582-637), `f372558d…` (783-794),
  `1cad6dd6…` (551-579), `462be57b…` (299-331).
- `has_generated_header` and `_GENERATED_HEADER_RE` stay in
  `semantic_lint.py`. `symbol_targets.py` imports `has_generated_header`
  there, and that file is 860 lines, so this plan leaves it alone.
- Read-only dependencies: `symbol_targets.parse_target_line` reads a proof
  entry as one `::*` token, because metadata carries no backticks and
  `scope-reason` is last. The CLI fallback in `src/gobby/cli/plans.py` and
  `_completed_plan_sections` in `src/gobby/tasks/expansion/_validate.py`
  are unchanged.
- Rejected: a new lint code, which breaks the CLI fallback, and a
  plan-structure exclusivity rule that bans other owners. The ordering rule
  is enough, and it keeps follow-on edits possible.

Proof contract (the exact rules this leaf implements):
- Grammar. One Targets bullet, with its fields in this fixed order:
  ```text
  - `<path>::*` — operation: delete-lines — base-blob: <40 hex> — lines: <ranges> — scope-reason: <text>
  ```
  - The separator is the em dash with single spaces (` — `).
  - The bullet marker is optional. The regular expression accepts
    `^\s*(?:[-*+]\s+)?`, because the inventory iterator also yields a
    header-form entry (`Targets: <entry>`) without one. Everything after the
    marker is matched exactly. `parse_delete_lines_proof` carries this in its
    own expression, because it cannot import `_BULLET_RE`.
  - `<path>` is a canonical repository-relative POSIX file path. It is
    non-empty and has no leading `/`, no `.` or `..` component, no empty
    component (`//`), and no backslash, whitespace, backtick or colon. The
    target is always `<path>::*`.
  - `base-blob` is 40 lowercase hex characters. The repository object
    format is `sha1` (`git rev-parse --show-object-format`).
  - `<ranges>` is a comma-and-space list of `N` or `A-B` items. Numbers are
    decimal with no leading zero, and `A-B` requires `A < B`. Ranges are
    strictly ascending, with at least one retained line between neighbours,
    and they must leave at least one line.
  - `scope-reason` comes last. Its text is non-empty, with no backtick and
    no further reserved field (` — operation:`, ` — base-blob:`,
    ` — lines:` or ` — scope-reason:`).
  - No key may repeat, be reordered or be omitted, and no other
    `operation:` may appear.
- Candidate rule. Split the entry on `_PRIMARY_TOKEN_SEPARATOR` (` — `) and
  strip the bullet marker from the first segment, as
  `_is_whole_file_deletion` does. The entry is a proof candidate when either
  of these holds:
  - any segment starts with `operation: delete-lines`;
  - a segment after the first, and before the first segment that starts
    with `scope-reason:`, contains `delete-lines`.
  The first segment counts only when it starts with `operation:
  delete-lines`, which no valid target does. A file or symbol named
  `delete-lines` therefore stays an ordinary target, and a plain mention of
  `delete-lines` in scope-reason prose is not a candidate. Every candidate
  must parse. A candidate that does not parse is an error, including one
  whose path cannot be recovered.
- Line model. Lines are the base bytes split after each `\n`. A non-empty
  tail with no final `\n` is the last line, and each line keeps its own
  terminator. The projection concatenates the retained lines unchanged, so
  CRLF endings and the final-newline state survive byte for byte. The base
  must decode as strict UTF-8, and a `\r` not followed by `\n` fails.
- Count rule. Base and projection are both counted with today's production
  rule: universal-newline iteration, where a `.rs` file stops at its first
  `#[cfg(test)]` line. The projected count must be at most the base count
  and below `PRODUCTION_SIZE_CEILING` (1,000). A base at or above 1,000
  passes only when the projection drops below 1,000.

Implementation, new module `src/gobby/plans/production_size.py` (all symbols
new):
- `PRODUCTION_SIZE_GROWTH_THRESHOLD = 850` and `PRODUCTION_SIZE_CEILING =
  1_000`, plus private `_PRODUCTION_SUFFIXES`, `_NON_PRODUCTION_PARTS` and
  `_RUST_TEST_MODULE_RE`, all with today's values.
- `is_production_source_path(file_path: str) -> bool`: today's suffix, part
  and stem rules from `_is_hand_maintained_production_path`, as pure path
  logic.
- `production_line_count(text: str, *, suffix: str) -> int`: iterates
  `io.StringIO(text, newline=None)` and stops at the first `#[cfg(test)]`
  line when `suffix == ".rs"`. This is exactly today's `_line_count` loop.
- `git_blob_id(data: bytes) -> str`: SHA-1 of `b"blob %d\0" % len(data) +
  data`, which equals `git hash-object --no-filters`.
- `DeleteLinesProofError(ValueError)`, and frozen dataclasses
  `DeleteLinesProof(path, base_blob, ranges)` and
  `ProjectedDeletion(data, blob, base_count, projected_count)`.
- `parse_delete_lines_proof(line: str) -> DeleteLinesProof`: one anchored
  regular expression implementing the Proof contract grammar, then three
  validations:
  - the path rule;
  - the scope-reason rule (non-empty, no backtick, no reserved field);
  - range validation: canonical `N` / `A-B` with `A < B`, no leading zeros,
    strictly ascending, with a retained line between neighbours.
  It raises `DeleteLinesProofError` with a specific reason.
- `project_delete_lines(proof, data: bytes) -> ProjectedDeletion`, which
  raises `DeleteLinesProofError` when:
  - `git_blob_id(data) != proof.base_blob` (the message names both IDs);
  - strict UTF-8 decoding fails;
  - a lone `\r` is present;
  - a range falls outside 1..line count;
  - the ranges delete every line;
  - the projected production count is greater than the base count;
  - the projected production count is at least `PRODUCTION_SIZE_CEILING`.
  On success the projection is the concatenation of the retained `\n`-split
  lines.

Implementation in `src/gobby/plans/semantic_lint.py`:
- Import the constants and functions above, and delete the moved
  definitions.
- `_is_hand_maintained_production_path` becomes
  `is_production_source_path(file_path) and source_path.is_file() and not
  has_generated_header(source_path)`.
- `_line_count` keeps its lenient contract: `errors="ignore"` and `OSError`
  counts as 0. It reads the text and returns
  `production_line_count(text, suffix=path.suffix)`.
- New public `is_delete_lines_entry(line: str) -> bool` beside
  `_is_whole_file_deletion` implements the Proof contract candidate rule.
  It reuses `_PRIMARY_TOKEN_SEPARATOR` and `_BULLET_RE`, which stay in
  `semantic_lint.py`. It lives here because `production_size.py` may not
  import `semantic_lint.py`.
- `_lint_production_size_growth` first scans the raw entries from
  `iter_target_block_lines(plan_doc, section)` for lines that satisfy
  `is_delete_lines_entry`, before it iterates the strict inventory. The
  raw scan catches a candidate whose path `_primary_target_path` cannot
  recover, which strict inventory drops. Each candidate runs the proof
  branch. The branch reports one `production-size-growth` issue with
  `details` `file_path` and `proof_error` when any of these hold.
  `file_path` is the parsed path, else `_primary_target_path(line)`, which
  may be `None`.
  - parsing fails;
  - more than one Targets entry has the proof's path as its
    `_primary_target_path`;
  - the path is not hand-maintained production;
  - the path, or any parent below the project root, is a symlink, which the
    branch checks with `source_path.is_symlink()` and by comparing
    `resolve()` against `project_root.resolve() / file_path`;
  - reading raises `OSError`;
  - `project_delete_lines` raises.
  A valid proof satisfies the lint for that file. Either way, the
  strict-inventory loop then skips the threshold and split heuristic for
  every path that has a candidate, whatever the file's size.
- `_lint_shared_target_ordering` collects proof owners (deliverables whose
  Targets block holds a candidate that parses to a proof for a path). For
  every other owner of that path it requires `_has_dependency_path(graph,
  other, proof_owner)`, else it reports `shared-target-ordering` with message
  "section X targets P before the delete-lines proof in section Y binds its
  base".
- New public `iter_description_target_lines(description: str | None) ->
  Iterator[str]` beside `collect_description_target_inventory`. It drops
  fenced lines (the same `_FENCE_RE` toggle `section_body_lines` uses), then
  yields `_iter_inventory_lines(..., _TARGET_LINE_RE)`. This makes leaf-side
  parsing agree with plan-side parsing. `collect_description_target_inventory`
  keeps its current behavior.
- Expected net size: the moved definitions roughly offset the new branch.
  `semantic_lint.py` stays near its current size and well below 1,000.

Docs:
- `docs/contracts/plan-coverage.md`, Target Inventory: after the whole-file
  deletion paragraph, add a "Partial deletion proof" paragraph with the
  fenced grammar, the candidate rule, the line model, the count rule and the
  fail-closed list.
  It also explains how to compute the fields: `git hash-object --no-filters
  <path>` for `base-blob`, 1-based line numbers from the current file.
- Same file, Validator Lints table:
  - the `production-size-growth` row's disposition gains the
    valid-`delete-lines`-proof alternative;
  - the `shared-target-ordering` row gains the proof-owner-first rule;
  - the paragraph beginning "Apart from explicit whole-file deletion" also
    names the proof.
- The skill reference `plan/coverage.md`: the 850-line bullet gains "a pure
  partial deletion can carry a typed `operation: delete-lines` proof instead
  of a split". `_Last verified` is bumped to the landing date.

**Verification:**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/plans/test_production_size.py tests/plans/test_semantic_lint.py tests/plans/test_landed_sections.py tests/plans/test_consumer_coverage.py tests/plans/test_symbol_targets.py -q`,
then `uv run ruff check src/gobby/plans`, `uv run ruff format --check src/gobby/plans`
and `uv run mypy src/gobby/plans`. The existing size-growth tests
(`tests/plans/test_semantic_lint.py:478-1053`) must pass unchanged.

**Granularity:** eleven acceptance items, two production files, one outcome:
plan validation accepts exactly the valid typed deletion proofs. The new
module and the lint wiring cannot split. Only one deliverable can carry the
`semantic_lint.py` split paragraph: a second deliverable would need a second
new same-extension file just to satisfy the heuristic. The docs describe this
grammar and nothing else.

**Acceptance:**

- 1.1.1 - The canonical proof entry parses into path, base blob and ranges,
  both as a bullet and in the header form without a bullet marker.
  Each non-canonical variant raises `DeleteLinesProofError`: missing,
  repeated or reordered key; backticked metadata; uppercase or short hex;
  non-`::*` target; `0`, a leading zero, `N-N`, descending, overlapping or
  adjacent ranges; `scope-reason` not last; a second `operation:`; an en
  dash separator; a missing, absolute, `..`, `./`-prefixed or doubled-slash
  path; an empty scope-reason; a backtick or reserved field after
  `scope-reason:`. test:
  `tests/plans/test_production_size.py::test_parse_delete_lines_proof_grammar`.
- 1.1.2 - Projection is byte-exact: CRLF lines survive, and a retained final
  line without `\n` stays without it. Deleting the last line leaves the new
  last line's own terminator. A stale blob, an out-of-bounds range, a
  delete-every-line range, a lone `\r` and invalid UTF-8 each raise. test:
  `tests/plans/test_production_size.py::test_project_delete_lines_binds_base_bytes`.
- 1.1.3 - The projected count reuses the production rule:
  - deleting a Rust `#[cfg(test)]` line that exposes test lines raises;
  - deleting only test-tail lines passes;
  - a 1,005-line base projected to 990 production lines passes, and the
    same base projected to 1,000 raises;
  - `production_line_count` matches the pre-change `_line_count` on CRLF,
    lone-CR and Rust fixtures.
  test: `tests/plans/test_production_size.py::test_project_delete_lines_recounts_production_lines`.
- 1.1.4 - The 927-line Rust fixture with a valid proof deleting only the
  re-export line produces no `production-size-growth` issue. The same entry
  without the proof still reproduces today's diagnostic. test:
  `tests/plans/test_semantic_lint.py::test_production_size_growth_accepts_delete_lines_proof`.
- 1.1.5 - Each of these reports `production-size-growth` with a
  `proof_error`:
  - mixed: the proof plus a second entry for the path, or the proof plus
    `operation: delete`;
  - ambiguous: a malformed proof, `operation: delete-lines` placed after
    `scope-reason:`, and each candidate whose path cannot be recovered
    (empty backticks before the metadata, `operation: delete-lines` in the
    target slot, an em dash right after the bullet marker);
  - a malformed candidate on a file below 850 lines, and one beside a valid
    split paragraph;
  - a proof on a `tests/` path or a generated file;
  - a proof reached through a symlink.
  These are not candidates for `is_delete_lines_entry`, and each keeps
  today's behavior: an ordinary entry whose scope-reason only mentions
  `delete-lines`, and an ordinary `src/delete-lines.py::*` target. test:
  `tests/plans/test_semantic_lint.py::test_production_size_growth_delete_lines_proof_fails_closed`.
- 1.1.6 - A stale proof fails even beside a valid split paragraph naming a
  new bare-path Target. The same stale proof passes when its section is in
  `completed_section_ids`. test:
  `tests/plans/test_semantic_lint.py::test_production_size_growth_stale_proof_has_no_fallback`.
- 1.1.7 - A deliverable targeting the proof path without depending on the
  proof owner reports `shared-target-ordering`. A dependent deliverable
  passes. test:
  `tests/plans/test_semantic_lint.py::test_shared_target_ordering_requires_proof_owner_first`.
- 1.1.8 - A proof entry yields exactly one strict-inventory path, one
  `collect_target_inventory` path, and one wildcard `SymbolTarget` from
  `parse_target_line` with no issues. test:
  `tests/plans/test_semantic_lint.py::test_delete_lines_proof_entry_is_one_target`.
- 1.1.9 - `iter_description_target_lines` yields a real Targets block's
  proof line and skips a fenced example holding a `Targets:` line and a
  proof bullet. test:
  `tests/plans/test_semantic_lint.py::test_iter_description_target_lines_skips_fenced_blocks`.
- 1.1.10 - The contract documents the grammar, line model, count rule,
  fail-closed list, field computation and proof-owner ordering. behavior:
  "Partial deletion proof" in `docs/contracts/plan-coverage.md`.
- 1.1.11 - The plan coverage reference names the proof as the alternative
  to a split for a pure partial deletion. behavior: "delete-lines" in
  `src/gobby/install/shared/skills/gobby/references/plan/coverage.md`.

## P2: Committed-Result Proof
`kind: framing`

**Goal:** a leaf carrying a delete-lines proof closes only when the close
candidate's file is byte-identical to the declared deletion, and the close
transition adopts only a task row whose gate inputs match the evaluation.

### 2.1 Size-proof check in close gate 8 [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_size_proof_gate.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close`
- `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py`
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py::*` — scope-reason: add the gate-8 size-proof wiring tests on this module's real Git repository fixture
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::CloseEvaluationFingerprint`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: add the proof-edit stale-close test beside the existing fingerprint and commit-close tests
- `docs/contracts/plan-coverage.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`

Split the committed-result check into the new module `src/gobby/mcp_proxy/tools/tasks/_size_proof_gate.py`; `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` gains only the call and the gate-8 result wiring, so the 910-line close module does not take the logic.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py` — no-edit-reason: it calls `_evaluate_close` with unchanged keyword arguments, and the new check runs inside it.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py` — no-edit-reason: it only stores the captured `CloseEvaluationFingerprint` on `CloseEvaluation`.
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` — no-edit-reason: its task descriptions carry no delete-lines entry, so the check returns before any Git call and gate 8 is unchanged.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: it replaces `_evaluate_close` with mocks or runs proof-free descriptions.
- `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py` — no-edit-reason: its task descriptions carry no delete-lines entry, so the gate-8 checklist entries are unchanged.
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py` — no-edit-reason: its task descriptions carry no delete-lines entry, so the check returns before any Git call.
- `tests/tasks/test_close_checklist.py` — no-edit-reason: its task descriptions carry no delete-lines entry, so the gate-8 checklist entries are unchanged.

**Research context:**
- Gate 8 lives in `_evaluate_close` (As-Is Facts, `_lifecycle_close.py:480-527`,
  excerpt `6cad74c1…`). It awaits `evaluate_task_scope`, then either collects
  `task_scope_mismatch` (whose action asks for a `scope_justification`) or
  passes. Commit set and candidate resolve earlier, at about `:360-383`.
  A commit error blocks every later gate.
- The candidate is explicit. `select_close_candidate` returns `(None, None)`
  when nothing is linked and no `commit_sha` is given, and never infers
  HEAD. `_foreign_close_commit` accepts another task's commit once
  `link_commit` links it. That is the `already_implemented` remediation.
- Git access uses `gobby.utils.daemon_git.daemon_git`
  (`src/gobby/utils/daemon_git.py`):
  - `stream_bytes(args, *, cwd, consume, timeout=10.0)` (`:289`) returns a
    `GitResult`. It delivers raw stdout to `consume` in byte chunks before
    it returns. `GitOk.stdout` is a `str` (`:25-33`) and carries no blob
    bytes, so the caller collects the chunks itself. Used for
    `cat-file blob`.
  - `run(args, *, cwd, timeout=10.0)` (`:263`) returns a `GitResult`, and
    only a `GitOk` result's `stdout` is parsed. Used for `ls-tree -z`.
  - A failure comes back as a `GitFailed` or `GitTimeout` value.
  The close path already uses this service in `_lifecycle_close_preview.py`.
- Persistence: `_contract_section_body` (excerpt `d39e3ecd…`) puts the
  section's Targets lines, metadata included, into the leaf description, and
  M1 hashes cover those section bytes. Leaf-side parsing uses 1.1's
  `iter_description_target_lines`, which skips fenced examples. This plan's
  own leaves mention `delete-lines` in prose, so parsing only the Targets
  block is required.
- Proof freshness. The task description is mutable during the bounded
  review, while the candidate commit and the object store are not.
  `commit_close` (`_lifecycle_close_finalization.py:231-481`, imported by
  `_lifecycle_close` as `_commit_close`) fetches a fresh task row (`:248`),
  captures a fresh
  `CloseEvaluationFingerprint` and returns a stale-close response when it
  differs from the evaluated one (`:280-285`). `capture` records
  `validation_criteria` but not the description
  (`_close_evaluation_support.py:70-103`), so a refreshed proof would close
  unchecked today. `fingerprint_differences` (`:121`) iterates the
  dataclass fields, so it names a new field without change. A later window,
  between that comparison and the linker's re-fetch, is closed generically
  by 2.2.
- Read-only in this deliverable: `_lifecycle_close_finalization.py` (fresh
  scope at about `:334`, fingerprint recheck at `:280`; 2.2 edits it later)
  and `_lifecycle_review_gate.py`. The `capture` call sites
  (`_lifecycle_close.py:282`, `:449` and finalization `:280`) keep their
  arguments.
- Rejected:
  - a 14th gate, because `test_close_task_flow.py` pins items 1..13;
  - moving the gate-8 block, because tests patch
    `_lifecycle_close.evaluate_task_scope` in many places;
  - checking at plan validation only, because landing marks the section
    complete before the close;
  - fingerprinting the whole description, which would stale a close on any
    unrelated description edit;
  - re-running the Git check in finalization, because the fingerprint
    already binds the proof lines and the candidate is immutable.

Implementation, new module `src/gobby/mcp_proxy/tools/tasks/_size_proof_gate.py`
(all symbols new):
- `SIZE_PROOF_CLOSE_REASONS = frozenset({"completed", "already_implemented"})`.
- `size_proof_lines(description: str | None) -> tuple[str, ...]`: in order,
  the lines from `iter_description_target_lines(description)` that satisfy
  `is_delete_lines_entry`. The gate and the close fingerprint both use it,
  so they bind identical inputs.
- A frozen dataclass `SizeProofResult(checked_paths: tuple[str, ...],
  failures: tuple[str, ...], action: str | None)`, with property `passed`,
  `message` (the failures joined) and `details()` (`checked_paths`,
  `failures`).
- `async def evaluate_size_proofs(*, description: str | None, reason: str,
  candidate_commit_sha: str | None, repo_path: str) -> SizeProofResult`
  works in this order:
  1. When `reason` is not in `SIZE_PROOF_CLOSE_REASONS`, return a passing
     empty result.
  2. Take `size_proof_lines(description)`. When it is empty, return a
     passing empty result with no Git call.
  3. Parse each with `parse_delete_lines_proof`. A parse error is a failure.
  4. When `candidate_commit_sha` is None, fail with the action "link the
     commit that delivered the deletion with link_commit and pass it as
     commit_sha".
  5. For each proof, collect the base bytes in a fresh per-call
     `bytearray` with `await daemon_git.stream_bytes(("cat-file", "blob",
     base_blob), cwd=repo_path, consume=buffer.extend, timeout=10.0)`. A
     `GitFailed`, a `GitTimeout` or an ordinary exception is a failure
     ("base blob not readable from the object store", plus the available
     diagnostic), and the partial bytes are discarded.
  6. Run `project_delete_lines(proof, bytes(buffer))`. Its errors are
     failures.
  7. Await `daemon_git.run(("--literal-pathspecs", "ls-tree", "-z",
     "--full-tree", candidate, "--", path), cwd=repo_path, timeout=10.0)`.
     A non-`GitOk` result, an ordinary exception, a malformed record or a
     record count other than one is a failure. Otherwise the one record
     must have exactly this path, type `blob`, mode `100644` or `100755` and
     an object ID equal to `ProjectedDeletion.blob`. Anything else is a
     failure naming expected and found mode, type and object ID.
  Every failure lands in the returned `SizeProofResult`. Only cancellation
  escapes the function. The mismatch action names the remediation:
  - make the candidate's file equal the declared deletion;
  - if the base moved, refresh `base-blob` and `lines` in the canonical plan
    and revalidate it, re-derive M1 through the stale-manifest route, and
    have the Orchestrator update the leaf's Targets entry to the refreshed
    line.
- In `_evaluate_close`, await `evaluate_size_proofs(description=task.description,
  reason=reason, candidate_commit_sha=evaluation.candidate_commit_sha,
  repo_path=repo_path)` before the scope `try`, so a scope evaluation error
  never skips it. Gate 8 then records exactly one item-8 `task_scope`
  result. This matters because `collect_failure` appends a gate entry on
  every call (`_lifecycle_close_preview.py:182-208`).
  - When `size_proofs.passed`, the existing branches run unchanged:
    `task_scope_unavailable`, `task_scope_mismatch` or `pass_gate`.
  - Otherwise a single `evaluation.collect_failure(8, "task_scope",
    "size_proof_mismatch", size_proofs.message, ...)` records the failure:
    - `reasons` holds the proof failures plus any simultaneous scope
      diagnostic (the unavailable message or the scope mismatch message);
    - `actions` holds the proof action, plus the scope action when scope
      also failed;
    - `details` is `{"size_proofs": size_proofs.details(), "scope":
      scope.details() if scope else None}`;
    - `extra=scope.details()` is passed when scope also mismatched, as the
      existing mismatch branch does.
  - When a proof failed, the `except RuntimeError` branch keeps its message
    for that combined failure instead of recording its own entry. The scope
    snapshot, `scope_justification` and advisory-drift assignments are
    unchanged.
  - `scope_justification` never cures a proof failure. A deliberate close
    of an escalated task waives only gate 13 and the TDD check
    (`_lifecycle_close.py:128`, `:732`), so it never waives gate 8 either.
  The new import sits beside the existing `_task_scope` imports. The module
  grows by about 25 lines and stays below 940.

Implementation in `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py`
(763 lines):
- `CloseEvaluationFingerprint` gains the field `size_proof_lines:
  tuple[str, ...]`, and `capture` sets it to
  `size_proof_lines(task.description)`. The import runs one way:
  `_close_evaluation_support` imports `_size_proof_gate`, which imports
  only `gobby.plans` and `gobby.utils` modules. Nothing under `gobby.plans`
  imports `mcp_proxy`.
- With no call-site change, finalization's existing recheck then returns
  the stale-close response when the proof lines changed after evaluation,
  and `fingerprint_differences` names `size_proof_lines`. A description edit
  outside the proof lines leaves the fingerprint equal.

Docs:
- `docs/contracts/plan-coverage.md`: after the 1.1 "Partial deletion proof"
  paragraph, add a "Committed result" paragraph covering:
  - close gate 8 checks each proof against the explicit close candidate for
    `completed` and `already_implemented`;
  - byte equality is checked through blob IDs, and only a regular-file entry
    passes;
  - `scope_justification` never cures a mismatch;
  - a proof edited after evaluation stales the close;
  - the base-drift remediation: refresh `base-blob` and `lines` in the
    canonical plan and revalidate it, re-derive M1 through the
    stale-manifest route, and have the Orchestrator update the leaf's
    Targets entry to the refreshed line.
- The skill reference `tasks/closing.md`: after the `task_scope_mismatch`
  sentence, add one sentence on `size_proof_mismatch` and its remediation.
  `_Last verified` is bumped.

**Verification:**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/tasks/test_size_proof_gate.py tests/mcp_proxy/tools/tasks/test_close_candidate.py tests/mcp_proxy/tools/tasks/test_close_task_flow.py tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py tests/mcp_proxy/tools/tasks/test_task_scope.py tests/tasks/test_close_checklist.py -q`,
then `uv run ruff check src/gobby/mcp_proxy/tools/tasks`,
`uv run ruff format --check src/gobby/mcp_proxy/tools/tasks` and
`uv run mypy src/gobby/mcp_proxy/tools/tasks`. Gate tests build real
temporary Git repositories, as `test_close_candidate.py::candidate_repo`
does.

**Granularity:** nine acceptance items, three production files, one
outcome: the close gate enforces the declared result. The wiring is the
call and the single gate-8 result in `_evaluate_close`, plus one
fingerprint field, and it is tested with the module. Splitting off the
fingerprint field would leave a gate whose proof can change unchecked
before the close. The docs describe only this check.

**Acceptance:**

- 2.1.1 - With a candidate whose file equals base minus the declared lines,
  the gate passes. With a candidate that also adds a line, it fails. Across
  two linked commits, the net result at the candidate decides: an addition
  removed again by the later candidate passes, and stopping at the earlier
  commit fails. Two valid proofs for distinct paths pass only when both
  candidate files match, and one mismatch fails. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_candidate_must_equal_projected_deletion`.
- 2.1.2 - A candidate that renames or removes the path, replaces it with a
  symlink, or turns it into a gitlink fails with expected and found entry
  details. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_candidate_entry_must_be_regular_file`.
- 2.1.3 - Each of these fails closed with a diagnostic `SizeProofResult`,
  never an exception:
  - a base object missing from the store;
  - a malformed proof line in the description;
  - a proof-bearing `completed` or `already_implemented` close with no
    candidate;
  - a failed or timed-out Git call;
  - malformed `ls-tree` output.
  Chunked base reads keep CRLF lines and a final line without `\n` byte for
  byte. A path holding a pathspec metacharacter (`[`) matches only itself.
  test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_unbindable_proof_fails_closed`.
- 2.1.4 - `wont_fix`, `obsolete` and `duplicate` closes, and descriptions
  with no Targets-block proof (including a fenced example and an ordinary
  `src/delete-lines.py::*` target), pass without any `daemon_git` call.
  test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_size_proofs_skip_without_obligation`.
- 2.1.5 - A leaf description built by `_contract_section_body` from a plan
  section with a proof yields, through `iter_description_target_lines` and
  `parse_delete_lines_proof`, the same `DeleteLinesProof` as plan-side
  parsing. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_compiled_leaf_description_retains_proof`.
- 2.1.6 - Through `_evaluate_close`, a mismatched candidate fails gate 8
  (`task_scope`) with `size_proof_mismatch` even when `scope_justification`
  is supplied. For both `completed` and `already_implemented`, the exact
  deletion passes gate 8 and an added line fails it. Each of these yields
  exactly one item-8 `task_scope` entry with `size_proof_mismatch` and
  keeps any scope diagnostic:
  - a proof mismatch together with a scope mismatch;
  - a proof mismatch while `evaluate_task_scope` raises `RuntimeError`;
  - a proof mismatch on a deliberate close of an escalated task.
  test:
  `tests/mcp_proxy/tools/tasks/test_close_candidate.py::test_size_proof_mismatch_fails_task_scope_gate`.
- 2.1.7 - A proof edited after evaluation stales the close. When the fresh
  row from the first fetch (`_lifecycle_close_finalization.py:248`) changes
  only the proof's `base-blob` and `lines` for the same path,
  `_commit_close` returns the stale-close response, the task stays open,
  and `fingerprint_differences` names `size_proof_lines`. This holds for an
  ordinary close and for a deliberate close of an escalated task. A fresh
  row whose description changes outside the proof lines still closes.
  test:
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_size_proof_edit_after_evaluation_stales_close`.
- 2.1.8 - The contract documents the committed-result check, its close
  reasons, regular-file rule, proof freshness and base-drift remediation.
  behavior: "Committed result" in `docs/contracts/plan-coverage.md`.
- 2.1.9 - The closing reference explains `size_proof_mismatch` and that a
  scope justification cannot cure it. behavior: "size_proof_mismatch" in
  `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`.

### 2.2 Recheck close gate inputs on the linked row [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::commit_close`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::CloseEvaluationFingerprint`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: add the linked-row fingerprint recheck tests beside the existing commit-close tests

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py` — no-edit-reason: it only stores the captured `CloseEvaluationFingerprint` on `CloseEvaluation`.
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` — no-edit-reason: it runs `_evaluate_close` then `commit_close` on a real harness with no concurrent row edit, so the linked row's fingerprint equals the evaluated one and the close proceeds as today.

**Research context:**
- Found by Adv2 during this plan's review. The Orchestrator placed it here
  at 19:44 CT on 2026-10-05 so that ownership stays with one plan. It is
  generic: any gate input changed in the window is adopted unchecked today,
  not only 2.1's proof lines.
- The race. Adv2's `gcode evidence` excerpts are each complete with no
  warnings:
  - `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py:231-305`
    (`6b94c6105da81a39e465b277bc6023b44cff419ee2a1b933630e46085d0c2829`):
    `commit_close` fetches a fresh row (`:248`), then captures and compares
    a fresh `CloseEvaluationFingerprint` (`:280-291`).
  - Same file `:305-413`
    (`ff6047cab49144a582443dae7a969442c8788cc4f073d9b700b2b13472cb998e`): it
    rechecks scope (`:349`), then calls `link_close_commit_shas` with
    `task=fresh` and adopts the returned `linked` row (`:377-383`).
  - Same file `:413-481`
    (`145726706434669abc42b441e1dd3ba6a27e75ce784c1694e22c75779baa0ff5`):
    `close_task` runs with `expected_updated_at=linked.updated_at` (`:423`).
  - `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py:474-508`
    (`aedae5351da6e763bd732e5ebd73f63e62ba66b0cf81dc84ecefc1e4d80b7d73`):
    `link_close_commit_shas` re-fetches the row (`:502`) and returns it
    (`:508`), even when every candidate is already linked (`:486-487`).
  - `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py:1-122`
    (`598cbbbfdab8610c7898c5f7eed5096da04e5079024c7b5245a53064d6b13b9c`):
    the fingerprint fields and `capture` (`:70-103`).
  - A gate input such as `validation_criteria`, or 2.1's
    `size_proof_lines`, that changes between the comparison at `:285` and
    the linker's fetch at `:502` is therefore adopted by the
    compare-and-set without a check. A change after `:502` fails the
    compare-and-set with `TaskStaleStateError`.
- Adv2 reproduced it with a read-only probe: an already-linked candidate and
  a fake manager whose row changed only description and version. The helper
  returned the changed row with no error.
- Linking changes no fingerprint field. `link_commit` adds to
  `task.commits`, which the fingerprint does not hold. Child state and
  attribution are inputs to `capture`, not read from the row.
- Adv2's F-LINKED-SCOPE-FRESHNESS (the Orchestrator placed it in 2.2 at
  20:02 CT under the same ruling): the window also adopts a declared-Targets
  edit. Adv2's excerpts are each complete with no warnings:
  - `src/gobby/mcp_proxy/tools/tasks/_task_scope.py:148-193`
    (`65fbd945274ac4d7aa2d2c85761831e15ae9c637ea26ad720b3ce5a69dfafb6d`):
    `_collect_task_scopes` adds `collect_declared_task_targets(task.description)`
    (`:161`), which normalizes each `collect_description_target_inventory`
    entry to its path (`:185`).
  - `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py:70-103`
    (`05c7036aba6ae4323521784d40e1973afde2e9ea4d146ad616f42f297cbee3dd`): the
    fingerprint fields (`:74-82`) hold no description-derived value.
  - `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py:305-430`
    (`c73ac41926dd9d5644258ec813051ac9318394e394b04d9ddf891f0c520d7522`): the
    scope recheck (`:349`) runs before the linked-row adoption (`:377-383`)
    and the compare-and-set (`:423`).
  - Adv2's read-only probe built two rows whose descriptions differ only in
    Targets (`src/a.py`, then `src/b.py`). `collect_declared_task_targets`
    returned `['src/a.py']` and `['src/b.py']`, while
    `CloseEvaluationFingerprint.capture` returned equal fingerprints. Neither
    row has a proof, so 2.1's `size_proof_lines` is `()` in both.
- `_close_evaluation_support.py` already imports `collect_commit_paths_async`
  from `_task_scope` (`:22`), and `_task_scope` imports nothing from it, so
  the new import adds no cycle. Every fingerprint is built through `capture`
  (`_lifecycle_close.py:282,449`, `_lifecycle_close_finalization.py:280`,
  and the tests), so a new field needs no other constructor change.
- Affected-file annotations, the other declared-scope source (`:152-159`),
  live in their own table. The row compare-and-set never adopted them, and
  Adv2 bounded this finding to the row; this deliverable leaves them alone.
- `_lifecycle_close_finalization.py` has 539 lines and
  `_close_evaluation_support.py` has 763. The 850 heuristic applies to
  neither.
- Rejected:
  - a proof-only recheck, which leaves the window open for every other gate
    input;
  - a second fetch or a row lock. The fingerprint comparison on the row the
    compare-and-set uses is enough;
  - a new lock framework or a linked-row scope re-evaluation (Decision
    Record item 10).

Implementation in `CloseEvaluationFingerprint`
(`src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py`):
- Add the field `declared_targets: tuple[str, ...]` after 2.1's
  `size_proof_lines`. `capture` sets it to
  `tuple(sorted(collect_declared_task_targets(task.description)))`, imported
  beside `collect_commit_paths_async` at `:22`.
- The values are the normalized paths scope already uses. A Targets entry's
  symbol, bullet or scope-reason can change without changing them.
- `fingerprint_differences` names `declared_targets` with no change of its
  own, because it iterates the dataclass fields.

Implementation in `commit_close`
(`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py`):
- Extract the existing comparison at `:285-291` into a module-private
  helper `_gate_inputs_changed(evaluation, fingerprint) -> dict[str, Any] |
  None`. It records `evaluation.extra["changed_gate_inputs"]` and returns
  today's `stale_close_response(...)` with the same message ("Task gate
  inputs changed after evaluation (...); retry close_task."), or `None`.
- Call it at `:285` with `fresh_fingerprint`. The new field means a
  declared-Targets path edit made before the first fetch (`:248`) now stales
  there and names `declared_targets`. Before this change, only the scope
  recheck at `:349` caught it, and only when the scope result changed.
  Every other behavior at `:285` is unchanged.
- Call it again right after the `link_error` check, before
  `determine_close_outcome`, with `CloseEvaluationFingerprint.capture(linked,
  children_state=fresh_children_state, attribution=fresh_attribution)`.
  This reuses the child and attribution state captured for the first
  comparison, and a non-`None` result is returned at once.
- The finalization module grows by about 10 lines and the support module
  by about 3.

**Verification:**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/tasks/test_close_task_flow.py tests/mcp_proxy/tools/tasks/test_close_candidate.py tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py -q`,
then `uv run ruff check src/gobby/mcp_proxy/tools/tasks`,
`uv run ruff format --check src/gobby/mcp_proxy/tools/tasks` and
`uv run mypy src/gobby/mcp_proxy/tools/tasks`.

**Granularity:** four acceptance items, two production files, one outcome:
the close transition adopts only a row whose gate inputs match the
evaluation. The guard is independent of 2.1's Git check. It depends on 2.1
because its test exercises the `size_proof_lines` field and its new field
follows that one in `CloseEvaluationFingerprint`.

**Acceptance:**

- 2.2.1 - The close is refused when `validation_criteria` changes between
  the first fingerprint comparison and the linker's fetch. `_commit_close`
  returns the stale-close response, `changed_gate_inputs` names
  `validation_criteria`, and `close_task` is not called. test:
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_linked_row_gate_input_change_stales_close`.
- 2.2.2 - The close is likewise refused when, in the same window, only the
  proof's `base-blob` and `lines` change, both for an ordinary close and for
  a deliberate close of an escalated task. `changed_gate_inputs` names
  `size_proof_lines`. test:
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_linked_row_proof_edit_stales_close`.
- 2.2.3 - With no concurrent edit, both of these close: a close that links
  a new candidate, and one whose candidate is already linked. A benign
  bookkeeping change on the linked row (`updated_at`, `path_cache`) also
  still closes, as does a linked-row description edit that changes only
  prose and a Targets entry's scope-reason while keeping its path. test:
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_linked_row_recheck_keeps_benign_closes`.
- 2.2.4 - With no proof in either description, the close is refused when,
  between the first fingerprint comparison and the linker's fetch, only the
  declared Targets change from `src/a.py` to `src/b.py`. `_commit_close`
  returns the stale-close response, `changed_gate_inputs` is exactly
  `["declared_targets"]`, and `close_task` is not called. The same edit made
  before the first fetch stales at the first comparison with the same
  `changed_gate_inputs`. test:
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_linked_row_targets_edit_stales_close`.

## Rollout
`kind: framing`

- 1.1, 2.1 and 2.2 land through the Merge Manager in order.
- After 2.2 lands, the Orchestrator schedules a daemon restart with the usual
  global notice. It makes the close gate, the linked-row recheck and both
  skill references live.
- Until DAEMON BACK after that restart, no plan may author a `delete-lines`
  proof. 1.1 alone would accept a proof whose committed result nothing yet
  checks.
- No data migration. No existing plan uses the grammar.

## V2: Verification
`kind: verification`

- All three leaves' focused pytest, ruff and mypy commands pass, as listed
  in each deliverable.
- A rerun of the As-Is reproduction with a valid proof deleting line 1
  returns no issue. The proof-free entry still returns the 927-line
  diagnostic.
- `uv run gobby plans validate .gobby/plans/production-size-partial-deletion-proof.md -p /Users/josh/Projects/gobby`
  exits 0 before handoff.
- After the restart, a preview close on a scratch proof-bearing task in an
  isolated test repository reports `size_proof_mismatch` for a padded
  candidate.
