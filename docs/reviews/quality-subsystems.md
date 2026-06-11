# Review: quality subsystems (review_learning + project_verification + test_quality + voice)

- **Scope:** `src/gobby/review_learning/`, `src/gobby/project_verification/`, `src/gobby/test_quality/`, `src/gobby/voice/` (~5.7k lines)
- **Reviewer:** Claude Fable 5 — 6-agent fan-out (candidates/synthesis, review_learning service, review_learning lessons/promotion, test_quality, project_verification evidence/refresh, voice); every Blocker independently re-verified against source by the synthesizer
- **Commit / branch:** `b4d0248e3` / `0.5.0`
- **Summary:** 4 Blocker · 36 Important · 35 Nit — three of the four subsystems quietly mis-handle untrusted or scale inputs: auto-derived verification commands become shell-executed RCE, the learning ladder rests on a storage filter it never actually gets, and the voice path leaks orphaned inference threads past every lock. test_quality is the strongest but leaks gate-blocking false positives in one direction and silent false negatives in the other.

## Findings

### [BLOCKER] Evidence-derived commands with shell metacharacters pass `is_safe_validation_command`, get auto-written, then run under `shell=True`

- **Where:** `src/gobby/project_verification/candidates.py:146-162` (`is_safe_validation_command`), `:82-88` (`generate_candidates` final filter, called with **no slot** arg), `:379-381` (`_package_script_command` quotes `subdir` but not `script`) → written to `.gobby/project.json` by `refresh.py:186-209` → executed at `src/gobby/hooks/verification_runner.py:64-97` (`run_command` → `subprocess.run(command, shell=True, cwd=<repo>, env=<full user env minus VIRTUAL_ENV>)`)
- **Failure mode:** `is_safe_validation_command` rejects only mutating *options* (`--fix`/`--write`) and a few formatter forms — nothing about shell control operators. A command lifted verbatim from repo content (a `.github/workflows/*.yml` `run:` step, a Makefile/Justfile/Taskfile recipe, an inline doc code block) such as `pytest && id > /tmp/pwn` is classified `unit_tests`, returns `is_safe=True`, and becomes a `CommandCandidate`. CI evidence carries confidence 0.82 (`evidence.py:301`) vs the deterministic manifest pytest at 0.58, so the **untrusted command is preferred** over the safe one. The reviewer reproduced classification + selection live; the synthesizer verified each hop: `generate_candidates:88` filters with no slot (so even the `prettier --check` guard is dead), `is_safe_validation_command:146-162` has no operator check, `_package_script_command:381` leaves `script` unquoted, and `verification_runner.py:92` runs `shell=True` in the repo dir with the user's inherited environment (tokens/credentials). `gobby init` re-run auto-writes this with `fix=True` and no user review (`src/gobby/utils/project_init.py:170`).
- **Why it matters:** This shifts the trust boundary for verification commands from user-authored to auto-derived-from-repo-content, then auto-writes and shell-executes them with full ambient credentials. Cloning/refreshing a hostile repo yields arbitrary command execution at commit time. The function literally named `is_safe_validation_command` reports safe while the contract is violated.
- **Minimal fix:** Reject candidates whose command contains shell control/expansion tokens (`;`, `&&`, `||`, `|`, backtick, `$(`, `>`, `<`, `&`, newlines) unless they match a curated `cd <quoted-subdir> && <allowlisted-tool>` shape; quote `script` in `_package_script_command`; validate that the leading executable is in an allowlist rather than merely "classifiable."
- **Confidence:** high — full chain (classifier → auto-write → `shell=True` with repo cwd + user env) verified in source by the synthesizer; medium on real-world exploitability (requires verification hooks enabled).

### [BLOCKER] CI `run:` backslash continuations become truncated commands that win selection and overwrite user commands

- **Where:** `src/gobby/project_verification/evidence.py:407-419` (`_split_run_commands`), `:301` (CI confidence 0.82); downstream auto-write at `src/gobby/project_verification/refresh.py:85-87` and re-init at `src/gobby/utils/project_init.py:170` (`fix=True`)
- **Failure mode:** `_split_run_commands` iterates `run.splitlines()`, `.strip()`s each, and appends each physical line as an independent command (verified in source — no continuation joining). A continuation like `uv run pytest \` / `--cov=gobby tests/unit` yields the command `uv run pytest \` — trailing backslash, arguments gone. `_looks_like_command` passes it, and `is_safe_validation_command` passes it because `shlex.split` raises ValueError on the lone backslash and `_command_tokens` falls back to `str.split` (`candidates.py:165-169`); `classify_command` maps it to `unit_tests` at 0.82, beating a short existing user command (0.74). The reviewer reproduced it end-to-end: a temp repo with `verification:{"unit_tests":"pytest -x"}` plus such a workflow produced `project.json` containing `"unit_tests": "uv run pytest \\"` with `written:True`. Multi-line `if`/`for` blocks and here-docs are mangled identically.
- **Why it matters:** Backslash continuation is one of the most common GitHub-workflow idioms. Refresh `--fix` and re-init silently replace a working user command with a malformed one whose dropped flags change what validation actually runs — success reported while the "produce runnable commands" contract is violated.
- **Minimal fix:** In `_split_run_commands`, join backslash-continued physical lines into one logical command before filtering, skip clear fragments of compound constructs, and reject any candidate whose command ends with `\`.
- **Confidence:** high — source-verified (`splitlines()` + per-line append, no join); reviewer reproduced the bad write live.

### [BLOCKER] Barge-in cancellation releases the synthesis lock while inference still runs, corrupting the `inference_turbo` monkeypatch

- **Where:** `src/gobby/voice/tts_chatterbox.py:345-374` (`synthesize_stream`: `async with self._synthesis_lock` wraps `await asyncio.to_thread(self._generate_with_token_cap, ...)`), `:249-287` (`_generate_with_token_cap`: `getattr(turbo_decoder, "inference_turbo")` save, patch, `finally`-restore); triggers: `src/gobby/servers/websocket/voice/tts.py:107-110` (`cancel()` mid-`to_thread`), `src/gobby/servers/websocket/voice/mixin.py:148-153` (fire-and-forget `existing.cancel()` then immediately builds a new pipeline on the same provider singleton), `:176-196` ("barge-in from VAD")
- **Failure mode:** `asyncio.to_thread` is not cancellable. On barge-in the awaiting coroutine is cancelled, `CancelledError` unwinds the `async with self._synthesis_lock` and **releases the lock**, but the inference thread keeps running `_generate_with_token_cap`. A new pipeline (created immediately at `mixin.py:153`) acquires the now-free lock and starts a second `model.generate` concurrently on the same non-thread-safe torch model with shared `model.conds` state. Worse, the new call's `getattr(turbo_decoder, "inference_turbo")` captures the *old call's* `_capped_inference_turbo` wrapper as its "original"; when the orphaned thread's `finally` fires it strips the new call's cap mid-generation, and the new call's `finally` then permanently installs the stale wrapper. The synthesizer verified both the lock/`to_thread` structure and the save/restore in `_generate_with_token_cap`. The warmup-priming path (`tts_chatterbox.py:205,293-296`, cap=8) can similarly leave an 8-token cap permanently installed — all subsequent speech truncated to ~8 tokens until reload.
- **Why it matters:** Barge-in is a designed, normal-use feature; the consequence is concurrent forward passes on a non-thread-safe model (garbage audio or crash) plus a silently wrong token cap that persists for the model's lifetime. The existing test (`tests/voice/test_tts_chatterbox.py:554-567`) only asserts CancelledError propagation; nothing covers cancel-then-resynthesize.
- **Minimal fix:** Guard inference with a `threading.Lock` acquired inside the worker thread (so a new generate blocks until the orphaned one finishes), and stop per-call monkeypatching — pass `max_gen_len` through `model.generate(..., max_gen_len=token_cap)` if upstream supports it, or patch once at load time.
- **Confidence:** high (mechanism — lock release + non-cancellable thread + save/restore corruption all source-verified), medium (real-world frequency).

### [BLOCKER] Promotion/dedupe rely on `tags_all` as an exact filter; storage only tag-filters a `limit*3` newest-rows window

- **Where:** `src/gobby/review_learning/promotion.py:129-135` (occurrence count via `alist_memories(limit=500, tags_all=[...])`), `src/gobby/review_learning/service.py:215-220` (duplicate guard via `alist_memories(limit=1, tags_all=[occurrence_tag])`); storage at `src/gobby/storage/memories.py:422-470` (`fetch_limit = limit * 3 if tags... else limit`, `ORDER BY updated_at DESC LIMIT %s`, `_filter_by_tags` applied in Python, `return memories[:limit]`)
- **Failure mode:** The synthesizer confirmed `list_memories` fetches only `limit*3` newest rows, then tag-filters in Python. The dedupe guard (`limit=1`) therefore scans only the **3 newest** pattern memories project-wide — it misses any duplicate occurrence as soon as ≥3 newer pattern memories exist, so re-recording the same finding creates duplicate memories and re-runs promotion. For occurrence counting, once a project accumulates >1500 pattern memories, older occurrences fall out of the window: `_count_occurrences` (`promotion.py:334-340`) shrinks, and `_create_or_update_task` (`:213-226`) rewrites an existing guardrail task's title/target/labels **downward** (e.g. `target:validation` demoted to `target:test`), or `resolve_promotion` returns no task at all. There is no pruning of pattern memories anywhere (`git grep "expire|prune|decay|ttl" src/gobby/memory/` finds only search-time temporal scoring), so growth is guaranteed.
- **Why it matters:** The module's core contract — monotonic, deterministic promotion thresholds and idempotent occurrence recording — silently degrades with normal data growth. Test fakes (`tests/review_learning/conftest.py:90-96`) implement `tags_all` as a true whole-list filter, which is exactly why the suite passes while production drifts.
- **Minimal fix:** Push tag filtering into SQL (Postgres array/JSONB containment) so `tags_all` is exact regardless of limit; or at minimum paginate until exhaustion in the occurrence-count and dedupe paths.
- **Confidence:** high — all three layers source-verified; the divergence between the storage window and the consumers' exact-filter assumption is confirmed.

---

### [IMPORTANT] Go-only projects populate zero standard verification slots — every Go command shoved into `custom`

- **Where:** `src/gobby/project_verification/candidates.py:64` (`primary_claimed = bool(bundle.python or bundle.has_cargo or bundle.has_go_mod)`), `:72-74` (`_go_candidates(custom=primary_claimed, ...)`)
- **Failure mode:** `primary_claimed` is True if *any* primary language is present — including Go itself — so for a pure-Go repo `_go_candidates` runs with `custom=True` and renames everything to `go_test`/`go_vet`/`go_build`, leaving `unit_tests`/`lint`/`build` empty (reviewer reproduced). Cargo's branch accidentally self-corrects by re-checking `bundle.python` (`:69`); Go's uses raw `primary_claimed`. A hook stage configured for `unit_tests`/`lint`/`build` finds no command and silently no-ops for the whole language.
- **Minimal fix:** Mirror cargo: `_go_candidates(custom=bool(bundle.python) or bundle.has_cargo, ...)`, or start `primary_claimed` False and set it incrementally. Add a `go.mod`-only test.
- **Confidence:** high (reproduced).

### [IMPORTANT] Python candidates hardcode `uv run` and Gobby-internal conventions regardless of the project's toolchain

- **Where:** `src/gobby/project_verification/candidates.py:238` (`GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v`), `:244-257`
- **Failure mode:** Every detected Python project gets `uv run ...` with no detection of poetry/pdm/pip/hatch; a poetry-managed repo without uv gets commands that fail to run. `GOBBY_TEST_PROTECT=1` (a Gobby-only env var) and the hardcoded `tests/` path leak into every third-party project's `project.json`. With `fix=True` these overwrite whatever the user had.
- **Minimal fix:** Detect the package manager from lockfiles (`uv.lock`/`poetry.lock`/`pdm.lock`) and emit the matching runner; drop `GOBBY_TEST_PROTECT=1` for non-Gobby projects.
- **Confidence:** medium.

### [IMPORTANT] Refresh confidence model overwrites deliberate short user commands; re-init applies it silently

- **Where:** `src/gobby/project_verification/candidates.py:433-441` (existing non-generic short = 0.74/0.76) vs `evidence.py:301` (CI = 0.82); tie-break dominated by confidence (`candidates.py:444-449`); auto-applied at `src/gobby/utils/project_init.py:170` (`fix=True`)
- **Failure mode:** A user who sets `unit_tests:"pytest -x"` (2 tokens, not in GENERIC_EXISTING_COMMANDS → 0.74) has it replaced by whatever CI runs (0.82) on the next `gobby init` — no prompt, no flag. Tests bless replacing *generic* and preserving *rich* commands, but short-and-deliberate is uncovered collateral.
- **Minimal fix:** Floor any existing slot command at ≥ CI confidence unless it's generic, or make re-init pass `fix=False`/require explicit opt-in to overwrite a populated slot.
- **Confidence:** medium.

### [IMPORTANT] Corrupt project.json: evidence layer silently skips it, then `_write_verification` crashes with raw JSONDecodeError on `--fix`

- **Where:** `src/gobby/project_verification/evidence.py:161-164` (tolerates corrupt JSON) vs `src/gobby/project_verification/refresh.py:189-191` (unguarded `json.loads(read_text())`)
- **Failure mode:** With a broken `.gobby/project.json`, `before` is `{}`, `changed` is True, the preview says "Run with --fix to write changes," then `--fix` (or re-init) crashes with an unhandled `json.JSONDecodeError` inside `_write_verification` (reproduced). The preview actively recommends the crashing action.
- **Minimal fix:** Wrap the read/parse in `_write_verification` with `try/except (json.JSONDecodeError, OSError)`; treat non-parseable/non-dict as `{}` after backing up.
- **Confidence:** high (reproduced).

### [IMPORTANT] Valid-but-non-object project.json or package.json crashes `collect_evidence` (and `gobby init`)

- **Where:** `src/gobby/project_verification/evidence.py:165` (`data.get("verification")` on non-dict), `:236` (`data.get("scripts", {})` on non-dict)
- **Failure mode:** `json.loads` returning a list/str isn't caught by the `(JSONDecodeError, OSError)` handlers, so `.get` raises AttributeError (both reproduced: `project.json=[]`, `package.json=["..."]`). `initialize_project` calls `detect_verification_commands` unguarded, so one malformed package.json crashes `gobby init`. The YAML collectors guard with `isinstance(data, dict)`; JSON doesn't.
- **Minimal fix:** `if not isinstance(data, dict): return/continue` after both `json.loads` calls.
- **Confidence:** high (reproduced).

### [IMPORTANT] 64 KB read truncation silently erases existing verification, then `--fix` clobbers user commands

- **Where:** `src/gobby/project_verification/evidence.py:533-534` (`_read_text` truncates at `MAX_FILE_BYTES`), `:162` (truncated JSON → JSONDecodeError → skip); asymmetric full read at `refresh.py:190`
- **Failure mode:** `_read_text` truncates every file at 64 KB, turning a *valid* oversized project.json into a parse failure that's silently swallowed; `before` becomes `{}`, the user's rich command never enters selection, and `_write_verification` (full read) replaces the verification block (reproduced with a 70 KB project.json). Oversized pyproject/workflow/Taskfile evidence is dropped the same way.
- **Minimal fix:** Detect truncation (file size > MAX_FILE_BYTES) and skip-with-warning for structured files; treat unreadable existing verification as a hard stop for `fix=True`.
- **Confidence:** high (reproduced).

### [IMPORTANT] `has_mypy_config` is always True for any parseable pyproject.toml

- **Where:** `src/gobby/project_verification/evidence.py:207` (`mypy = tool.get("mypy", {})`), `:216` (`isinstance(mypy, dict)`)
- **Failure mode:** The `{}` default is a dict, so `has_mypy_config` is True even with no `[tool.mypy]` (reproduced). The flag feeds AI synthesis (`asdict(self.python)`), skewing it toward mypy commands on projects without mypy.
- **Minimal fix:** `mypy = tool.get("mypy")`; adjust `mypy_strict` to guard on `isinstance(mypy, dict)`.
- **Confidence:** high (reproduced).

### [IMPORTANT] Unguarded `_read_text` in the Makefile branch crashes `collect_evidence`

- **Where:** `src/gobby/project_verification/evidence.py:312` (`_parse_indented_recipes(_read_text(path))`)
- **Failure mode:** Every sibling collector wraps `_read_text` in OSError handling; the Makefile/justfile loop does not. A directory named `Makefile` (reproduced: `IsADirectoryError`) or a permission-denied Makefile crashes init and refresh.
- **Minimal fix:** `try/except OSError: continue`, like the Taskfile branch below it.
- **Confidence:** high (reproduced).

### [IMPORTANT] CLI `resolve_refresh_root` treats cwd as root and its error message routes users into the split-project bug

- **Where:** `src/gobby/cli/projects.py:31-47` (`Path.cwd().resolve()`, no walk-up; remediation hint at `:45`: `Run 'gobby init -C {root}'`)
- **Failure mode:** Run from any subdir of an initialized project, the refresh CLI errors — and tells the user to `gobby init -C <subdir>`, which is exactly the action that plants the id-less subdir project.json and splits the repo into two projects (the filed Blocker in `docs/reviews/support-infra.md`).
- **Minimal fix:** Resolve via `find_project_root(Path.cwd())` first; only suggest `gobby init` when no ancestor has project.json.
- **Confidence:** high.

### [IMPORTANT] Fingerprint identity is positionally ambiguous — distinct findings collide, lessons silently dropped

- **Where:** `src/gobby/review_learning/fingerprint.py:33-49` (consumed at `service.py:199-200,215-231`)
- **Failure mode:** `derive_finding_fingerprint` joins only non-empty field values with `|`, no labels, no empty-slot preservation. Reviewer verified empirically: `{'rule_id':'mutable-default-arg'}` and `{'principle':'mutable-default-arg'}` produce identical fingerprints; `("rev:1","fp")` == `("rev","1:fp")` in `build_occurrence_key`. On collision, `record()` returns `duplicate_occurrence` and a genuinely distinct lesson is never stored. The dict-fallback hashes `str(finding)`, so key-order differences make equivalent findings dedup-miss.
- **Minimal fix:** Encode field labels and keep empty slots; use a non-ambiguous join (length-prefix or hash parts separately). Add collision tests.
- **Confidence:** high.

### [IMPORTANT] record→promote partial failure permanently strands the threshold crossing

- **Where:** `src/gobby/review_learning/service.py:247-262` (create_memory then promote_lesson, no atomicity), `:215-231` (retry short-circuits with `duplicate_occurrence`, never re-attempts promotion); `src/gobby/review_learning/promotion.py:119-157` (no idempotent re-drive). Flagged independently by both review_learning reviewers.
- **Failure mode:** If `promote_lesson` raises after the occurrence memory is committed (task-DB transient, daemon restart), the guardrail task is never created; re-calling `record` returns `duplicate_occurrence` and skips promotion. For high-risk lessons the guardrail task is simply never created until a completely new occurrence arrives — which then skips the intended tier.
- **Minimal fix:** On the `duplicate_occurrence` path, still call `promote_lesson` (it recounts and create-or-updates idempotently), or persist a promotion-pending marker.
- **Confidence:** high.

### [IMPORTANT] Check-then-act races: concurrent records create duplicate memories and duplicate guardrail tasks

- **Where:** `src/gobby/review_learning/service.py:215-231` → `:247` (existence check then create_memory); `src/gobby/review_learning/promotion.py:194,228,242-257` (`_find_existing_task` then `create_task`, no lock/transaction/unique constraint). Flagged by both review_learning reviewers.
- **Failure mode:** No lock spans the dedupe-read and write. Two concurrent `record()` for the same occurrence both pass the preflight and both create; two records of the same *pattern* both see no existing guardrail task and create two tasks. Gobby explicitly runs many concurrent agents.
- **Minimal fix:** Per-`pattern_key` advisory lock (or a partial unique index on open guardrail tasks per pattern label) around find-or-create.
- **Confidence:** medium (mechanism certain; needs concurrent same-pattern records).

### [IMPORTANT] Sync DB I/O on the event loop in `_resolve_scope`

- **Where:** `src/gobby/review_learning/service.py:316-327`, called from async `recall_context`/`recall_review_lessons_for_files`/`record` (`:101,142,198`); sync callees `src/gobby/storage/session_resolution.py:61-78`, `src/gobby/storage/hub/postgres.py:189`
- **Failure mode:** Every recall/record does 1-3 blocking Postgres round-trips directly on the MCP server's event loop; `recall_review_lessons_for_files` sits on the blocking `before_tool` hook path (`inject-review-lessons-for-touched-files.yaml:27` `background:false`). The package knows the convention — promotion offloads via `asyncio.to_thread`, memory search via `_run_storage` — `_resolve_scope` is the inconsistent spot.
- **Minimal fix:** Wrap the resolution body in `await asyncio.to_thread(...)`.
- **Confidence:** high.

### [IMPORTANT] `recall_context` has no bound on findings — unbounded search fan-out and response size

- **Where:** `src/gobby/review_learning/service.py:105-129,281-288` (up to 4 hybrid searches per finding); schema `src/gobby/mcp_proxy/tools/review_learning.py:35-44` (no `maxItems`)
- **Failure mode:** A 200-finding review triggers up to 800 sequential vector+keyword searches and unbounded `flat_matches` growth in the MCP response. Every other limit in the service is clamped; findings count is the one unguarded input.
- **Minimal fix:** Clamp findings to a documented cap and cap `flat_matches`.
- **Confidence:** high (mechanism), medium (frequency).

### [IMPORTANT] `record()` silently falls back to wrong project scope when session resolution fails

- **Where:** `src/gobby/review_learning/service.py:329-336` (debug-level swallow), `:429-433` (PERSONAL_PROJECT_ID fallback), write path `:198`
- **Failure mode:** If a caller-supplied `session_id` fails to resolve, `_resolve_scope` logs at debug and returns the contextvar project or `PERSONAL_PROJECT_ID`. For *record* the durable lesson is written into the wrong project (often the personal bucket in daemon context where `get_project_context` returns None), and project-scoped recall never finds it — yet the result reports `success:True`.
- **Minimal fix:** In `record()`, raise on caller-supplied-`session_id` resolution failure (or include a `scope_warning`); keep fail-soft for recall only.
- **Confidence:** medium.

### [IMPORTANT] Worktree-recorded paths never match main-checkout paths; tag fast path dead for absolute paths

- **Where:** `src/gobby/review_learning/file_paths.py:29-59` (no repo-root relativization; `path_tag` hashes the verbatim path; `paths_match` suffix rule); consumers `service.py:345-368` (tag-exact then 200-cap fallback scan); touched paths arrive as raw absolute tool-input paths (`src/gobby/hooks/_normalization_canonical.py:60-62`)
- **Failure mode:** Reviewer verified empirically: `paths_match('/Users/josh/.gobby/worktrees/.../foo.py', '/Users/josh/Projects/gobby/.../foo.py')` → False, and `path_tag(absolute) != path_tag(relative)`. A lesson recorded with an absolute worktree path (review agents run in worktree isolation) is unreachable from the main checkout. The headline "inject lessons before touching matching files" degrades to a 200-newest suffix scan and goes fully blind for abs-vs-abs cross-checkout pairs.
- **Minimal fix:** Relativize against the repo/worktree root in `normalize_lesson_file_path` before tagging/matching.
- **Confidence:** high.

### [IMPORTANT] Suffix matching produces wrong-file matches, including paths outside the repo

- **Where:** `src/gobby/review_learning/file_paths.py:59` (`endswith(f"/{...}")` both directions), `:42-43` (strips `./` not `../`)
- **Failure mode:** Reviewer verified: `paths_match('src/foo.py','vendor/pkg/src/foo.py')` → True (two distinct files); `normalize_lesson_file_path('../other/foo.py')` suffix-matches in-repo `other/foo.py`. Wrong guidance is injected into agent context pre-edit; the rule fires on every matching edit.
- **Minimal fix:** After repo-relativization (above), require exact equality; until then reject `..` segments and require the shorter side ≥2 components.
- **Confidence:** high (behavior), medium (frequency).

### [IMPORTANT] One task per pattern across decisions: a no-fix-policy promotion rewrites/demotes a confirmed guardrail task

- **Where:** `src/gobby/review_learning/promotion.py:242-257` (`_find_existing_task` matches only `pattern:{key}` + group labels — decision/target ignored), `:213-226` (update rewrites title/category/target/validation_criteria), `:225` (clears `implementation_domain` via explicit `None` → SET NULL at `src/gobby/storage/tasks/_updates.py:158`)
- **Failure mode:** A pattern with 3 confirmed occurrences (`target:validation`, category `code`, domain `backend`) is updated by two later `no-fix-policy` occurrences to `target:checklist`, category `docs`, clearing `implementation_domain` — the stronger code guardrail is silently demoted and human edits to the domain are clobbered to NULL.
- **Minimal fix:** Scope `_find_existing_task` by decision, or refuse to downgrade target/category on update; pass `implementation_domain` only when setting a value.
- **Confidence:** high (mechanics), medium (intent).

### [IMPORTANT] Confirmed ladder silently discards explicit `guardrail_target="tool-config"`

- **Where:** `src/gobby/review_learning/promotion.py:160-176` (`_confirmed_target` honors only `{helper,checklist}` at occ 2, `{rule,workflow,pipeline}` at 3+, `{...,validation}` high-risk); the enum admits `tool-config` (`src/gobby/review_learning/lessons.py:22-31`, validated `:109-114`)
- **Failure mode:** A confirmed finding that explicitly requests a `tool-config` guardrail (valid, validated) is always rewritten to `test`/`validation` — a code-category task instead of the requested config change. Only the no-fix-policy branch honors `tool-config`.
- **Minimal fix:** Add `tool-config` to the honored sets at occurrence ≥2, or reject it for confirmed lessons at validation time.
- **Confidence:** medium.

### [IMPORTANT] Memory-dream consolidation can delete the occurrence evidence the promotion ladder depends on

- **Where:** `src/gobby/memory/dream/candidates.py:59-80` (stale scan has no `review-lesson`/`pattern` exclusion — `git grep "review-lesson" src/gobby/memory/dream/` finds nothing), `src/gobby/memory/dream/apply.py:100` (delete); promotion counts live solely in those memories' tags (`promotion.py:334-340`)
- **Failure mode:** Running `gobby memory dream` can classify >30-day-old pattern memories for merge/delete; occurrence counts drop, the demotion behavior above triggers, and task "Evidence memory IDs" dangle. Review-learning assumes pattern memories are immutable evidence; nothing marks them protected.
- **Minimal fix:** Exclude `review-lesson`-tagged memories from dream candidate discovery, or store occurrence counts durably on the task.
- **Confidence:** medium.

### [IMPORTANT] One unparseable or non-UTF-8 .py file crashes the entire test-quality audit

- **Where:** `src/gobby/test_quality/analyzer.py:147` (`read_text(encoding="utf-8")`), `:155` (`ast.parse`), `:125-126` (per-file loop, no try/except); CLI `src/gobby/test_quality/cli.py:62` propagates
- **Failure mode:** Reproduced: a dir containing `def broken(:` raises SyntaxError out of `audit_paths`; a latin-1 file raises UnicodeDecodeError. `_discover_files` includes every `.py` (conftest, fixtures), so one bad file aborts the run with a traceback. The audit gates pushes (`pre-push-test.sh:253-254`) and is run by agents on arbitrary repos; the `AuditWarning` channel exists but isn't used for parse failures.
- **Minimal fix:** Wrap per-file analysis; on `SyntaxError`/`UnicodeDecodeError`/`OSError` emit a `PARSE_ERROR` warning and continue.
- **Confidence:** high (reproduced).

### [IMPORTANT] Suppression and TODO spans leak across sibling tests in decorated classes (gate bypass)

- **Where:** `src/gobby/test_quality/analyzer.py:255-257` (`start_line = min([node.lineno, *decorator_lines])` over decorators including inherited class decorators), consumed by `_suppressed_codes`/`_todo_lines` (`:391-416`)
- **Failure mode:** Reproduced both directions. With `@pytest.mark.usefixtures(...)` on a Test class, every method's span starts at the class decorator line, covering earlier siblings: a `# test-quality: allow NO_ASSERTION` in `test_one` silently suppresses `test_two`'s genuine `NO_ASSERTION` (high), so `--fail-on-new` passes; a `# TODO` in `test_one` is reported against both.
- **Minimal fix:** Compute the comment-scan span from `min(node.lineno, *own_decorator_lines)` only; keep inherited class decorators solely for skip/xfail checks.
- **Confidence:** high (reproduced).

### [IMPORTANT] Bare `@pytest.mark.xfail` escapes XFAIL_WITHOUT_STRICT_OR_REASON

- **Where:** `src/gobby/test_quality/analyzer.py:430-431` (`if not isinstance(decorator, ast.Call): return False`)
- **Failure mode:** Reproduced: `@pytest.mark.xfail` with no args (the worst variant — neither strict nor reason) isn't flagged, while `@pytest.mark.xfail(reason="...")` (strictly better) is flagged high. Stripping the reason makes the finding disappear — inverted incentive on a gate-relevant check.
- **Minimal fix:** For Name/Attribute decorators named `xfail`, return True.
- **Confidence:** high (reproduced).

### [IMPORTANT] JS/TS delimiter scanner desyncs on apostrophes in comments — tests silently vanish

- **Where:** `src/gobby/test_quality/analyzer.py:770-820` (`_find_matching_delimiter` treats any quote outside a string as an opener; no `//`/`/* */`/regex awareness), used by `_iter_script_tests` (`:746-767`)
- **Failure mode:** Reproduced: a file with `it("first", () => { // don't flake here ... });` plus a second `it(...)` reports `tests_scanned=1` — `first` disappears (the apostrophe opens an unterminated quote, the test is skipped). Any test containing "don't"/"can't" in a comment is silently excluded; a weak/assertion-free test passes the gate unexamined.
- **Minimal fix:** Skip `//...\n` and `/*...*/` (and ideally regex literals) when not inside a string.
- **Confidence:** high (reproduced).

### [IMPORTANT] `regex.test(...)` creates phantom JS tests with false high-severity NO_ASSERTION

- **Where:** `src/gobby/test_quality/analyzer.py:46` (`_SCRIPT_TEST_CALL_RE` has no preceding-`.` guard), consumed `:746-767`
- **Failure mode:** Reproduced: `const valid = /\d+/.test("123");` at module scope yields a phantom test `123` flagged `NO_ASSERTION` (high). `RegExp.prototype.test` is ubiquitous; a false new high fails the `--fail-on-new --min-severity high` runs agents must produce as evidence.
- **Minimal fix:** Add negative lookbehind `(?<![.\w$])` to the test-call regex.
- **Confidence:** high (reproduced).

### [IMPORTANT] Zero-file audits exit 0; explicitly passed supported files silently ignored

- **Where:** `src/gobby/test_quality/analyzer.py:166-199` (`_discover_files` silently drops explicit files failing `_is_analyzable_file`; nonexistent paths fall through), `src/gobby/test_quality/cli.py:61` (default `Path("tests")` unvalidated)
- **Failure mode:** Reproduced: passing an existing `.ts` whose name lacks `.test.`/`.spec.` → `files_scanned:0`, exit 0; a missing `tests` dir → empty report, exit 0. With `--baseline --fail-on-new` these pass green. Task #14648 history records `gobby test-quality audit` exiting 0 with "Files scanned: 0" submitted as TDD evidence.
- **Minimal fix:** Emit a warning (or nonzero exit under `--fail-on-new`) when an explicit path yields no analyzable files, and when `files_scanned == 0`.
- **Confidence:** high (reproduced + recorded incident).

### [IMPORTANT] `from pytest import raises` → false NO_ASSERTION (high)

- **Where:** `src/gobby/test_quality/analyzer.py:461-467` (`_is_strong_assertion_call` matches only fully-qualified `pytest.raises`/`warns`/`deprecated_call`)
- **Failure mode:** Reproduced: `from pytest import raises; with raises(ValueError): ...` → `NO_ASSERTION` (high). Idiomatic pytest produces a gate-failing false positive; the escape hatch is a suppression comment, training people to suppress.
- **Minimal fix:** Also accept leaf names `raises`/`warns`/`deprecated_call`.
- **Confidence:** high (reproduced).

### [IMPORTANT] `node_modules` (and `target/`, `dist/`) not excluded from directory walks

- **Where:** `src/gobby/test_quality/analyzer.py:194-195` (exclusion set is only `__pycache__`/`.venv`/`.mypy_cache`), rglob `:182-187`
- **Failure mode:** Reproduced: `node_modules/pkg/__tests__/vendor.test.js` is scanned and its issues reported against vendor code; `.rs` under Rust `target/` likewise. Auditing `web/` floods the report with third-party findings (perf cliff) and can fail `--fail-on-new` on unowned code.
- **Minimal fix:** Add `node_modules`, `target`, `dist`, `build`, `.git` to the excluded parts set.
- **Confidence:** high (reproduced).

### [IMPORTANT] `_check_imports` performs full heavy imports synchronously on the event loop

- **Where:** `src/gobby/voice/dep_check.py:47-55` (`importlib.import_module` at `:52`), called from async `ensure_stt_deps`/`ensure_tts_deps` (`:129,135,155,167`); event-loop entry `src/gobby/servers/websocket/voice/warmup.py:364-380`
- **Failure mode:** Probing availability imports `faster_whisper` (ctranslate2/onnxruntime) and `chatterbox` (torch/torchaudio/librosa) inline; first-time torch import alone is multi-second, stalling the daemon's whole WebSocket/HTTP loop for every session.
- **Minimal fix:** `await asyncio.to_thread(...)` at all four call sites.
- **Confidence:** high.

### [IMPORTANT] Availability/status probe imports the full Chatterbox runtime (torch) on the event loop

- **Where:** `src/gobby/voice/tts_chatterbox.py:78-89` (`importlib.import_module("chatterbox.tts_turbo")` at `:83`), reached from `is_available`/`get_status` (`tts.py:114-128`); callers `warmup.py:112`, `providers.py:65→77`, `src/gobby/servers/routes/voice.py:192-194`
- **Failure mode:** When chatterbox is installed, the "cheap" status getter performs the real torch import synchronously; the first `/api/voice/status` poll blocks the loop for the full import (intermittent after sys.modules caches it).
- **Minimal fix:** Only `find_spec("chatterbox.tts_turbo")` in the probe; defer the real import to `_ensure_model`'s threaded `_load`.
- **Confidence:** high.

### [IMPORTANT] `unload()` mutates the shared model (`conds = None`), contradicting its own safety docstring, and races `_ensure_model`

- **Where:** `src/gobby/voice/tts_chatterbox.py:219-221` vs docstring `:215-217`; race `:335-343`; callers `warmup.py:384-422` (never cancels `_active_tts_pipelines`)
- **Failure mode:** The docstring's safety argument covers `self._model = None` but `:220` mutates the *shared model object* an in-flight (or orphaned, per the Blocker) synthesis still holds — generation reads `model.conds` mid-flight and crashes. `unload()` racing the conditioning await leaves `_conditioning_ready=True` paired with `_model=None`, so the caller gets None and fails with a misleading error.
- **Minimal fix:** Make `unload()` take `_load_lock`/`_synthesis_lock` (or set only `self._model = None` and let GC release conds); cancel active pipelines first.
- **Confidence:** high (contract contradiction), medium (crash frequency).

### [IMPORTANT] Lazily-loaded voice models are never unloaded — idle reclaim gated on warmup status, not load state

- **Where:** `src/gobby/voice/tts_chatterbox.py:320`, `src/gobby/voice/stt.py:111` (models held on the singleton); `warmup.py:430-436` (`models_loaded` checks only warmup status strings)
- **Failure mode:** `_ensure_model` loads on first use regardless of warmup. If warmup errored but a later lazy load succeeds, status never becomes READY, so `_check_voice_idle` skips `_unload_voice_models` and the multi-GB models stay resident forever (pinning GPU memory on MPS/CUDA).
- **Minimal fix:** Gate unload on actual state (`self._tts_provider is not None or self._whisper_stt is not None`).
- **Confidence:** medium.

### [IMPORTANT] SentenceBuffer: unbounded accumulation and O(n²) re-scan when the stream has no sentence boundary

- **Where:** `src/gobby/voice/sentence_buffer.py:47-53` (`self._buffer += chunk` then `_SENTENCE_END.split(self._buffer)` over the whole buffer per feed); no length cap in `feed` (`:38-81`)
- **Failure mode:** Text with no `[.!?]`+whitespace (code blocks, logs, tables — what attached-session terminal TTS feeds, `voice_attached.py:71`) emits nothing until `flush()`, while every `feed` re-scans the whole buffer. Verified: 500k chars in 250-char chunks → 0 sentences, 500k-char buffer, 2.6s cumulative CPU on the loop.
- **Minimal fix:** Force-emit via `_split_text` on the prefix when buffer exceeds a multiple of `max_chunk_chars` with no boundary; scan only the tail.
- **Confidence:** high (demonstrated).

### [IMPORTANT] Tensor→PCM conversion runs on the event loop and may synchronize the GPU

- **Where:** `src/gobby/voice/tts_chatterbox.py:363-366` (`wav.squeeze().cpu().numpy()` and `np.clip(...).astype(...)` outside the `to_thread`)
- **Failure mode:** On MPS/CUDA, `.cpu()` blocks the event loop until the GPU drains — potentially a large fraction of synthesis time — plus the full-utterance numpy copy/clip, in the daemon's hot WebSocket path.
- **Minimal fix:** Move squeeze/cpu/clip/int16 into the `asyncio.to_thread` body and return ready bytes.
- **Confidence:** medium.

### [IMPORTANT] `except Exception: pass` swallowing subprocess reap errors

- **Where:** `src/gobby/voice/dep_check.py:97-101` (`proc.kill()` then `await proc.wait()` inside `except Exception: pass`)
- **Failure mode:** Any failure reaping the killed installer is silently dropped; violates the repo's no-`except Exception: pass` contract (impact low — post-kill cleanup with a following error log).
- **Minimal fix:** Catch `ProcessLookupError | OSError` specifically, or log at debug.
- **Confidence:** high (code), low (impact).

### [IMPORTANT] `file_paths.py` has no direct tests; fakes mask the real storage contract

- **Where:** only indirect coverage (`tests/review_learning/test_lessons.py:5,66,90`); `git grep "paths_match|normalize_lesson_file_path|extract_file_paths" tests/` → no other hits; `tests/review_learning/conftest.py:90-96` implements `tags_all` as an exact filter, hiding the storage window
- **Failure mode:** The matching semantics gating context injection and the dedupe/promotion math are load-bearing and entirely unasserted against real behavior; the e2e test covers only small-N happy paths where the window can't bite.
- **Minimal fix:** Add a `file_paths.py` unit test matrix and one storage-backed test proving occurrence dedupe/count with >3·limit pattern memories.
- **Confidence:** high.

---

### [NIT] candidates `_has_mutating_option` misses real mutating forms; final filter drops slot

- **Where:** `src/gobby/project_verification/candidates.py:172-176,88`
- **Note:** `ruff check --fix-only`/`--unsafe-fixes` pass as safe; `generate_candidates` calls the filter with no `slot`, so the prettier-`--check` guard never fires. Match any `--fix*`/`--write*` token; thread the slot through.

### [NIT] `_is_frontend_command` hardcodes `cd web &&` while `FRONTEND_SUBDIRS` lists seven dirs

- **Where:** `src/gobby/project_verification/candidates.py:485-487` vs `evidence.py:26-34`
- **Note:** A `cd frontend && ...` non-npm tool is mis-slotted. Check membership against `FRONTEND_SUBDIRS`.

### [NIT] refresh silently drops a user's existing verification command if it trips the safety filter

- **Where:** `src/gobby/project_verification/candidates.py:88,194-225`
- **Note:** An existing `ruff format src/` is filtered out, so `after` omits it, `changed=True`, and `--fix` deletes the user's command with no notice. Preserve or report unrecognized existing commands.

### [NIT] Case-insensitive filesystems collect Makefile/justfile/Taskfile twice

- **Where:** `src/gobby/project_verification/evidence.py:308,324`
- **Note:** Reproduced on APFS — duplicate recipe items into the synthesis payload. Deduplicate by `path.resolve()`.

### [NIT] Existing-command evidence items hardcode confidence 0.75, contradicting recomputed candidate confidences

- **Where:** `src/gobby/project_verification/evidence.py:179,191` vs `candidates.py:433-441`
- **Note:** The same command appears in the synthesis prompt with two confidences. Drop the hardcoded value or compute via `_existing_confidence`.

### [NIT] Async `refresh_project_verification` does all evidence/file I/O synchronously

- **Where:** `src/gobby/project_verification/refresh.py:100,151`
- **Note:** Only the CLI calls it today (verified — no daemon/HTTP/MCP callers), but the async signature invites daemon adoption that would block the loop. Wrap in `asyncio.to_thread` or document CLI-only.

### [NIT] Broad `except Exception` with a lone MemoryError carve-out around AI synthesis

- **Where:** `src/gobby/project_verification/refresh.py:121-128`
- **Note:** Any programming error in synthesis silently degrades to deterministic output. Catch the expected LLM/json/validation surface; let genuine bugs propagate.

### [NIT] Atomic write narrows project.json to mkstemp 0600 permissions

- **Where:** `src/gobby/project_verification/refresh.py:193-203`
- **Note:** `mkstemp` creates 0600; `os.replace` preserves it, silently narrowing project.json from 0644. `os.chmod(tmp_name, 0o644)` (or copy original mode) before replace.

### [NIT] "Do:" guidance echoes the avoid-text when prevention starts with "Avoid …"

- **Where:** `src/gobby/review_learning/service.py:580-593`, rendered `guidance.py:23-26`
- **Note:** `prevention="Avoid using bare except"` → injected `Do: Avoid using bare except` / `Avoid: using bare except`. Emit only `Avoid:` when the marker starts the text.

### [NIT] Legacy 200-row scan runs unconditionally on the before_tool hot path

- **Where:** `src/gobby/review_learning/service.py:363-374`; hot path `inject-review-lessons-for-touched-files.yaml:6-28` (`before_tool`, `background:false`)
- **Note:** Even when the tag-path scan already yields ≥`bounded_limit` candidates, the legacy fallback still lists up to 200 memories per Edit/Write. Skip it when tag matches already cover `limit`.

### [NIT] `_parse_evidence` breaks if "## Evidence" appears in a finding title/message

- **Where:** `src/gobby/review_learning/service.py:545-555` (`content.split(marker, 1)`); content `lessons.py:287-315`
- **Note:** The split lands inside the title, evidence parse returns None, and file-path matching degrades silently. Anchor the marker to a line or take the last occurrence.

### [NIT] rules/CLAUDE.md group table omits the review-learning rule group

- **Where:** `src/gobby/install/shared/workflows/rules/CLAUDE.md` vs `.../review-learning/inject-review-lessons-for-touched-files.yaml:1-9`
- **Note:** Doc drift; add a `review-learning` row.

### [NIT] `path_tag("")` yields a plausible-looking tag for the empty path

- **Where:** `src/gobby/review_learning/file_paths.py:47-50`
- **Note:** `path_tag('')` and `path_tag(None)` both return `path:e3b0c44298fc`. Return `""`/raise on empty; callers skip falsy tags.

### [NIT] Dead `guardrail_status` parameter / permanently stale `guardrail:lesson-only` tag

- **Where:** `src/gobby/review_learning/lessons.py:183,194`
- **Note:** No caller passes `guardrail_status=`; the tag never flips after promotion, so every lesson forever claims `guardrail:lesson-only`. Drop the param or update the tag on promotion.

### [NIT] Dead Protocol method `PromotionMemoryManager.list_memories`

- **Where:** `src/gobby/review_learning/promotion.py:35-43`
- **Note:** No call sites (`promote_lesson` uses only `alist_memories`). Remove the sync method.

### [NIT] Unbounded, inconsistently-formatted label growth on guardrail tasks

- **Where:** `src/gobby/review_learning/promotion.py:214-217,266-275`
- **Note:** Two labels per occurrence accrue indefinitely; raw `source_review` values make fragile labels. Cap/replace `evidence:`/`review-lesson:` labels on update; slugify values.

### [NIT] Identity fallback hashes `str(dict)` — insertion-order sensitive

- **Where:** `src/gobby/review_learning/lessons.py:155`
- **Note:** Two identical findings with different key order produce different pattern IDs. Use `json.dumps(finding, sort_keys=True, default=str)` (a `_json_blob` already exists at `:352`).

### [NIT] `_diagnostic_locations` emits malformed range when `start_line` missing but `end_line` set

- **Where:** `src/gobby/review_learning/promotion.py:325-331`
- **Note:** `{path:"a.py", end_line:5}` renders `- a.py-5`. Append `-{end}` only when `start` is present.

### [NIT] No case-folding or symlink resolution in path matching

- **Where:** `src/gobby/review_learning/file_paths.py:29-44`
- **Note:** `paths_match('Src/Foo.py','src/foo.py')` → False, same file on macOS's default FS. Casefold during normalization; document/resolve symlinks.

### [NIT] `await asyncio.sleep(0)` and any `*.sleep` method flagged SLEEP_IN_TEST

- **Where:** `src/gobby/test_quality/analyzer.py:474-475`
- **Note:** The canonical yield-to-loop idiom (and any `clock.sleep`) is flagged. Exempt `asyncio.sleep(0)`; consider dropping the broad `.endswith(".sleep")` arm.

### [NIT] Line-less fingerprint collapses repeat instances and masks recurrence through the baseline

- **Where:** `src/gobby/test_quality/models.py:80-81`, `analyzer.py:496-502`
- **Note:** Two `assert True` lines in one test → one issue; adding a second to a baselined test is never "new". Document, or add an occurrence count to baseline entries.

### [NIT] `--write-baseline` absorbs failing issues even on a failing run

- **Where:** `src/gobby/test_quality/cli.py:86` (unconditional write before the failure check at `:95-96`)
- **Note:** `audit --baseline X --fail-on-new --write-baseline X` exits 1 but has already rewritten X to include the failing issues; the next run passes. Refuse/warn when `--write-baseline` combines with a failing diff.

### [NIT] Test discovery misses pytest/Jest-collectable shapes (fails open)

- **Where:** `src/gobby/test_quality/analyzer.py:231-247,46`
- **Note:** Nested test classes, `unittest.TestCase` subclasses, and chained Jest modifiers (`it.concurrent.skip`) are invisible (reproduced). Nil impact in this repo today (pyproject pins `Test*`/`test_*`), but skills run this audit elsewhere. Recurse into nested ClassDefs; treat `TestCase` bases as test classes.

### [NIT] Rust: same-line `#[test] fn ...` skipped; `?`-anywhere counts as outcome check

- **Where:** `src/gobby/test_quality/analyzer.py:575-583,620-632,697-698`
- **Note:** `#[test] fn one_liner() {...}` not counted; `"?" in body` matches `?` in strings/comments. Both fail open. Resume scanning at `attr_end+1` within the same line.

### [NIT] `it.todo("...")` misclassified as NO_ASSERTION (high)

- **Where:** `src/gobby/test_quality/analyzer.py:729`
- **Note:** The Vitest/Jest todo placeholder (no callback by design) is flagged high, blocking `--fail-on-new`. Map `.todo`/`.fixme` to `TODO_IN_TEST` and skip the assertion check.

### [NIT] Text renderer never lists new-but-below-threshold issues

- **Where:** `src/gobby/test_quality/render.py:37,54-58`
- **Note:** When `failing_issues` is non-empty only those are listed; "New issues: N" can exceed what's shown. Add a "New issues (below threshold)" section.

### [NIT] Dead `_loading` attribute in WhisperSTT

- **Where:** `src/gobby/voice/stt.py:52`
- **Note:** Written once, never read (`_load_lock` does the real work); the test asserts dead state. Delete attribute and assertion.

### [NIT] Unreachable ext_map key in STT

- **Where:** `src/gobby/voice/stt.py:169` (`"audio/webm;codecs=opus"`) vs lookup `:177` (`split(";")[0]`)
- **Note:** The lookup normalizes away parameters, so the parameterized key never matches. Drop it.

### [NIT] STT temp file leaks if the write fails

- **Where:** `src/gobby/voice/stt.py:183-185`
- **Note:** `NamedTemporaryFile(delete=False)` + `f.write` happen before the try/finally that unlinks (`:187,219`); an ENOSPC write leaves the file. Move creation+write inside the try.

### [NIT] `list_tts_providers` has zero callers

- **Where:** `src/gobby/voice/providers.py:23-25`
- **Note:** Dead public API (`git grep` → only the definition). Delete or wire into status/CLI.

### [NIT] Voice provider factory load failures logged at debug, surfaced as reason-free status

- **Where:** `src/gobby/voice/providers.py:37-44,67-72`
- **Note:** An ImportError loading the provider is invisible at default log level and the status gives no cause. Log at warning; thread the exception into the status `reason`.

### [NIT] HTTP voice status route always reports cold provider state

- **Where:** `src/gobby/voice/providers.py:63-77` (fresh provider per call) vs `src/gobby/servers/routes/voice.py:192-194`; websocket prefers the live singleton (`warmup.py:259-269`)
- **Note:** `tts_runtime_primed`/`tts_reference_audio_conditioned` are always False over HTTP even when the daemon singleton is warmed. Let the HTTP route consult the live provider.

### [NIT] Zero-length audio yielded as success

- **Where:** `src/gobby/voice/tts_chatterbox.py:365-367`
- **Note:** An empty `wav` becomes `b""` yielded with success; clients get an audio header plus a 0-byte frame with no error. Skip the yield when `pcm_int16.size == 0`. (low — no confirmed empty-tensor path.)

## Systemic patterns

1. **Trust boundary moved without the sanitization moving with it.** project_verification lifts commands from repo content (CI, Makefiles, package.json, docs), vets only *options*, then auto-writes and shell-executes them with the user's full environment. Both project_verification Blockers and the candidates/CLI Importants are instances; `is_safe_validation_command` reports safe while the contract is violated.
2. **Consumers treat approximate storage as exact.** review_learning's whole ladder rests on `tags_all` being a real filter, but storage tag-filters only a `limit*3` newest window — and the test fakes encode the *intended* contract, not the real one. Correctness holds at demo scale and decays predictably with data growth.
3. **Cancellation vs `asyncio.to_thread`.** The voice stack assumes cancelling the awaiting coroutine stops the work; it never does. Orphaned inference threads outlive locks, unloads, and pipeline replacement — the Blocker, the `unload()` race, and the never-unloaded-models leak are all this one mistake.
4. **Check-then-act without uniqueness backstops.** Memory dedupe and guardrail-task find-or-create rely on racy reads with no DB constraint or lock, in a system explicitly built for many concurrent agents.
5. **Identity without canonicalization.** Fingerprints join values without field labels; file paths are hashed/matched in whatever form the producer supplied (absolute worktree vs absolute main vs relative). Same logical thing gets multiple identities → both false dedup and false matches.
6. **Tolerate-on-read, crash-on-write asymmetry, and "bounded" as blind truncation.** project_verification swallows parse failures on read but crashes on the same inputs at write; the 64 KB cap converts valid files into silent parse failures that cascade into data loss.
7. **Gate tools eroded from both sides.** test_quality's hand-rolled JS/Rust lexers fail open (hidden tests, zero-file green runs, scanner desync) while its Python checks fail closed with high-severity false positives (`raises`, `regex.test`, `it.todo`, bare xfail span leaks) that push users to the suppression escape hatch — every miss is silent, with no cross-check against the real runner's collection count.
8. **Cross-subsystem ownership gaps.** review_learning's pattern memories are load-bearing evidence with no lifecycle owner; the only deletion path (memory dream) doesn't know they're protected, so consolidation can silently break the promotion ladder.
