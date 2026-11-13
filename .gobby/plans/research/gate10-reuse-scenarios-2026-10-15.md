# Gate 10 reuse: six scenarios

Orchestrator gobby#15977, 2026-10-15 19:05 CDT. This describes the gate as it stands today (`src/gobby/tasks/close_checklist.py`, `criterion_commands.py::first_invalidating_edit`, `close_test_coverage.py`) and what reuse would change. Nothing here is built yet.

## How Gate 10 works today

- **Evidence.** The gate reads commands and exit codes from the session transcripts. A seat's message never counts. Runs through a wrapper (such as rtk) and runs with no exit code earn no credit.
- **Staleness.** Any later task edit makes every earlier test run stale. Only lint-style runs with bounded inputs (one ruff command, for example) survive an edit in a different language. Edits to YAML, JSON or files the code index doesn't know make everything stale.
- **What must pass.** Each changed test file needs a passing pytest run that names it. Each changed source file needs its related tests to pass. Changed tests also need a type audit.
- **Runs in another copy.** A run in an export or another worktree counts only if the test and every changed file are byte-identical to the candidate commit.
- **Results.** The latest result per category wins. A later clean run clears an earlier failure.

## The proposed reuse key

Each test module gets its own key:

- **The module itself:** its own file contents.
- **What it imports:** the contents of every file it imports, followed through the whole chain, taken from the code index.
- **Its conftest files:** every conftest.py above it.
- **The environment:** `uv.lock`, `pyproject.toml`, the Python version, the migrations directory and the bundled templates under `src/gobby/install/shared/`.

The target file's contents alone are never enough, and neither is an identical whole tree. A test module's earlier pass keeps its credit only while its key is unchanged.

## The six scenarios

### 1. A one-line edit to a target file after a green packet

- **Today:** every test in the packet goes stale and the whole packet reruns. #24426 ran six packets, about 1,000 tests.
- **With reuse:** only the test modules whose imports include the edited file rerun. The rest keep credit because their keys are unchanged.
- **Where quality could slip:** a test that reaches the file without importing it, for example by starting a subprocess, reading the file, or scanning the `src/` tree.
- **Prevention:** a test module that starts subprocesses or reads repository files is marked as having unbounded inputs and always reruns. Edits to data files or unindexed files make everything stale, as they do today.

### 2. An edit to a shared helper or a conftest imported by many tests

- **Today:** the whole packet reruns.
- **With reuse:** the helper or conftest is in nearly every key, so nearly everything reruns.
- **Where quality could slip:** nowhere new. The saving is near zero, and that is the correct result.

### 3. A rebase onto a new 0.5.0 tip

- **Today:** the preview on the merged tree decides. Tonight's #24426 merge needed one 2-file packet (`tests/tasks/test_acceptance_rust_modules.py` and `tests/tasks/test_grok_background_evidence.py`) instead of six packets.
- **With reuse:** keys are recomputed at the new candidate. Test modules whose imports changed upstream rerun, and the rest keep credit.
- **Where quality could slip:** an upstream change outside the import chain, such as a rule YAML file, a migration or an installed template.
- **Prevention:** those changes are in the environment part of the key, or they are data files that make everything stale.

### 4. A change to the lockfile, a dependency or config

- **Today:** everything reruns.
- **With reuse:** the environment part of the key changes, so everything reruns.
- **Where quality could slip:** nowhere. The test-hub address and timeouts are pinned in the packet command itself.

### 5. A flaky test that passed once

- **Today:** one clean run after the last edit earns credit. A later failure in the same category blocks the close until a clean rerun.
- **With reuse:** the latest result under the same key wins. Any later failure under that key cancels the reused pass.
- **Where quality could slip:** a lucky pass is carried into more closes.
- **Prevention:**
  - A test module that has both passed and failed under the same key is never reused.
  - The gate details show every reused pass with its original run time and session, so reviewers can see it.
- **What reuse does not do:** it does not detect flakiness any better than today's gate does.

### 6. A seat-reported run versus a daemon-recorded run (tamper resistance)

- **Today:** evidence comes from session transcripts, never from a seat's message.
- **Why it matters more with reuse:** one record would be worth more, because it covers more closes.
- **Design:** reuse credits only runs the daemon recorded itself, with the daemon computing the keys when the run happened. It never accepts a key or result a seat supplies. Wrapped runs stay uncredited. The Security Reviewer reviews the design before it lands.
- **Pairing with option 2:** with the bounded-run tool, the daemon runs the command itself and records the result and keys directly. That is the strongest version.

## Hard requirement: rtk (Josh, 19:07 CDT)

Reuse, and the bounded-run tool it builds on, must credit rtk-wrapped and rtk-compacted runs fully. No seat ever reruns without rtk just to satisfy a gate. The gate code already strips the `rtk` prefix and reads `rtk recall` output, but nothing proves that end to end yet. #24535 (P1, Lane 1) proves parity for every close gate and fixes whatever breaks. Until it lands, Release Manager packets keep RTK_DISABLED=1, and once it lands that requirement comes off.

## Recommendation

1. Build reuse on top of the bounded-run tool (option 2), so that every reused run is one the daemon recorded itself.
2. Start conservative: tests with unbounded inputs always rerun, and data-file edits make everything stale.
3. Measure the savings on real closes before widening the rules.

Quality could slip in three places: imports the code index can't see, flaky tests, and tampered records. The three controls above cover them. The main savings come in scenarios 1 and 3, the most common cases.
