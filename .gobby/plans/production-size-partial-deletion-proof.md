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

Anything the grammar cannot express exactly fails closed: mixed add/delete
edits, in-file moves, renames, malformed or stale proofs. Those keep today's
split requirement. Whole-file deletion (`operation: delete`) is unchanged and
stays a separate path.

This plan does not depend on the Chrome plan or its workaround. It does not
touch gclient. The pane-accessor split that plan already carries stays as it
is.

Ownership: the Python daemon implements both leaves. The Orchestrator routes
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
2. **Grammar.** One Targets bullet carries the proof, in this fixed order:
   ```text
   - `<path>::*` — operation: delete-lines — base-blob: <40 hex> — lines: <ranges> — scope-reason: <text>
   ```
   - The separator is the em dash with single spaces (` — `).
   - `base-blob` is 40 lowercase hex characters. The repository object format
     is `sha1` (`git rev-parse --show-object-format`).
   - `<ranges>` is a comma-and-space list of `N` or `A-B` items. Numbers are
     decimal with no leading zero, and `A-B` requires `A < B`. Ranges are
     strictly ascending, with at least one retained line between neighbours.
     They must leave at least one line; deleting every line is whole-file
     deletion.
   - `scope-reason` comes last, because `symbol_targets.parse_target_line`
     consumes everything after it.
   - Metadata carries no backticks. A second backticked span would parse as
     another target token.
   - The target is always `::*`, because the committed check constrains the
     whole file's bytes.
   One anchored regular expression matches the entry. Any Targets entry whose
   text before `scope-reason:` contains `delete-lines` is a proof candidate,
   and a candidate the expression does not match is an error. No key may
   repeat, be reordered or be omitted, and no other `operation:` may appear.
3. **Exclusivity.**
   - A proof is the only Targets entry for its path in its deliverable.
   - Every other deliverable that targets the same path must transitively
     depend on the proof owner. An earlier editor would make the base stale
     before the proof leaf starts.
   - A proof entry never satisfies whole-file deletion
     (`_is_whole_file_deletion` requires exactly `operation: delete`). An
     entry carrying both operations fails the proof grammar.
4. **Line model and counting.**
   - Lines are the base bytes split after each `\n`. A non-empty tail with no
     final `\n` is the last line, and a line keeps its own terminator.
   - The projection concatenates the retained lines unchanged, so CRLF endings
     and the final-newline state of retained lines survive byte for byte.
   - The base must decode as strict UTF-8. A `\r` not followed by `\n` fails
     the proof, so the `\n` line model and the universal-newline count agree.
   - Base and projection are both counted with the existing production rule:
     universal-newline iteration, where a Rust file stops at its first
     `#[cfg(test)]` line.
   - The projected production count must be at most the base count and below
     1,000. Deleting a `#[cfg(test)]` line that exposes test lines therefore
     fails, and so does a base already at or above 1,000.
   - Deleting only Rust test-tail lines passes: the file shrinks and the
     production count does not grow.
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
8. **Where the gate runs.**
   - The close path is the only enforcement point. `_task_has_landed_commit`
     marks a section complete as soon as a tagged commit reaches HEAD, before
     the close, and plan lint then skips it.
   - The gate-order test pins items 1..13 with unique names, so the check
     joins gate 8 rather than becoming a 14th gate.
   - The gate-8 block stays in `_evaluate_close`, because many tests patch
     `_lifecycle_close.evaluate_task_scope`. The new logic lives in a new
     module, and `_evaluate_close` gains only the call and the result wiring.
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
    - Re-checking the proof in finalization or in the review gate is not
      needed. Every close call re-runs `_evaluate_close`, and the candidate
      and object store are immutable within a call.

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
- `is_delete_lines_entry(line: str) -> bool`: true when the text before any
  `scope-reason:` contains `delete-lines`.
- `parse_delete_lines_proof(line: str) -> DeleteLinesProof`: one anchored
  regular expression implementing Decision Record item 2, then range
  validation: canonical `N` / `A-B` with `A < B`, no leading zeros, strictly
  ascending, with a retained line between neighbours. It raises
  `DeleteLinesProofError` with a specific reason.
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
- `_lint_production_size_growth` checks each strict-inventory path before
  the production-path filter. It collects that file's Targets-block entries
  (as `_is_whole_file_deletion` does) and runs the proof branch when any
  entry satisfies `is_delete_lines_entry`. The branch reports one
  `production-size-growth` issue with `details` `file_path` and
  `proof_error` when any of these hold:
  - there is more than one entry for the path;
  - parsing fails;
  - the path is not hand-maintained production;
  - the path, or any parent below the project root, is a symlink, which the
    branch checks with `source_path.is_symlink()` and by comparing
    `resolve()` against `project_root.resolve() / file_path`;
  - reading raises `OSError`;
  - `project_delete_lines` raises.
  A valid proof satisfies the lint for that file. Either way the branch then
  skips the threshold and split heuristic for the file.
- `_lint_shared_target_ordering` collects proof owners (deliverables whose
  Targets block holds a `delete-lines` entry for a path). For every other
  owner of that path it requires `_has_dependency_path(graph, other,
  proof_owner)`, else it reports `shared-target-ordering` with message
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
  fenced grammar, the line model, the count rule and the fail-closed list.
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

- 1.1.1 - The canonical proof entry parses into path, base blob and ranges.
  Each non-canonical variant raises `DeleteLinesProofError`: missing,
  repeated or reordered key; backticked metadata; uppercase or short hex;
  non-`::*` target; `0`, a leading zero, `N-N`, descending, overlapping or
  adjacent ranges; `scope-reason` not last; a second `operation:`; an en
  dash separator. test:
  `tests/plans/test_production_size.py::test_parse_delete_lines_proof_grammar`.
- 1.1.2 - Projection is byte-exact: CRLF lines survive, and a retained final
  line without `\n` stays without it. Deleting the last line leaves the new
  last line's own terminator. A stale blob, an out-of-bounds range, a
  delete-every-line range, a lone `\r` and invalid UTF-8 each raise. test:
  `tests/plans/test_production_size.py::test_project_delete_lines_binds_base_bytes`.
- 1.1.3 - The projected count reuses the production rule:
  - deleting a Rust `#[cfg(test)]` line that exposes test lines raises;
  - deleting only test-tail lines passes;
  - a base at or above 1,000 production lines raises even after deletion;
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
  - ambiguous: a malformed proof;
  - a proof on a `tests/` path or a generated file;
  - a proof reached through a symlink.
  test: `tests/plans/test_semantic_lint.py::test_production_size_growth_delete_lines_proof_fails_closed`.
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
candidate's file is byte-identical to the declared deletion.

### 2.1 Size-proof check in close gate 8 [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_size_proof_gate.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close`
- `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py`
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py::*` — scope-reason: add the gate-8 size-proof wiring tests on this module's real Git repository fixture
- `docs/contracts/plan-coverage.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`

Split the committed-result check into the new module `src/gobby/mcp_proxy/tools/tasks/_size_proof_gate.py`; `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` gains only the call and the gate-8 result wiring, so the 910-line close module does not take the logic.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py` — no-edit-reason: it calls `_evaluate_close` with unchanged keyword arguments, and the new check runs inside it.
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` — no-edit-reason: its task descriptions carry no delete-lines entry, so the check returns before any Git call and gate 8 is unchanged.
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py` — no-edit-reason: its task descriptions carry no delete-lines entry, so the check returns before any Git call and gate 8 is unchanged.
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
- Git access uses `gobby.utils.daemon_git.daemon_git`:
  - `stream_bytes(args, cwd=..., consume=..., timeout=...)` returns raw
    stdout bytes, for `cat-file blob`;
  - `run(args, cwd=..., timeout=...)` returns `GitOk` text, for
    `ls-tree -z`.
  The close path already uses this service in `_lifecycle_close_preview.py`.
- Persistence: `_contract_section_body` (excerpt `d39e3ecd…`) puts the
  section's Targets lines, metadata included, into the leaf description, and
  M1 hashes cover those section bytes. Leaf-side parsing uses 1.1's
  `iter_description_target_lines`, which skips fenced examples. This plan's
  own leaves mention `delete-lines` in prose, so parsing only the Targets
  block is required.
- Read-only, no change: `_lifecycle_close_finalization.py` (fresh scope at
  about `:334`) and `_lifecycle_review_gate.py`. Every close call re-runs
  `_evaluate_close`. Within a call the candidate commit and the object store
  are immutable, so the gate is deterministic and a changed proof or
  candidate is rechecked on the next call.
- Rejected:
  - a 14th gate, because `test_close_task_flow.py` pins items 1..13;
  - moving the gate-8 block, because tests patch
    `_lifecycle_close.evaluate_task_scope` in many places;
  - checking at plan validation only, because landing marks the section
    complete before the close.

Implementation, new module `src/gobby/mcp_proxy/tools/tasks/_size_proof_gate.py`
(all symbols new):
- `SIZE_PROOF_CLOSE_REASONS = frozenset({"completed", "already_implemented"})`.
- A frozen dataclass `SizeProofResult(checked_paths: tuple[str, ...],
  failures: tuple[str, ...], action: str | None)`, with property `passed`,
  `message` (the failures joined) and `details()` (`checked_paths`,
  `failures`).
- `async def evaluate_size_proofs(*, description: str | None, reason: str,
  candidate_commit_sha: str | None, repo_path: str) -> SizeProofResult`
  works in this order:
  1. When `reason` is not in `SIZE_PROOF_CLOSE_REASONS`, return a passing
     empty result.
  2. Collect lines from `iter_description_target_lines(description)` that
     satisfy `is_delete_lines_entry`. When there are none, return a passing
     empty result with no Git call.
  3. Parse each with `parse_delete_lines_proof`. A parse error is a failure.
  4. When `candidate_commit_sha` is None, fail with the action "link the
     commit that delivered the deletion with link_commit and pass it as
     commit_sha".
  5. For each proof, `stream_bytes(("cat-file", "blob", base_blob))`. A
     non-`GitOk` result is a failure: "base blob not in the object store".
  6. Run `project_delete_lines` on those bytes. Its errors are failures.
  7. Run `daemon_git.run(("ls-tree", "-z", "--full-tree", candidate, "--",
     path))`. Exactly one record must have this path, type `blob`, mode
     `100644` or `100755` and object ID equal to `ProjectedDeletion.blob`.
     Otherwise it is a failure naming expected and found mode, type and
     object ID.
  The mismatch action names the remediation: make the candidate's file
  equal the declared deletion; if the base moved, follow Decision Record
  item 9.
- In `_evaluate_close`, after the `try` that sets `scope` and before
  `if scope is not None:`, await `evaluate_size_proofs(description=task.description,
  reason=reason, candidate_commit_sha=evaluation.candidate_commit_sha,
  repo_path=repo_path)`. The gate-8 `pass_gate` branch becomes
  `elif size_proofs.passed:`. After the scope block, when not
  `size_proofs.passed`, call `evaluation.collect_failure(8, "task_scope",
  "size_proof_mismatch", size_proofs.message, action=size_proofs.action,
  details=size_proofs.details())`. The new import sits beside the existing
  `_task_scope` imports. The module grows by about 15 lines.

Docs:
- `docs/contracts/plan-coverage.md`: after the 1.1 "Partial deletion proof"
  paragraph, add a "Committed result" paragraph covering:
  - close gate 8 checks each proof against the explicit close candidate for
    `completed` and `already_implemented`;
  - byte equality is checked through blob IDs, and only a regular-file entry
    passes;
  - `scope_justification` never cures a mismatch;
  - the base-drift remediation of Decision Record item 9.
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

**Granularity:** eight acceptance items, two production files, one outcome:
the close gate enforces the declared result. The wiring is three statements
in `_evaluate_close` and is tested with the module. The docs describe only
this check.

**Acceptance:**

- 2.1.1 - With a candidate whose file equals base minus the declared lines,
  the gate passes. With a candidate that also adds a line, it fails. Across
  two linked commits, the net result at the candidate decides: an addition
  removed again by the later candidate passes, and stopping at the earlier
  commit fails. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_candidate_must_equal_projected_deletion`.
- 2.1.2 - A candidate that renames or removes the path, replaces it with a
  symlink, or turns it into a gitlink fails with expected and found entry
  details. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_candidate_entry_must_be_regular_file`.
- 2.1.3 - Each of these fails closed: a base object missing from the store,
  a malformed proof line in the description, and a proof-bearing
  `completed` or `already_implemented` close with no candidate. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_unbindable_proof_fails_closed`.
- 2.1.4 - `wont_fix`, `obsolete` and `duplicate` closes, and descriptions
  with no Targets-block proof (including a fenced example), pass without
  any `daemon_git` call. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_size_proofs_skip_without_obligation`.
- 2.1.5 - A leaf description built by `_contract_section_body` from a plan
  section with a proof yields, through `iter_description_target_lines` and
  `parse_delete_lines_proof`, the same `DeleteLinesProof` as plan-side
  parsing. test:
  `tests/mcp_proxy/tools/tasks/test_size_proof_gate.py::test_compiled_leaf_description_retains_proof`.
- 2.1.6 - Through `_evaluate_close`, a mismatched candidate fails gate 8
  (`task_scope`) with `size_proof_mismatch` even when `scope_justification`
  is supplied. The exact deletion passes gate 8. test:
  `tests/mcp_proxy/tools/tasks/test_close_candidate.py::test_size_proof_mismatch_fails_task_scope_gate`.
- 2.1.7 - The contract documents the committed-result check, its close
  reasons, regular-file rule and base-drift remediation. behavior:
  "Committed result" in `docs/contracts/plan-coverage.md`.
- 2.1.8 - The closing reference explains `size_proof_mismatch` and that a
  scope justification cannot cure it. behavior: "size_proof_mismatch" in
  `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`.

## Rollout
`kind: framing`

- 1.1 and 2.1 land through the Merge Manager in order.
- After 2.1 lands, the Orchestrator schedules a daemon restart with the usual
  global notice. It makes the close gate and both skill references live.
- Until DAEMON BACK after that restart, no plan may author a `delete-lines`
  proof. 1.1 alone would accept a proof whose committed result nothing yet
  checks.
- No data migration. No existing plan uses the grammar.

## V2: Verification
`kind: verification`

- Both leaves' focused pytest, ruff and mypy commands pass, as listed in each
  deliverable.
- A rerun of the As-Is reproduction with a valid proof deleting line 1
  returns no issue. The proof-free entry still returns the 927-line
  diagnostic.
- `uv run gobby plans validate .gobby/plans/production-size-partial-deletion-proof.md -p /Users/josh/Projects/gobby`
  exits 0 before handoff.
- After the restart, a preview close on a scratch proof-bearing task in an
  isolated test repository reports `size_proof_mismatch` for a padded
  candidate.
