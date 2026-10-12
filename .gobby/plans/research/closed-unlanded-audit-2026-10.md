# Closed-task unlanded-commit audit, October 2026

208 closed tasks reference 274 commits that are neither ancestors of the audited `0.5.0` tip nor patch-equivalent to its history. The initial proof pass found supersession evidence for all residual commits of 112 tasks and left 96 tasks for recovery review. The requested six-task semantic check identifies three missing corrections and three delivered/superseded results. Of the other 90, **89** retain **missing linked revision, semantic supersession not proven**; **#23269 — Node bootstraps refuse the Python daemon** is confirmed missing and already tracked by #24075 — Land node-bootstrap refusal.

Separately, 512 closed tasks reference 491 unavailable SHA strings. One additional task links a blob instead of a commit. These 513 tasks cannot be assessed as lost work from the local object store. The three groups do not overlap; the report covers all 721 exceptional tasks.

## Scope and counts

- Audited integration tip: `8eb6e7a8b568fc3eec51f0111689e124db2b34c1`.
- Task snapshot: 2026-10-12T01:09:32.196764+00:00; obtained through `gobby-tasks:backup_tasks` to a temporary JSONL file. PostgreSQL task data came through MCP, not direct SQL or the operator tasks CLI.
- 18,069 project task records; 17,762 closed tasks; **15,553 closed tasks with linked commits examined**.
- 24,166 task/commit links; 21,797 distinct linked SHA strings; 18,810 distinct resolved commit objects. Short/full aliases account for the difference.
- 628 linked SHA strings resolve to patch-equivalent commits; 274 to unlanded commits; 491 are absent; one resolves to a blob. Two malformed comma-joined references are excluded from the absent count after their four component commits were verified as ancestors. The original ancestor-string total is omitted; raw inventory counts above remain unchanged.
- Within the 274 unlanded commits, 153 have supersession evidence and 121 remain unproven. The 96 recovery-review tasks may also contain superseded revisions.
- The task snapshot is fixed. Closures after its cutoff are outside the sweep. The six-task semantic addendum separately pins newer integration source.

## Method and limitations

1. Selected every snapshot record with `closed_at` and a nonempty linked `commits` list, including documentation, research, release and probe tasks.
2. Resolved references with `git cat-file --batch-check`. Compared resolved commit OIDs with `git rev-list 8eb6e7a8b568fc3eec51f0111689e124db2b34c1`, the same ancestor relation tested by `git merge-base --is-ancestor`. Sample ancestry checks for the two completed Lane 4 candidates returned exit zero.
3. A literal `git cherry <pin> <head>` walk for each historical head repeatedly rebuilt upstream patch IDs. The Lane Manager and Orchestrator approved one shared upstream index plus bounded cherry confirmation. The upstream stream was `git rev-list <pin>`, then `git diff-tree --stdin --root -p --no-renames --no-ext-diff`, then `git patch-id --stable`. The same diff/patch-ID stream covered all 902 distinct non-ancestor commit objects. 19,808 upstream patch IDs were indexed; 628 candidate IDs matched.
4. Bounded `git cherry` confirmations returned `- 51fb3c2bbcd02ccb4b8abd958c6850f7e50f3688` and `- 92c343553965e5533d190154b349969abeab6681`. The known missing node-bootstrap revision returned `+ 91a483db0d906938cd6adecc4c8f9ba89e90980d`.
5. Mapped holding local/remote branches through a topological parent walk seeded with ref tips; mapped worktrees through `git worktree list --porcelain`. Branch/worktree locations are the captured ref inventory, not a promise that a live fleet will preserve them. No holding branch means the object exists but no captured local/remote branch reaches it; tags/reflogs were not used as branch substitutes.
6. Supersession evidence is bounded: exact (path, file mode, blob) versions in landed history (44 commits); later task-tagged landed revisions covering the same changed paths (99); later deletion of retired paths or exact retained versions (5); baseline integration merges with an ancestor second parent and empty remerge diff (5). Same-task path coverage establishes a later delivered revision, not independent semantic equivalence of every discarded draft.
7. Unproven revisions retain the Orchestrator-approved label **missing linked revision, semantic supersession not proven**. Semantic reconstruction was limited to the six requested recent tasks below. No test suite, branch replay, cherry-pick or source repair was performed. Missing objects cannot establish whether their patches landed under other SHAs.
8. Prefix `e2277d3f` matched one tree and one commit. The sole commit, `e2277d3fabb9508b4d3bb7f8ecbe7f34d6ce7fb0`, is an ancestor and names the linked lifecycle fixes. It was resolved read-only; no task link was edited.

## Confirmed missing work already tracked

**#23269 — Node bootstraps refuse the Python daemon**: linked revision `91a483db0d906938cd6adecc4c8f9ba89e90980d` is unlanded, with no upstream patch-ID match and a positive cherry result. It is held by `task-23269-node-refusal`. MCP task **#24075 — Land the node-bootstrap refusal** records that the refusal and its tests are absent and already owns the port to the post-lease-retirement lifecycle seam. This audit creates no duplicate landing task.

## Review correction and lane-3-runbooks-4 check

The independent review found two malformed linked reference strings. **#1130 — Implement handoff artifact context injection** links `4d1513f,e1f7fa4`; **#1356 — Fix premature stop counter to reset on tool calls** links `6923875,48f7811`. Each is a comma-joined pair, not an absent commit. Read-only `git merge-base --is-ancestor <component> 8eb6e7a8b568fc3eec51f0111689e124db2b34c1` returned exit zero for all four resolved commits:

- `4d1513f789212d7cfd07beb08daf5639f70c14e8`
- `e1f7fa4cfc7541754d5c079e77b119dddc855828`
- `692387554225099f57f546bc1bd70c22b6fbbca5`
- `48f781117f036f2020a164ef96069b7f5b9a5b88`

Both tasks are landed and removed from the unavailable-object table. The corrected counts are **512 unavailable-object tasks / 491 absent strings / 721 exceptional tasks**. The 24,166 links and 21,797 distinct strings are the raw snapshot inventory, including these malformed strings; their ancestor-string aggregate is omitted. Neither task ledger was rewritten.

The Orchestrator also requested a read-only check of two commits held by `lane-3-runbooks-4`. This does not retire the worktree or add previously unlinked commits to the sweep.

| Commit / task | Audit coverage | Evidence and recovery conclusion |
| --- | --- | --- |
| `1e55ed055636d6b8807e37bbaeb14687f5c74ea9` — #23752, Merge Manager and Inbox Manager seat definitions | **Outside the original linked snapshot, but patch-equivalent to landed work** | Snapshot links are `44cc5e0d69`, `5e74dd758a`, `8be6418fa4`; this branch revision was not linked there. `git cherry <sweep-pin> 1e55ed0556 1e55ed0556^` returned `- 1e55ed055636d6b8807e37bbaeb14687f5c74ea9`. Both this commit and landed `5e74dd758a7c8cdbe6e2e0399e7a378d373aa1b1` have stable patch ID `26baee20de8cf48f6631115154113c0901fa9068`. The complete Inbox Manager role-text test patch landed; **no recovery needed**. |
| `0d903330ac95fa7b802554ee13d38c796d2afedf` — #23754, spawn_agent resumes an existing provider thread for crew relaunch | **Included in the original unlanded table; superseded** | Later landed same-task `88fda9494010c7fdfad75c04d6bddede5038a42d` covers every one of its 14 changed paths and adds corrected relaunch lifecycle state. That commit is an ancestor of the sweep pin. Current source below retains provider resume propagation, relaunch preparation and rollback state preservation; **no recovery needed for this branch revision**. |

Fresh source evidence for the second conclusion is pinned to `3ab433a9e8079fa48c1cc54aa7afda59659c51f7`, tree `43fe57cd499ae43c3107468b9e869d5ff72e73d5`, observed 2026-10-12 around 03:01 UTC:

- `src/gobby/agents/spawn_executor_providers.py:458-464`, excerpt hash `1b74676c7b6e91ce93a58c3370d10f02cfe13a2cc0443b4ddefdca56049178f7`: `459|         cli="codex",`, `460|         resume_session_id=request.resume_session_id,`.
- `src/gobby/agents/runtime_cleanup.py:44-66`, excerpt hash `cb824ac4c2355cac31bf17f1284f0722afab6d43832e46fdb5d642ded745a8b7`: `51|             preserve_session_state = bool(metadata and metadata.get("resume_existing_session"))`, `66|     if child_session_id and not preserve_session_state:`.
- `src/gobby/agents/spawn.py:340-404`, excerpt hash `da89e8262a49a0272e9f8758a8cbd1206aa29476bcbb1ffd9b6b6c38638b596f`: `372|     relaunch: bool = False,`, `399|             relaunch_parent_session_id=parent_session_id if relaunch else None,`.

## Bounded semantic check of six recent tasks

The Orchestrator requested these six checks before the report commit. Source evidence below is pinned separately to `0.5.0` commit `b3944b8da2e1d2bc98fd31cc25607e59a73cff4c`, tree `3231a99c5481055e5358366bc4b8e4b7dd966afe`, observed 2026-10-12 around 02:00 UTC. This addendum does not recompute the original ancestry/patch-ID sweep.

Of the original 96 recovery-review tasks, these six resolve to **three with missing linked behavior/correction** and **three with delivered or intentionally superseded behavior**. Of the other 90, **89** retain **missing linked revision, semantic supersession not proven**; **#23269 — Node bootstraps refuse the Python daemon** is confirmed missing and already tracked by #24075 — Land node-bootstrap refusal. The original 153/121 revision-proof counts describe the initial sweep; semantic conclusions here override the six tasks' recovery interpretation, without changing their unlanded SHA status.

| Task | Semantic result at addendum pin | Missing work / delivered evidence |
| --- | --- | --- |
| #23201 — Reopening a task leaves its post-close memory review demanded by the stop gate | **Missing** | `81a71a4d8088028f4dabea73cb209109bfb3f19c` withdraws pending closure reviews on reopen/reset/move/expansion; current reopen clears closure but does not withdraw the queue. `src/gobby/storage/tasks/_transitions.py:447-469`; `src/gobby/workflows/memory_review_conditions.py:78-112`; `src/gobby/mcp_proxy/tools/memory_review.py:189-190`. |
| #23252 — Close gate extracts a behavior artifact's command-shaped prose as a mandatory exact command | **Missing** | Shared artifact-span exclusion in `0b125f0fe8` / `fb9e73ef724761da5b2b39ccadb4065d7f8dc2ad` is absent. The current extractor still returns the full cargo/prose span. `src/gobby/tasks/criterion_commands.py:268-291,336-345`. |
| #23304 — Found-work stop gate treats a direct pytest usage error (exit 4) as an uncoverable terminal validation failure | **Intentionally superseded; final behavior delivered** | The broad exit-4 exemption in the four unlanded drafts was retired in favor of #23342 — Stop gate missing-path pytest supersession. A later green over surviving paths clears the specific missing-path red, while other exit-4 failures remain strict. Final linked `2c8f9db6d5c7c9b9b8cc5c30b6ff09362e4a3510` is an ancestor and adds real-subprocess regression coverage. `found_work_gate.py:273-275`; `validation_cover.py:259-308`. |
| #23345 — Plan semantic lint rejects plans after their leaves land | **Delivered under later completion semantics** | Current project-scoped completion lookup and completion-dependent size/symbol lints remain. The lone uncovered fixture change has also evolved into explicit draft-before-manifest and expansion-after-manifest checks. `src/gobby/tasks/expansion/_validate.py:168-180,270-305`; `src/gobby/plans/semantic_lint.py:206-207`; `src/gobby/plans/symbol_targets.py:538`; `tests/tasks/test_expansion_compile_validate.py:478-492`. |
| #23519 — Shared-token cutover: gdaemon validates keys and Python trusts only the front door | **Core authentication delivered; linked test-isolation correction missing** | Rust strips caller identity, validates an API key and stamps identity with its secret; Python verifies that secret. Missing residual `cc5581cd4f46a77503e2bc93b0356069eac14e72` adds early pytest home isolation before plugin bootstrap probes; current conftest has no such configuration hook. `crates/gdaemon/src/front_door/auth.rs:126-163`; `src/gobby/servers/auth_service.py:191-206`; `tests/conftest.py:46-84`; `tests/fixtures/postgres.py:219-229`. |
| #23626 — gclient chrome fixes: lane-title ticker, Help wheel scroll, header/tab colours, theme rename | **Delivered; residual merge is superseded release bookkeeping** | Pinned session ref plus ticking title, dialog wheel routing, distinct menu-bar role and Midnight moss are present. `80d5923ae04c0432043a1706a6ea87bdd74f648a` resolves only 0.1.28-versus-0.1.27 version conflicts; later source/release versions supersede that decision. `crates/gclient/src/ui/sidebar_rows.rs:438-473`; `app/live_loop/mouse/wheel.rs:17-55`; `ui/menu_bar.rs:60-73`; `theme/catalog.rs:135-139`. |

Recovery tasks filed by the Orchestrator: **#24084 — Recover #23201 reopen memory review** and **#24085 — Recover artifact-reference command exclusion** in Lane 1; **#24086 — Recover early pytest GOBBY_HOME isolation** in Lane 3. The already tracked **#24075 — Land node-bootstrap refusal** remains separate.

### Evidence: missing reopen withdrawal

Linked diff `81a71a4d80` adds `withdraw_memory_reviews` to storage transitions, stage reset/move, expansion apply and the session-variable manager. The pinned source has no occurrence of that identifier in those scopes. Current reopen ends with the ancestor reopen and task return, with no queue withdrawal:

- `src/gobby/storage/tasks/_transitions.py:447-469`, excerpt hash `bb97717b62e412e3fc764fc1a7efa743d1969d3536b2831fd16f6dd2acbb5e79`: `453|         closed_at=None,`, `468|     _reopen_closed_ancestors(db, task_id)`, `469|     return get_task(db, task_id)`.
- `src/gobby/workflows/memory_review_conditions.py:78-112`, excerpt hash `ca87ed2fb4593b1d886103ee7da9ce98b532f8ccfa6de0dbc5a4727f897183f8`: `96|         if not isinstance(item, Mapping) or item.get("closure_id") in reviewed_ids:`, `111|         remaining.append({"task_id": task_id, "task_ref": task_ref})`. Closure retirement is not consulted.
- `src/gobby/mcp_proxy/tools/memory_review.py:182-192`, excerpt hash `f5c561bed5dad32832c8454ea790fb254345d6b5339dffd3ba5705bff49672c3`: `189|         if task.closed_at is None:`, `190|             return _error("task_not_closed", f"Task {_task_ref(task)} is not closed.")`.

A read-only function probe passed an unreviewed retired closure entry to `pending_memory_reviews`; it remained pending. No live task was closed or reopened. Impact: a reopen can leave a review that the review endpoint refuses, retaining the stop-gate deadlock. Send this missing correction to the Orchestrator for landing.

### Evidence: missing artifact-span exclusion

The final linked diff makes gate 10 consult the same `artifact_reference_span_starts` recognizer as gate 11, including behavior/symbol labels. That helper and exclusion are absent from the pinned task modules.

- `src/gobby/tasks/criterion_commands.py:268-291`, excerpt hash `e19d05d2f926acb2101697f6602bb5e2281cfda572e2cd7b781f5caf7bd08623`: `274|         return True` is followed by prose masking at `277|     masked = _CRITERIA_CODE_SPAN_RE.sub(lambda span: " " * len(span.group()), criteria)`; there is no artifact-reference check.
- `src/gobby/tasks/criterion_commands.py:336-345`, excerpt hash `a1596fbca64cf27fa85b892c6c99f1a5b6b5d8b6f025c869b99588604bc9a85b`: `340|     for command in _command_shaped_spans(criteria):`, `344|         commands.append(command)`.

The read-only probe ``authored_criterion_commands('R1.7: behavior: `cargo nextest run -p gobby-client passes with no golden regeneration`')`` returned `['cargo nextest run -p gobby-client passes with no golden regeneration']`. Impact: command-shaped artifact prose can still block a close. Port the final shared recognizer, preserving standalone command requirements.

### Evidence: deliberately retired pytest exemption

The task snapshot's VALID close feedback explicitly says #23342 — Stop gate missing-path pytest supersession supersedes the retired fail-open exemption. The final linked commit `2c8f9db6d5` contains only real pytest subprocess tests; the old production drafts are not the approved final behavior.

- `src/gobby/workflows/found_work_gate.py:245-283`, excerpt hash `8a84b5fb1d242c2bf5e40ef5e0db9765f55f6bba26806a78007776160ba454bc`: `273|         surviving = surviving_path_failure(failed, project_path)`, `274|         if surviving is not None and any(run_covers(run, surviving) for run in later_greens):`, `275|             continue`.
- `src/gobby/workflows/validation_cover.py:259-320`, excerpt hash `c44eea519709c06fd7c4ad8854137e816aff340aad80ea86fff39c45977e4eba`: `278|         or not (failure.exit_code == 4 or _NO_TESTS_RE.search(output))`, `279|         or _COLLECTED_RE.search(output)`, `294|     if _tracked_at_head(directory, files, project_path):`, `295|         return None`.

A later green is required, and collected tests, other errors, truncated output or tracked missing paths remain strict. Restore none of the four old exemption drafts.

### Evidence: current plan-completion behavior

- `src/gobby/tasks/expansion/_validate.py:168-180`, excerpt hash `fa2437e1c94d1d11abde0889ed20acfb79f2a5976d2bb94389c2a1dedae12720`: `177|         delivered = task.closed_at and task.closed_reason in {"completed", "already_implemented"}`, `178|         if delivered or (project_root and _task_has_landed_commit(task, project_root)):`.
- `src/gobby/tasks/expansion/_validate.py:270-305`, excerpt hash `772620adf964f716f0d33235753c6fb334ae8c66143b723cc4c99dbaabcaeab1`: `286|         plan_doc, project_root=project_root, completed_section_ids=executed_section_ids`, `304|         completed_section_ids=completed_section_ids | executed_section_ids,`. Later semantics distinguish executed sections for size checks from completion evidence for symbol checks.
- `src/gobby/plans/semantic_lint.py:185-215`, excerpt hash `9b43b79b69120d43244e5233111eb9eeab12fdddaa49fd1bcecc8b03e9c3192f`: `206|         if project_root is not None and section.section_id not in completed_section_ids:`, `207|             issues.extend(_lint_production_size_growth(plan_doc, section, project_root))`.
- `src/gobby/plans/symbol_targets.py:523-552`, excerpt hash `74588bdfc799dc4582867968d1b48a2a13ce23d423825119c45e18030d6f9bf5`: `538|             if symbols and target.section_id not in completed_section_ids:`.
- `tests/tasks/test_expansion_compile_validate.py:478-492`, excerpt hash `55b932b472c250bcfcdd7c616eeeacb4e9e0c330ecb2515b13e1c7f98f937ff7`: `485|     draft_cli_result = _validate_plan_for_cli(plan_path, None, mode="draft")`, `489|     assert emit_stub_manifest(plan_path, plan_id="test-artifact") == "fresh"`, `490|     expansion_cli_result = _validate_plan_for_cli(plan_path, None, mode="expansion")`.

The old one-line fixture correction is superseded by these explicit parser-phase checks. No missing landing work was identified for this task.

### Evidence: authentication delivered, early test isolation missing

- `crates/gdaemon/src/front_door/auth.rs:126-163`, excerpt hash `fa5c071c5b5b02afce384ed56637466ef910141488226f005fab55c41fb46b53`: `128|             headers.remove(name);`, `144|         let key = api_key_format::parse(bearer).ok_or_else(missing_auth)?;`, `147|             .resolve(&api_key_format::hash(key))`, `155|             self.secret.as_str(),`.
- `src/gobby/servers/auth_service.py:191-212`, excerpt hash `d8bacc18532edbd9ab3e2ced78bc59440b9fcd5cbed3ef311e529da92bb08bc2`: `199|     if not secrets.compare_digest(supplied.encode(), secret.encode()):`, `200|         return None`, `206|     return FrontDoorIdentity(user_id, machine_id, key_id)`.
- Missing linked `cc5581cd4f46a77503e2bc93b0356069eac14e72` adds a `@pytest.hookimpl(tryfirst=True)` configuration hook and four inherited-home cases, keeping operator bootstrap metadata untouched. Current `tests/conftest.py:46-84`, excerpt hash `7676c28659e91a430e18b79052f935980a1c8f3de3d4cf77a7dbe24475068e68`, instead returns at `55|     if raw_home is None or not raw_home.strip():` and `59|     if home.resolve(strict=False) == operator_home.resolve(strict=False):`. Its import-time bootstrap helper is followed directly by session fixtures, with no early configuration hook.
- `tests/fixtures/postgres.py:219-235`, excerpt hash `fccc70c6530a1b584a7ad4f47dcee2d1da9f3560a7c0d8cf8e80cc616fe10b9c`: `228|     hub_identity = _live_hub_identity()` runs during plugin configuration. No home selection precedes it there.

This is a missing test-environment correction, not an unlanded shared-token cutover. Native `32658b9baf` is baseline merge bookkeeping; `8594953482` has later landed same-task documentation revisions. The focused landing scope is the early home-isolation correction and its regression tests. No operator bootstrap or token file was read by this audit.

### Evidence: chrome features and superseded release merge

- `crates/gclient/src/ui/sidebar_rows.rs:438-473`, excerpt hash `c4800f313b4a2865acaad3a2d46b02beff2ddfdb592b214f77773e2bb2af2a17`: `454|                         spans.push(Span::styled(row.reference.as_str(), identifier_style));`, `455|                         spans.push(Span::styled(": ", secondary_style));`, `457|                     spans.extend(ticker_spans(`, `460|                         chrome.ticker,`. The session ref remains pinned.
- `crates/gclient/src/app/live_loop/mouse/wheel.rs:17-58`, excerpt hash `e38ba8de483218b040a71bb0fcd0565837f4314a9b44b72250236195d29b7adf`: `32|         (Mode::KeybindHelp, _) | (Mode::ProjectDialog, Some(Dialog::Alerts { .. })) => {`, `33|             MOUSE_SCROLL_LINES`, `49|                 Mode::KeybindHelp => keybind_help_key(chrome, &key),`. Navigator/pick-list wheel handling uses one row.
- `crates/gclient/src/ui/menu_bar.rs:60-73`, excerpt hash `0fff0e6755b5a179b2371590087f40d931520131c6696b9b720c7b390cbe9ca7`: `68|         .bg(p.bar)`, `69|         .fg(p.bar_ink.ink(p.subtext0))`. The menu bar has its own palette role.
- `crates/gclient/src/theme/catalog.rs:124-142`, excerpt hash `ed10d207ba6ca8e29b5dd2d6851e36d5f98d37f03dbb2cc97693ee18f1cd75e7`: `139|             ThemeName::MidnightMoss => "Midnight moss",`.
- `git show --remerge-diff --format=short 80d5923ae0` shows only version conflicts in `Cargo.lock`, `crates/gclient/Cargo.toml` and `src/gobby/install/version_pins.py`, choosing 0.1.28 over 0.1.27. At the addendum pin, `crates/gclient/Cargo.toml:1-5`, excerpt hash `4db8771b837a10fb20393f910b6d0744af70c08f09cb7019a61b9da12e95866d`, says `3| version = "0.1.35"`; `src/gobby/install/version_pins.py:7-18`, excerpt hash `3819c004beedbea3bda84a500953a12aba68dc9f9e9decc98d773a3a5a1707a9`, says `11|     "gclient": "0.1.34",`. Both supersede the old merge choice; this is source/release evidence, not an installation or live-UI attestation.

No missing chrome behavior was identified from the residual merge. Installation/promotion status is outside this audit.

## Every closed task with an unlanded commit

| Task | Closed (UTC) | Unlanded linked SHAs | Holding branches | Holding worktrees | Verdict |
| --- | --- | --- | --- | --- | --- |
| #10399 — Fix messages scrolling under ChatInput after Phase 2b flex change | 2026-03-18T01:50:33.131916+00:00 | `7ae3b093b3f8624e20f8f2ac53f876e0089a2425` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11141 — Update bundled compose template image tags to latest, remove password defaults from installed copy | 2026-04-01T17:51:12.023460+00:00 | `c9e2a2937f2edb18a7042b156e350aca3ca08dfc` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11143 — Drop summary column from code_symbols | 2026-04-01T19:25:05.034915+00:00 | `a807c7b1bee4333d8504521de6cb89a54697788b` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11201 — Fix codex-rescue agent template mode: self → interactive | 2026-04-03T17:33:18.351781+00:00 | `2fe1e8c83ce00245d34f7693af5817387311d726` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11202 — Skill template refactor: eliminate source='template' DB rows | 2026-04-03T18:15:48.096720+00:00 | `69c8e41bb94c13c9c1eeff96fb77d47d95d7b097` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11206 — Migration v185 + baseline_schema.sql for checkpoints and dispatch_failure_count | 2026-04-03T19:18:18.352478+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11207 — Checkpoint storage CRUD module | 2026-04-03T19:18:46.249622+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11208 — In-memory loop tracker | 2026-04-03T19:18:54.719538+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11209 — Shadow git checkpoint manager | 2026-04-03T19:19:03.337994+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11210 — Task model dispatch_failure_count field through all layers | 2026-04-03T19:19:10.596935+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11211 — PromptDetector loop counting + lifecycle monitor integration | 2026-04-03T19:19:18.291871+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11212 — Unit tests for doom loop detection features | 2026-04-03T19:21:26.708766+00:00 | `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11214 — Remove session_id from stdio proxy tool schemas and wire ContextVar fallback | 2026-04-03T19:53:46.415288+00:00 | `62029ef16a78655650c0bf7f48600f5f20aa963b` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11215 — Remove stale session_id references from prompts, skills, and rules | 2026-04-03T20:00:26.509297+00:00 | `b9262c2fe5e568706d95a4089ca3ca1475a425a5` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11218 — Implement subagent rule isolation via is_subagent variable | 2026-04-03T21:14:42.581064+00:00 | `ef6038c2fb092d481b72fe5d77af1b58c31bda1f` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11223 — Fix Neo4j auth: resolve $secret reference before passing to Docker | 2026-04-04T02:55:09.540466+00:00 | `44efe20794c32955eb114c85039954656a4260ce` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11224 — Disable codex-review-gate in template and database | 2026-04-04T03:15:11.952223+00:00 | `24c4d53679310131a1c0e977389c3c17e90fb5c2` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11225 — Remove dead executor layer, streaming code, and UI remnants | 2026-04-04T04:27:22.529402+00:00 | `f6d3e84b4fdf50796dfcae55729353f9f3b573b9` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11226 — Fix stuck is_subagent variable on interrupted subagents | 2026-04-04T04:47:30.543556+00:00 | `d4bda7781b97413a8855aa0e3c3d547bc508ec53` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11227 — Add v186 migration for agent definition mode renames (terminal→interactive, self→inherit) | 2026-04-04T05:17:30.853561+00:00 | `8ddf86c8192221da27b0aeeec92b9d1639673090` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11230 — Fix neo4j auth: move password to bootstrap.yaml | 2026-04-04T05:39:19.251364+00:00 | `f9f31971730b6953fb44065df9f710b5af022c4c` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11231 — Pin claude-agent-sdk<=0.1.45 and investigate 0.1.53 breakage | 2026-04-04T05:57:12.530272+00:00 | `7829ab519325e5e00a5521850f55b2359dbf027a` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11232 — Pass CLAUDECODE=1 explicitly in SDK options to unpin claude-agent-sdk | 2026-04-04T06:05:29.731628+00:00 | `6467de53b11bf7146492d78e7f45db13d555daff` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11235 — Remove session_id parameter from task/variable tool calls | 2026-04-19T05:50:51.852025+00:00 | `000334f1a563c54aa03ddcebb9247125eb40ca13`, `b55a28e3a16effdf74f2dad8e0f5bf3784afcbdc` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11238 — Fix /clear and /compact handoff context injection | 2026-04-04T14:12:44.500651+00:00 | `121640b3e590d6fe7fb5f7447f5aef56b4b2ddfb` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11241 — Clean up stale mode='terminal' references in docs, docstrings, and YAML | 2026-04-04T16:20:51.699783+00:00 | `184a1e964258fed862908989d4b9943347af7737` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11242 — Fix set_variable receiving unexpected session_id kwarg from proxy | 2026-04-04T16:12:19.299215+00:00 | `a7a0e70f03c168ce5cf7c790e364d39985baf2c9` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11243 — Add CLI diagnostic probe on SDK text generation failure | 2026-04-04T16:23:36.157700+00:00 | `184a1e964258fed862908989d4b9943347af7737`, `5a20e9e5c91c827e37b30050a34272427f642e7b` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11246 — Add session_id param to set_variable and get_variable in stdio MCP proxy | 2026-04-04T17:35:44.869783+00:00 | `4c3a205eaa299b74e351b37bb66d503390189841` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11265 — Pin claude-agent-sdk <=0.1.45 (initialize handshake broken in newer versions) | 2026-04-04T18:39:53.015952+00:00 | `10f6d97f93227baae493bbbb78b41ca01504735e` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11322 — Clean up remaining litellm string references in docstrings and tests | 2026-04-05T21:10:47.644168+00:00 | `9e46412e93bfb5d2119ea865d326c8ef3ac23032` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11339 — Fix CodexHooksAdapter: Stop hook rejects hookSpecificOutput | 2026-04-06T00:51:43.625636+00:00 | `029e42e7008aa53d36f517a4be3589a4f8152780` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11350 — Unified CLI Launcher — SDK Removal + Interchangeable Web Chat Sessions (v3) | 2026-04-06T06:05:05.616327+00:00 | `c741003136533a8a694d185e3210fd3d45117663` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11357 — Phase 7: Remove Claude Agent SDK and associated code | 2026-04-06T05:43:35.339308+00:00 | `3d4269f9730a4cb741f3a262a88160e3669c2a29` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #11389 — Fix mypy incremental cache crash in pre-push hook | 2026-04-06T22:02:06.573747+00:00 | `b192f2db9b1aa7f8e42ef4f0d4821e4002d5c783` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #11453 — Implement CLIChatSession for Claude | 2026-04-07T04:46:51.777850+00:00 | `2264a2f63cf1315e2a636469cfd0888959467872` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #12754 — Implement durable wake messages for agent continuations | 2026-04-25T17:28:40.718180+00:00 | `93b1d320c5c3feaa69b1440261e47c6a87ae8f33` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #12965 — LOCAL chip on Workflows agent cards | 2026-04-27T02:57:24.633063+00:00 | `2fb027f7010ce7faa1b675931861c0a77556576f` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #13500 — [IMPL] Runtime dispatch mutex wrapper | 2026-04-30T13:58:54.631825+00:00 | `69bcadbe34ba9284e80dff8c78b416c5624500b0` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #13518 — [IMPL] developer agent (active root) | 2026-04-30T14:45:18.639693+00:00 | `b2a40f86a7f0120fad510da88652cc98cb625426` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #13900 — Prove gobby build automation readiness for expanded epics | 2026-05-03T08:50:18.470558+00:00 | `a53cd5630188768cae2c83f59505cf76b8320488` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #14153 — Expose allow_automation toggle in update_task MCP tool and add gobby unbuild CLI | 2026-05-04T02:56:03.885010+00:00 | `be0d25c2e3808762418e04015ae2b7ece5cfe6ad` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #14364 — fix: stopped agent runs should release task ownership consistently | 2026-05-07T13:02:22.891466+00:00 | `50bb5188a0beb59fea8fda0b5122f1823618fd0c` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #14903 — Fix already-implemented merge detection for branch-only leaf commits | 2026-05-20T14:43:09.617988+00:00 | `3fc24d8250833108911d1db73999258e144f6849` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #14912 — Align merge-worker prompts with merge step tool allowlist | 2026-05-20T17:32:15.826139+00:00 | `e2537a061ec46e6a55d97a53984bc7f22e5fe1ba` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #14970 — Fix active task listing rows missing status after PostgreSQL cutover | 2026-05-21T15:26:05.556686+00:00 | `4f32b54d590aafaf667e06b0d2f1004252dfb142` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15039 — Refresh adapter fidelity after AGY and Grok integration | 2026-06-16T06:06:39.362156+00:00 | `26b698d55bfa40001d2b23eccb4998f0cbfe4c05` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15050 — Resolve injected wrapper session ids to canonical Gobby sessions | 2026-05-23T02:36:01.571308+00:00 | `5afd9980e7f60d6c7189f0ffe74adb8e7827f293` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15052 — Fix hook deliver_pending_messages calls missing target_session_id | 2026-05-23T01:25:16.300439+00:00 | `1eea88171fbf6229c91d7927c1e795b4950f5a06` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15115 — Repair local daemon bootstrap database URL | 2026-05-24T02:28:52.720296+00:00 | `97a1830db09fadf6aaf93b84d78a8f9520e70a74` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15118 — Remove build coordinator monitoring autoload rule | 2026-05-24T14:59:48.854431+00:00 | `c01a1fd2b760f6abb8f964d663ae5ca2cb34d3fe` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15132 — Make plan artifacts reachable in isolated review worktrees | 2026-05-24T16:54:11.821590+00:00 | `dc641be8f31df9df11f77861f3f19c5747c171f0` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15133 — Preserve ambient MCP session context for reviewer agents | 2026-05-24T16:48:15.795818+00:00 | `f377dda09f7aade20e0d5a5551e05fdf6d519343` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15157 — Make live wake prompt neutral instead of task-completed | 2026-05-24T21:02:34.456431+00:00 | `4d726c9c7a0de9081882d359a044b4186dcbaef9` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15271 — Remove gobby Stop hook wrapper installation | 2026-05-28T22:06:57.089517+00:00 | `a23c46a6f1caf94dccf9072915bc45f6af5dcc6d` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15293 — Prepare and publish 0.4.11 release | 2026-05-30T04:55:34.365129+00:00 | `d4cf939a6dd9ef9b0e466b501c505dbfe17979c3` | refs/heads/0.4.11, refs/remotes/origin/0.4.11 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15296 — Add dispatcher dependency base guard | 2026-05-29T20:24:18.847182+00:00 | `9d396e5531c4edd94ffb861f1ca23cd5f1f388d7` | refs/heads/0.4.11 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15329 — Bump version files for 0.4.12 branch | 2026-05-30T07:48:09.430571+00:00 | `89bf2aabcd377de01aaf2eca22b2fc51b033a380` | refs/heads/0.4.12, refs/remotes/origin/0.4.12 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15379 — Fix validation feedback false positive | 2026-05-31T06:34:17.491106+00:00 | `aaafefde75ec5f601e4b3c0e000628b113069e13` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15526 — Fix build stop leaving stopped tree automation eligible | 2026-06-02T07:45:45.735865+00:00 | `1da46360a856f82eb6006304e31798a422c16e89` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15562 — Bump version to 0.4.15 on release branch | 2026-06-03T02:06:46.078069+00:00 | `dc4f91584963da2f9a7a81f31fcc6df0fa6b2278` | refs/heads/0.4.15, refs/remotes/origin/0.4.15 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15617 — 1d: Request-changes revise loop — re-broadcast revised plan after reject | 2026-06-04T04:20:42.083449+00:00 | `5083a045e42c5f90366f731cfce076212c98b16f` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15623 — 1a.2: Frontend — plan approve/request-changes action layer + transport verification | 2026-06-04T03:01:20.458111+00:00 | `3acabd053770e56a435e48ac05b1f2d4337130fe` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15658 — Replace nightly memory cleanup with dream service | 2026-06-05T16:50:35.109980+00:00 | `25a7c5662cffcd910e6a8d87fc04ba1d98b7597d` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15694 — Expose gwiki trust through Gobby wiki MCP tools | 2026-06-07T22:18:08.957497+00:00 | `d6a5423446037fb17640427fecdb7fee9ab7393b` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15698 — Fix Plans-panel content (managed CLIs), approval-surface color, and mobile layout | 2026-06-07T23:59:15.885016+00:00 | `31059f808e4ca6e77f93b75d47653a97632f8c6d` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15711 — Bump version to 0.4.18 on branch 0.4.18 | 2026-06-08T23:59:23.961402+00:00 | `8fbaa32400f363641854096de26021dec7ba2f0b` | refs/heads/0.4.18 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15734 — Address CodeRabbit cleanup findings | 2026-06-09T15:03:04.271212+00:00 | `c89b1aa746c77505d0e858d3930d9c3ef92b536a` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15804 — Tighten text generation routing | 2026-06-10T17:09:21.889403+00:00 | `86fa95db31413b0e523868191622e196bba02d37` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15851 — Define the CommsMessage.channel_id contract; route platform destination via metadata | 2026-06-12T03:57:16.258719+00:00 | `3a415df1b5a49e60f747f7477b4f207a109b4eab`, `47e126480b746c60a8ac2b91e1ffd9f9d482e614` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15873 — Wire set_websocket_broadcast and fail gobby_chat sends when broadcast is unset | 2026-06-12T06:47:38.225102+00:00 | `fc89631eb4fae9332bcaccead0aa5d69c4cb4d45` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #15896 — Prevent barge-in cancellation from releasing the synthesis lock and corrupting the inference_turbo monkeypatch | 2026-07-13T03:56:29.862166+00:00 | `8f37a3d5394132af97edcf4222242054f59f8914` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15918 — Make clearing project settings fields actually clear (nulls dropped by exclude_none) | 2026-07-14T19:55:29.057282+00:00 | `eb87fff3f15df412734ea0e49abe10f6bde33c3c` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #15922 — Thread project_id through cron job creation — jobs persist unscoped and vanish | 2026-07-14T20:16:54.806126+00:00 | `cc8668c93637171c8bd0de01b71bd2605646e049` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16071 — FilesPage: failed save must not brick the editor or strand unsaved edits | 2026-07-14T19:17:10.236048+00:00 | `afe1e1a63aeb694b540cfd2b6006117899626adc` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #16507 — Log swallowed exceptions in admin _health.py instead of bare pass | 2026-07-14T06:31:48.292087+00:00 | `337b2807e1e6252c9cdc52369c535d304757db2c` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16567 — Normalize accept_edits to 'normal' in continuation restore to match the mode registry | 2026-07-14T21:39:45.451737+00:00 | `66883fd126385deca48d797163a866e306035492` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16574 — Surface cron request errors; validate cron expressions at create | 2026-07-14T21:20:37.975997+00:00 | `c0f255ff35828858344dc2a10c138b1bb9b959bc` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16582 — Add keyboard parity to clickable divs/rows/SVG across 15+ surfaces | 2026-07-14T21:58:50.921654+00:00 | `31ae7bf0ae30ecdec385058640d1edd51a86c77e` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #16608 — TaskCreateForm: confirm before backdrop/Escape destroys a typed form | 2026-07-15T00:08:16.752343+00:00 | `8440885b80a737208dcd149239e39e99ff3cead8` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16615 — Restore the focus indicator on the font-size slider | 2026-07-14T23:07:15.551111+00:00 | `dcd4644bfc5bcd312705c431a5c573c79b6e692d` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #16617 — Add tests for PipelineEditor/PipelinesTab editing paths | 2026-07-15T00:27:10.592338+00:00 | `564fc94db907167aeac3138538d49857a1cb1925` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16647 — Handle uvicorn early exit: convert server task SystemExit into graceful shutdown | 2026-07-14T13:08:26.134936+00:00 | `f608ef57200a45e1bfc1ceff3406d53858602aa2` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16652 — useFiles: key async tab updates by file identity, not positional index | 2026-07-15T01:06:32.713419+00:00 | `7e6ebab6e7ee87694493aa8c9096edeb0b3cf764` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16665 — ConfigurationPage Prompts/Variables tabs: surface failures, guard stale responses | 2026-07-15T01:20:53.771658+00:00 | `0ec7a307a8daf4b244d60685a4b0a3a62cce13cc` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16673 — Add coarse-pointer promotion to current app-shell and activity controls | 2026-07-15T00:32:20.790239+00:00 | `027d659295ee9e71ecfa2d1aa98c263be41baa65` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16685 — Align the state-token sibling sets (info/warning) with the token contract | 2026-07-15T01:35:14.688870+00:00 | `66343eb1dcd5f83fdc5db868fdb6c976ee7c8e00` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16688 — Make is_dev_mode walk parent directories per its documented contract | 2026-07-14T14:38:47.429232+00:00 | `543e41afcabc637adad9fb99f3272e1e45623772` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16695 — Stop UI-settings hydration clobbering selections made during load | 2026-07-15T01:56:42.603472+00:00 | `404411584cd142bb96cf92e2ba97b0fa060e361f` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #16697 — Delete the 25 unreachable tasks/ components (~5,600 lines) and fix the live ready/needs_review color collision | 2026-07-15T02:33:22.100617+00:00 | `fb12bf036b902e06fc0ddbd9a04f37adde1bd26d` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16739 — Fix invalid step-type badge CSS (hex alpha concatenated onto var()) | 2026-07-15T02:52:41.742854+00:00 | `3941eeb74a7d64c0266a292d361f7c219f6d4849` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #16882 — Nit sweep: agents | 2026-06-13T07:23:05.707196+00:00 | `68ce35570bed0b17e0d03f56f0dea3d92db87bdf` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17021 — Add the Rules YAML editor view | 2026-06-13T22:37:25.398642+00:00 | `6785668654baa7cb2850f3d97310c2f31159c770` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17036 — Build the Integrations activity tab | 2026-06-14T04:45:55.495533+00:00 | `f2d485d81353de3a0fcc030b48f0e80f32882ef8` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #17040 — Build the Memory activity tab | 2026-06-14T06:16:10.509707+00:00 | `d9b9e2439695536b2b59bc70ea2ce1ee7db95782` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17115 — Add join key (session_id + recall_request_id) + caller tag to recall-signal log | 2026-06-15T02:39:03.722058+00:00 | `8c1f28ac34d4f72ca2f2f2275c4b34a31129ab54` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17135 — Apply proportionality at plan altitude (plan-review over-engineering dim + plan-adversary loads it) | 2026-06-15T06:36:47.243956+00:00 | `ff45460b1264693af0957b3f6ddac460d2899318` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17324 — Add hardened AGY one-shot TEXT_GENERATE adapter | 2026-06-22T20:38:41.236061+00:00 | `a9a239887816022df7f7dc89823078242e81bd3c` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17342 — Automate per-project codewiki freshness for memory-dream truth digests | 2026-06-23T18:25:14.827597+00:00 | `233f51add862e80abe495643a3d11c7303892948` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17362 — Separate project wiki vault from active branches | 2026-06-25T14:49:18.391130+00:00 | `30bba4f2d0c9028fd1dce972a4b996b1bac139c1` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17364 — Add wiki branch setup to init and install hooks | 2026-06-25T16:04:32.108009+00:00 | `a6a0a91b6ee8562247d17dc1e399ab9af3fe82a8` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #17456 — Fix code-index maintenance schema drift for retry timestamp columns | 2026-06-30T01:41:16.030303+00:00 | `133569228396dd1af24b310f6077e436762de780` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17457 — Fix codex text-gen adapter: pass prompt via stdin/file, not argv (OSError Argument list too long on large aggregate prompts) | 2026-07-01T05:55:56.659301+00:00 | `07098af7aa2385b20e8a53b9abda2e06b219a77f` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17461 — Fix tmux window title names rejected by tmux | 2026-07-01T04:35:51.259699+00:00 | `9a6edcc43d9b347e7c46dc2ac557f477e71c6078` | refs/heads/0.4.43 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #17462 — Write Codex goal prompt for fable fixes orchestration | 2026-07-01T05:06:52.506467+00:00 | `4ee2c63b30906c1c96a1245e1507840c3e56a4bf` | refs/heads/0.4.44 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #17482 — Hold gcode project index lock across projection sync to prevent concurrent --full --sync-projections | 2026-07-02T04:22:15.928821+00:00 | `813a3db57640590fa82a56d607d892d75e706b92` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17520 — Session-start wiki overview injection rule | 2026-07-05T12:54:12.897006+00:00 | `bdda3585b083e3e4b086ae928dc76c1656575880` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #17765 — Codewiki mode | 2026-07-11T06:27:28.296291+00:00 | `77a190fdd416365da9fe7dd50a4f8dd9e45d2486` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18250 — Resolve web-components-b JIT-merge conflict in cron storage validation tests | 2026-07-14T23:11:48.197792+00:00 | `30539f3ad5240f3dba3db03d39ec55fc5c4b14d1` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #18255 — Fix websocket chat lifecycle test failures: _mark_pending_messages_delivered arity | 2026-07-15T00:20:07.838747+00:00 | `cb03962e3ff9f72f81fd5301f2e4d74fd130c738` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18258 — Rules evaluation raises bare TimeoutError under concurrent hook load | 2026-07-15T01:38:40.297095+00:00 | `c55b8d87a874e0bb4f35244c6ef8284ec230cdb2` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18267 — Fold web-core retry fork (#16695) into review-fixes/16966-web-core lane | 2026-07-15T02:03:03.880711+00:00 | `16a1cca3d2c824c09e15a51d34ff0803f8f4a9f6` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #18478 — Migrate G201 in dispatch, clones, and worktrees | 2026-07-18T06:10:04.944678+00:00 | `1e4113f440a375269bea4529520e4dd77b233a09` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18560 — Harden pg_search sanitization and backend errors | 2026-07-22T20:48:12.566586+00:00 | `13ff9ca030549881b1e196a11ecb821d5d876f1c` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18690 — Stop phantom ACP sessions and stabilize Sessions UI WebSocket | 2026-07-22T18:46:04.198827+00:00 | `298013b9cab9be22873439dbb147714da6283278` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18954 — Replace the broken root test script | 2026-07-27T00:51:34.984837+00:00 | `019e2ed3a3c0082eb3d847f406c2af6b7e41895f` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18970 — Harden idle, prompt, stall, and pane-monitor behavior | 2026-07-27T02:18:15.845422+00:00 | `79486d6f34a8dca1d55f8678bb0b789453021d06` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #18971 — Harden sandbox, spawn, and SRT runtime contracts | 2026-07-27T01:58:16.237915+00:00 | `1e17d2942005e425d9fe305db5b793515462d8fe` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #18975 — Harden embedding-switch lifecycle and contention handling | 2026-07-27T02:30:19.056725+00:00 | `b81c4a12f811a70f24993507c64ec057ee997c08` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #18984 — Integrate hardened managed sandbox startup into 0.5.0 | 2026-07-27T02:12:11.339893+00:00 | `58e05a93f77fa3e99653be3442b4eb250536b601` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #19004 — Fix CodeRabbit packet 06: Tasks, plans, and review learning | 2026-07-28T01:56:51.509958+00:00 | `7daf01cf5552fc65d2e07ffaca06016ac7db6fa5` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #19005 — Fix CodeRabbit packet 07: Workflows, skills, and orchestration | 2026-07-28T03:10:42.414703+00:00 | `f8dbf6d9291d5071636da6f233ceca75fecbd829` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #19006 — Fix CodeRabbit packet 08: MCP proxy and Hub services | 2026-07-28T04:26:56.295667+00:00 | `6d121d19bf8b8111727d503af2ea00d09dd85430` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #19007 — Fix CodeRabbit packet 09: Servers, sessions, WebSocket, and runtime boundaries | 2026-07-28T05:36:13.719769+00:00 | `e95f9be14bcd1d821de8aff8efdea73fdf3536ec` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #19133 — Reduce routine daemon INFO noise | 2026-07-28T07:14:22.932827+00:00 | `2771fbfced9e555e0cc4256ddcf78671738d7b58` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #19142 — Arbitrate synthetic tmux input safely across providers | 2026-07-28T06:59:01.278687+00:00 | `85668288c0275f4998ad59eed87bc34a9428e4b4` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #20303 — Add project_checkouts via 412_project_checkouts.sql | 2026-08-30T02:55:02.408278+00:00 | `39a6de0f1427669505c2ad959e84eee61164e285`, `74feea869d83c7ed87eceaf4b25caf882fe136e4` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #20311 — Cut over agents, sessions, and MCP tool roots | 2026-08-30T18:54:26.463693+00:00 | `bae6772c589f73e66d057a74deb5b80305d4c912` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #20312 — Cut over files, source control, plans, and workflows | 2026-08-30T22:10:57.760852+00:00 | `1cf663eca8c267bae60cc9e3203e93b2e9997e5a` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #20807 — Unwrap bare MCP content-block lists in tool_output normalization | 2026-08-23T19:33:21.945431+00:00 | `29561ebbd894a34bd05c46058f1456b9751de3a2` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #21014 — Fix stale notify_file_changed expectation in test_tool_handlers | 2026-08-26T07:19:25.009689+00:00 | `ccc0b43d9d1cc373df7a2c071b08b62092555811` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #21251 — Resolve schema 412 number collision between checkout identity and MCP templates | 2026-08-30T22:55:31.831941+00:00 | `0ebdca49dbdf8723092bda3996093d9f943dc175` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21372 — Fix isolated marker missing-path test fixture | 2026-08-31T02:07:32.035255+00:00 | `ae640711eb1738b30ed40b0eeea37964d5e976d0` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #21414 — Fix project-scoped secret upsert conflict | 2026-08-31T18:03:02.168119+00:00 | `39211c1fe71dd8a2616bcf6f320ab7f7ffafcac7` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21770 — Add handoff provenance and transcript source-range selection | 2026-09-05T05:59:10.074439+00:00 | `7e8bffd406aada6f2855786955abd55686a2d8a1` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21781 — Recover structured session work and synthesize expired summaries | 2026-09-05T07:06:25.520096+00:00 | `88e6e32668d01bf284bf7ceb85374245478fe397` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21791 — Retire summary revisions with a recoverable mechanical migration | 2026-09-05T07:59:36.416306+00:00 | `2543162900c5e2de370ce0e81aac6a2da02588a0`, `c37094fba751e72e7be9dc6675663ba6c0885359` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21792 — Expose attributable project-scoped handoff retrieval | 2026-09-05T08:25:18.402327+00:00 | `46843a72909cf5cd1d2b13e3d88732631556f755` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21793 — Select session UI summaries by lifecycle status | 2026-09-05T09:08:30.585092+00:00 | `4056b4b32f3c54c37b08ea751089097bfd18dc95` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21794 — Validate session handoff rollout and document review handoff | 2026-09-05T23:11:27.858729+00:00 | `12abc42f165a18d8f6f0ef004a7d12b985cecb03`, `20308164c0dbf5e48dff8aeba5509f1cfbcf214b`, `fa12a9de439479accd2bbcd37e0d3601821712b0` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21820 — Schedule archive-backed summary retries without starving later sessions | 2026-09-07T00:24:05.240188+00:00 | `eb53e743b4e268e42e2fd46b85aabd413f4acb6d` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21822 — Wrap overlong session lifecycle test comments and SQL | 2026-09-05T09:03:35.811259+00:00 | `eb53e743b4e268e42e2fd46b85aabd413f4acb6d` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21826 — Accept fenced structured model output during handoff recovery | 2026-09-05T14:16:32.436749+00:00 | `2a76e1d76e6bedbc8726c96532eaafb14589f7d0` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21832 — Reuse the existing summary generator and parsed transcript recovery | 2026-09-05T17:16:44.237707+00:00 | `3e1c1e95841fea08e029533bea2893f6c1a9221a` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21842 — Update prompt sync tests for retired session prompts | 2026-09-05T16:58:29.445100+00:00 | `e416a262151a9edef6609d1ae4578f32acaf8c2b` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21847 — Keep explicit session references out of handoff task evidence | 2026-09-05T17:15:11.093202+00:00 | `39d5fc517bc47f2f1558a5685087ddb01a11abcc` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21850 — Preserve recovery reference fidelity and inherited work attribution | 2026-09-05T22:59:34.788456+00:00 | `9b925f9c2739077bf9ce3a74f87aebf89d201cf7`, `d10d2f7f6fe842675fe937cfed847be3839d895d` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21860 — Give bounded recovery output a concise target below its hard limit | 2026-09-05T22:59:29.829194+00:00 | `410c9506017f3ed4c924c6ef7d36e2bdb6b5c5bc` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21867 — Exclude session objects and displayed examples from handoff task evidence | 2026-09-05T22:59:03.908021+00:00 | `103a07c698eb1e4cbcf86a05f9d213c58bf5722f` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21875 — Keep optional review candidates from inventing unfinished session work | 2026-09-05T23:00:30.710265+00:00 | `0acc7cef777035f57136a68d575653a468370b54` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21881 — Preserve operation-specific qualifiers in recovered session facts | 2026-09-05T23:00:20.431469+00:00 | `5fa4992e9e81d735f548126bb3b9848a65feb2f8` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #21886 — Preserve receipt wording and reference kinds in recovered work | 2026-09-05T22:57:28.627136+00:00 | `19ad378d4e65347ec6406b04d83597c57ed2e342` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22039 — Synthesize dream run 49640230-c32a-4547-957c-f23d36e39109 | 2026-09-10T00:30:49.775206+00:00 | `dea81c11c8c746999952a480bbed6321e0580c58`, `f7cac386b2dc5b11fe0af346ab01259555f51735` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22040 — Synthesize feedback run e3b255c6-044e-48fc-a49b-297d821d016a | 2026-09-10T01:13:46.137275+00:00 | `e9cdbca0c37dc5bc406732963a4e1c56e35b114a` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22051 — Synthesize feedback run ef48b0b1-dd58-403a-ac84-52a28675d289 | 2026-09-10T01:03:56.737436+00:00 | `26aa30ddb88fca28e26f15c6d2adb57e601dcc34`, `dbe7162df67a9f00b5df939005d849cc5b19ac70` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22069 — Synthesize dream run c19e1126-cbf4-436e-b956-ada5631bd49c | 2026-09-10T07:21:28.945200+00:00 | `8370c53dea76479567e359677c0126e909587764` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22071 — Synthesize feedback run 15052df6-3b06-450d-9f4e-aa75b4e03917 | 2026-09-10T08:22:55.151328+00:00 | `c06f8df8e42f2a1362a8f8717fd8a9de1e134ecb` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22093 — Lint policy without blanket allows | 2026-09-16T00:45:00.304696+00:00 | `26387185f291a4b4596d89b67f622de31aadf06b`, `707cbc1735d011933162899ee8f2d1d749540bd2` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #22228 — Vendor a license file for the in-tree simdutf amalgamation | 2026-09-13T17:33:24.048430+00:00 | `1621d64f027013f9c2384a607354cb524de1e167` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #22319 — Isolated test daemon health wait is too tight and reports nothing | 2026-09-14T03:20:05.487507+00:00 | `ae65650a5e80e2e237c2ef411a331cc84ab7a329` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22326 — Repair B-group test drift: deadline guidance tier, review-evidence code-index fixtures, compile-minimal deferral shape | 2026-09-14T07:27:35.535384+00:00 | `624e3623dde7e293b43892a1951686b4bd1a4bf8` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22389 — found-work owner handoff must count every successful send_message, not only parent-targeted sends | 2026-09-15T19:04:01.350371+00:00 | `486fb22d37d0460196ee7574534c53a758b8df60` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22426 — Write a chocolate chip cookie recipe reference | 2026-09-16T17:48:57.120419+00:00 | `d95a76a5054c36ed53edc7e30da467be99d47733` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22434 — Droid probe: blocker round trip | 2026-09-16T19:03:59.456636+00:00 | `31f53f3166cc4e00bee8d5469b58d1a7cd84013e` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22468 — Preserve package-manager executable resolution in RTK rewrites | 2026-09-18T02:11:24.344333+00:00 | `d750a0a953b2f10ab662c18d12a8f82a06234ff3` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps | 2026-09-28T02:58:58.569431+00:00 | `1029c2f91d0e37e6c526f7ed0f1be5fef8b9d7ea`, `207548db2e801ad49de8fc1e64619e9e2b3fb650`, `2643406ce598fe5a29d78708aa69a5c5a56f7cf4`, `433fb63b352426dbc96e1c195681d138d7b0831d`, `4aa520f2a4732454c919595c82f8a56006f09a31`, `6002c063cefa1fa861f46a51d55a6382bb7aeae1`, `748cd5151a627eaa8a053fbd6e7f0d661ab78107`, `8f184b40c575c0e6177c95752cb0c502aac5ed59`, `dd85262f3b66f6d71370fa7b2bafcc8a5239fa05`, `f788377a52fef090fee70a389f98cfce82ffa96d` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #22706 — BEFORE_AGENT workflow evaluation cancelled: find and fix the canceller, keep inbox receipts and the post-compact handoff gate intact, resubmit continuations until BEFORE_AGENT arrives | 2026-09-30T20:05:51.331459+00:00 | `0a17973545326d24419fdfaf634e4074605ff14c`, `0ebabda96226386416917cd6abd114bcb7d1b797`, `240d4d698f5bddd7dde80283b2763b3d65386f99`, `312efa5f8f171bb604b1b74b37497ed1c168cb82`, `39774017b2b21aa334dc9dda84efa764a52a6299`, `437f829bfb5a9ed966867f652fe3fc38da73819d`, `4609f492b45e2ced3b83f9926cd5d4fb82f83665`, `66c9fc46726b3197a5db64505b4858ea8b704d3c`, `9093dfb2d6c299100585632bd60a9609d3acbd20`, `b8fa20f6a339132f9791a23c907e2a82dbeb25ed`, `ba2d1c3e5453b77cf08982493e2c23284874af3e`, `bdcf9cf76117e182536226aad9d6e3be40f8517c` | refs/heads/task-22706-before-agent-cancel | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22761 — User guide | 2026-09-26T14:03:18.927292+00:00 | `4cc1aad2450ed2616867920727b61bece9f271ea` | refs/heads/docs/gclient-chrome-user-guide-22761 | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22764 — Frontend vitest suite fails under machine load on a different test each run; three pre-push attempts blocked the 0.5.0 push | 2026-09-28T07:20:23.635239+00:00 | `b5e5fe5837ce8438b8df5ae41b64d6bbdcb1549e` | refs/heads/review-r3-c22764-778bd67, refs/heads/task-22764-usechat-sessions-races | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces | 2026-10-04T03:29:35.179761+00:00 | `28509dcd7ea504fd6190a0f3d223bed3da775733`, `6b15e2c08661ef1763159bafe7345f3b3030e591`, `babceccb367c6d05d7e6cec4c10fc14cbdc55c12`, `e37c011a8631b54e95d5a35ca67d7feac976fe04`, `ffc6a61f7ddfdf684b6e07f9bf246f2ecd180a0c` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22894 — Load per-session role files from default agent profile | 2026-09-25T16:07:21.579567+00:00 | `ec9898ff6147e85c0075caba465df7a1ae82bf3e` | refs/heads/backup/task-22894-session-roles-ec989 | No captured holding worktree | Superseded; per-commit evidence below |
| #22895 — Plan runbooks as existing pipelines with placed agent launch | 2026-10-03T05:46:33.536158+00:00 | `0902d3c0b2d391b3aacd4adbc7ea19529d536bb6`, `523f487870188416b063910d65a473a2fa184ff5`, `b472e4cb9ac540a091354695103b287609c87960`, `d79046696deffcc85caf485f4f3947c47f90902d` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22918 — Prune empty attention rows and bound roster snapshots | 2026-10-03T17:54:14.060129+00:00 | `0fc7cafbf614021f17d2144852c18d4682d72008`, `28bc26cd8b77cf9c0880a45c90baac9c61cf61f1`, `3c24dc673eed541c638cb0de1b28355ce3c7c174`, `759ab55e92f07468f13017b65eaad904c8265364`, `78da6fe00d7fbfb07652499bb8c7568e4175414b`, `c84ab35b565932e83d29af0da743893089078830` | refs/heads/task-22918-ab-3788, refs/heads/task-22918-ab-8965 | No captured holding worktree | Superseded; per-commit evidence below |
| #22927 — Retire lane /goal and make Lane Manager recover stalled lanes | 2026-10-03T15:23:25.140084+00:00 | `04772b5e6437a431e15b300e2f02ebff7f342705`, `92fb1823da1ac8569f82bb088c589c062033b17a` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #22940 — Reconcile runbooks migration references with the occupied schema head | 2026-09-26T16:46:30.354595+00:00 | `769e42d910f4a884e99754a6eadd501418b1c4bb` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #22951 — Refresh reviewed front-door plan for current-code expansion | 2026-09-28T22:17:08.142308+00:00 | `04c3b8cc6d6f44b542a884a44854c53285765a4d`, `4573796764859eb434b6e7b688d766eaf19a010a`, `753fb3310286e2918224abc5ce5823f76a3ff804`, `9774514ae23be4c91b6b6c8748633c1c37bf08a9`, `c7274d2be24faa4fde01f9c68fe038f32d80c862` | refs/heads/task-22951-front-door-plan-refresh, refs/heads/task-23058-front-door-ask-retirement | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23001 — Role files point at seat definitions | 2026-10-08T17:12:21.003405+00:00 | `aa9f081623c6506d644388f9a378760b52dfa3b4` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23055 — Retire gcode Ask and gobby-ask while preserving the evidence CLI | 2026-09-29T13:17:28.609087+00:00 | `de91a6b7f6ed2cdd250f946dc5a00a3e46d5e409` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23058 — Refresh front-door plan for #23055 Ask retirement and migration renumber | 2026-09-29T00:08:54.086350+00:00 | `3792fa484a0417a6b81d1c87fa705b63a8efd7eb`, `efe2a739fd5ebabe96d1bdb1d83df6a6a5724736` | refs/heads/task-23058-front-door-ask-retirement | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23063 — UserPromptSubmit rule_engine latency: time the rule-loop bridge and bridged mcp_call effects, then fix the root cause | 2026-10-10T21:45:18.181896+00:00 | `212faa205f12314b63f15ca62496a388bc727e06` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23076 — gclient reconnects straight to the host | 2026-10-03T19:23:37.458105+00:00 | `9af079731a4e3630a8ebecbbcaab99061a3ce4a4`, `d5741fc24727471ac8949992331c550584c2ff3d`, `ecf27cff5af8cf6cafde1516f9ccde22db7ef90b` | refs/heads/r4-23076-pre-package7-efc6b0c | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23134 — Prevent task CLI guard from matching quoted heredoc data | 2026-10-05T20:18:57.913248+00:00 | `56c6ca2cca3d14d8bf13553e1645c273513c843f` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23182 — Runner lifecycle and shutdown tests log ERRORs from mis-shaped mocks | 2026-10-01T12:59:17.731073+00:00 | `2bff65e61cf980e11f941de9e5e19b5885952ae4`, `a4ba740b17837103f7ae5351c5f10b6d656375dc`, `ba18e3174ef0afa76b8ef7865b62eefd9a75da01` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan | 2026-10-01T18:08:19.497754+00:00 | `3f8d7baf3bedb75d3449e5154d40b2170864e54e`, `a30789fbec328ac6bc41519b2862c5c0ef91d81d`, `a5d275a0591cf3bc8f572b41edd6741086e3da7a`, `aaf14701926e9f7a48951c6a2861adf00211a8de`, `b9015c8be267fbe3c2e05f3e108f8623f755a81e`, `f36e62274aeecd94d2ee24307f90f6d1e612e92f` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23198 — Stop unsandboxed cargo fingerprint thrash between operator-shell and agent builds | 2026-10-01T13:48:16.721716+00:00 | `af3423ce740e1c5f7da2f7460d3c95fcd3c6a686` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23201 — Reopening a task leaves its post-close memory review demanded by the stop gate | 2026-10-01T13:39:55.475812+00:00 | `81a71a4d8088028f4dabea73cb209109bfb3f19c` | refs/heads/task-23201-reopen-memory-review | No captured holding worktree | Confirmed missing reopen withdrawal; recovery #24084 — Recover #23201 reopen memory review |
| #23208 — Epoch adoption, single-owner supervision, and restart preservation | 2026-10-08T20:50:05.024786+00:00 | `3552b143917dfb30286396de36574434ae982094`, `ce1d4061858ef1df3e064881a57433e22987e119` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23225 — Decompose the near-ceiling gclient files | 2026-10-06T07:56:38.063136+00:00 | `3a2f06df003ed314bcdca376c86812c54d97ad56` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23228 — Typed input and control path | 2026-10-06T02:41:06.421420+00:00 | `ac8f1ae9f65e68da2b6762300d1c35a8bb6d5056`, `f1a0b777dbafd323e5b0d4fe25a84024f0355276` | refs/heads/prep-23229-attach-jobs, refs/heads/task-23228-typed-input | No captured holding worktree | Superseded; per-commit evidence below |
| #23246 — Pool exhaustion diagnosis | 2026-10-01T14:27:56.478873+00:00 | `2c88be74b6f6ad12f531bf93480000f61ca06725` | refs/heads/task-23246-pool-census | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23252 — Close gate extracts a behavior artifact's command-shaped prose as a mandatory exact command | 2026-10-01T14:45:58.114902+00:00 | `0b125f0fe83a10bb05277af4ef503c647eb2b4b4`, `fb9e73ef724761da5b2b39ccadb4065d7f8dc2ad` | refs/heads/task-23252-artifact-span-not-command | No captured holding worktree | Confirmed missing artifact exclusion; recovery #24085 — Artifact-reference command exclusion |
| #23253 — route psycopg_pool.* loggers through the psycopg pool sanitizer | 2026-10-01T14:56:07.722899+00:00 | `4f09729438bbee580efc1f53fb566fbc82aa7493` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23269 — Node bootstraps refuse the Python daemon | 2026-10-01T19:18:32.908400+00:00 | `91a483db0d906938cd6adecc4c8f9ba89e90980d` | refs/heads/task-23269-node-refusal | No captured holding worktree | Confirmed missing; already tracked by #24075 — Land the node-bootstrap refusal |
| #23280 — gclient chrome: goblin eyes and halo, About braille layout, sidebar colour, worktree indicators, sentence case | 2026-10-04T20:35:43.687744+00:00 | `e73a0a043cc0e25115f233703c4a2a124d97e73b` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23303 — Rebase #22706 BEFORE_AGENT eval cancellation onto e30de69f7f and strip daemon logging | 2026-10-03T02:03:24.995278+00:00 | `e2f9674a132a1f884ce07af2d1011d181db1100e` | No captured local/remote branch | No captured holding worktree | Missing linked revision, semantic supersession not proven |
| #23304 — Found-work stop gate treats a direct pytest usage error (exit 4) as an uncoverable terminal validation failure | 2026-10-06T01:30:59.393012+00:00 | `02147d2b079e5718a9289b2e2350ed202560b6b6`, `546d4a8816a91dd7b67423025b80db1e883ea06c`, `d1aee96628b352470bc6dc6ec46828e8a2cbcc87`, `e69566e1d5c1d1ed294ffd4d286e6e0baa2f1645` | refs/heads/task-23304-usage-error-stop-gate | No captured holding worktree | Final behavior delivered; old exemption superseded by #23342 — Missing-path pytest supersession |
| #23345 — Plan semantic lint rejects plans after their leaves land | 2026-10-03T06:03:30.778697+00:00 | `241ac2a609968b7eb16a339766fb99ed765bfb82` | refs/heads/task-22727-partial-deletion-plan | No captured holding worktree | Delivered under later completion semantics; six-task evidence above |
| #23401 — Require a fresh passing vitest related run at close for changed web/src files | 2026-10-06T02:50:52.410692+00:00 | `a1e03365e7c5c9c9077814b2ec19c26a4d896e03`, `c19a7be9bd5d0f48d3bf0d8720d9fc6cfbbc668c` | refs/heads/task-23401-vitest-related | No captured holding worktree | Superseded; per-commit evidence below |
| #23437 — Rename checkout isolation to checkout_mode and remove herdr code references | 2026-10-08T08:57:37.780153+00:00 | `0a9331ed53d62b44a70a951ad5ef3a3b0fde65d9`, `11a170f910bed8338386d0a83719e0883bfb8d0a`, `80a89534e6289b582eeb1545162c1eb0b62ad507` | refs/heads/wip/23437-callback-contract | No captured holding worktree | Superseded; per-commit evidence below |
| #23519 — Shared-token cutover: gdaemon validates keys and Python trusts only the front door | 2026-10-09T19:46:12.817390+00:00 | `32658b9baf38461a0b513163569b9ad5d364e743`, `8594953482b1438254d20b0cc2395da89066a2e2`, `cc5581cd4f46a77503e2bc93b0356069eac14e72` | No captured local/remote branch | No captured holding worktree | Core authentication delivered; test-isolation correction missing; recovery #24086 — Early pytest GOBBY_HOME isolation |
| #23522 — Rollback rehearsal keeps the operator a way in | 2026-10-11T05:03:01.237625+00:00 | `c392957f18420c74784a0bec2a81fd9cbd4b1458` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23585 — Install paths ignore GOBBY_HOME: ensure_daemon_config and the Claude hook installer hardcode ~/.gobby | 2026-10-10T20:04:19.608892+00:00 | `7405e7e42c48ab6b7eba3e1e10032f299d951991` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename | 2026-10-11T00:59:25.290763+00:00 | `80d5923ae04c0432043a1706a6ea87bdd74f648a` | No captured local/remote branch | No captured holding worktree | Chrome behavior delivered; version merge superseded; six-task evidence above |
| #23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh | 2026-10-10T21:43:27.052224+00:00 | `0984d396366f2dbdd2c534cc0c43e17d1dab57be`, `3500332348a992eb19d9e84e9d4c8001ae28b6d5`, `438f21415c54dcf06fd8b4e1b6bab4ca60e1d9a7`, `cbc6d601dd6e7bee720ca9dbc1d4eaa049740b0f`, `d2be1da7bf468934b99f4cdd8da02ed37c8c9f30` | refs/heads/checkpoint-23750-0984d39, refs/heads/task-23750-c16-release, refs/heads/task-23750-reload-rebuilt | No captured holding worktree | Superseded; per-commit evidence below |
| #23754 — spawn_agent resumes an existing provider thread for crew relaunch | 2026-10-08T06:07:07.108677+00:00 | `0d903330ac95fa7b802554ee13d38c796d2afedf` | refs/heads/lane-3-runbooks-4 | No captured holding worktree | Superseded; per-commit evidence below |
| #23798 — crew-lane infers its inputs from the lane: seats and lane are the whole command | 2026-10-11T23:19:57.474263+00:00 | `3f6f8e1ab23f689695dd3d10b225c248146258a6` | refs/heads/task-23798-inferred-inputs | No captured holding worktree | Superseded; per-commit evidence below |
| #23923 — Sandboxed agents can read every same-user process's environment, including agent tokens and DATABASE_URL | 2026-10-11T02:18:47.107125+00:00 | `347fa6d85f94defb3b73dd6c2c8f2462a3cb39e4` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |
| #23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep | 2026-10-11T23:11:13.166953+00:00 | `a260885bb0b2d1d7330c9a49f0db9fd4d7a7d81b`, `a3d7fc298902e5187c72a8f6016e49757952d370` | No captured local/remote branch | No captured holding worktree | Superseded; per-commit evidence below |

## Per-commit supersession and recovery evidence

Every one of the 274 unlanded commit objects is included below. Task rows above identify ownership; these entries explain the initial sweep's revision-proof verdicts. The six-task semantic addendum above governs those six tasks' current recovery interpretation.

### `7ae3b093b3f8624e20f8f2ac53f876e0089a2425`

[gobby-#10399] Fix messages scrolling under ChatInput by adding overflow-hidden to wrapper

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/src/components/chat/ChatPage.tsx`.

### `c9e2a2937f2edb18a7042b156e350aca3ca08dfc`

[gobby-#11141] fix: update compose image tags to latest, remove password defaults, make invalidate idempotent

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `e48d495f63d888f84e7ddfa54c9e9b5832cdc531` (#11118 — [coderabbit] Backend code quality nits (B1-B16, B30-B31)); `3d9e29a929ab221db87c8f5ea9c08871e5e5a4db` (#11139 — Make /api/code-index/invalidate endpoint idempotent); `2910ed0f4431902ce97d17b24d517e59b646a226` (#11140 — Update bundled neo4j image tag to latest).

Exact path/version evidence:

- `src/gobby/data/docker-compose.services.yml`: blob `674492e2a335ce9117a42f8dde840e765fcb7fc2`, landed in `e48d495f63d888f84e7ddfa54c9e9b5832cdc531`.
- `src/gobby/servers/routes/code_index.py`: blob `e9f6e3a6bd8eee25404bad8b34410f1738b71415`, landed in `3d9e29a929ab221db87c8f5ea9c08871e5e5a4db`.
- `tests/cli/installers/test_neo4j_installer.py`: blob `abbcb14be77a17e3f3bb5d53ee86eb9a01d1c26c`, landed in `2910ed0f4431902ce97d17b24d517e59b646a226`.

### `a807c7b1bee4333d8504521de6cb89a54697788b`

[gobby-#11143] chore: drop summary column from code_symbols

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `ae803c8dcebb70ba2e748192b9bfb183d74d4bda` (#11143 — Drop summary column from code_symbols).

### `2fe1e8c83ce00245d34f7693af5817387311d726`

[gobby-#11201] fix: codex-rescue agent template mode self → interactive

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/shared/workflows/agents/codex-rescue.yaml`.

### `69c8e41bb94c13c9c1eeff96fb77d47d95d7b097`

[gobby-#11202] refactor: eliminate skill template rows from DB

Superseded: paths were retired by later landed deletions, or their exact versions landed.

Landed evidence: `0617b44ca5acbe906234981f13b8bfb524c2dce7` (#19427 — Switch Python to gdaemon; delete Python migration machinery); `f3202c70f8833d73150750de015fbf109743567b` (#11202 — Skill template refactor: eliminate source='template' DB rows).

### `1ddcd134b23c097fb8f5e2e818e69dc3e1f310aa`

[gobby-#11205] feat: doom loop detection + shadow git checkpoints

Superseded: paths were retired by later landed deletions, or their exact versions landed.

Landed evidence: `007f4f13cdef24c43f544230a684da36c635849b` (#14119 — Remove FTS5 runtime code and SQLite-specific migrations); `0617b44ca5acbe906234981f13b8bfb524c2dce7` (#19427 — Switch Python to gdaemon; delete Python migration machinery); `bd7c871faa9cad1a598ed556f4f35d403ebcff68` (#11205 — Doom loop detection + shadow git checkpoints).

### `62029ef16a78655650c0bf7f48600f5f20aa963b`

[gobby-#11214] refactor: remove session_id from stdio proxy and variable tool schemas

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/mcp_proxy/server.py`, `src/gobby/mcp_proxy/stdio.py`, `src/gobby/mcp_proxy/tools/workflows/_variables.py`, `src/gobby/servers/routes/workflows.py`, `tests/servers/routes/test_workflows.py`.

### `b9262c2fe5e568706d95a4089ca3ca1475a425a5`

[gobby-#11215] chore: remove stale session_id references from prompts, skills, and rules

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/shared/prompts/mcp/progressive-discovery.md`, `src/gobby/install/shared/skills/expand/SKILL.md`, `src/gobby/install/shared/skills/task-transitions/SKILL.md`, `src/gobby/install/shared/workflows/rules/context-handoff/inject-error-triage-policy.yaml`, `src/gobby/install/shared/workflows/rules/memory-lifecycle/require-memory-review-before-status.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-error-triage.yaml`, `src/gobby/mcp_proxy/instructions.py`, `tests/mcp_proxy/test_instructions.py`.

### `ef6038c2fb092d481b72fe5d77af1b58c31bda1f`

[gobby-#11218] feat: add is_subagent variable for native Claude Code subagent rule isolation

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `pyproject.toml`, `src/gobby/hooks/event_handlers/_agent.py`, `src/gobby/install/shared/workflows/rules/memory-lifecycle/require-memory-review-before-status.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/block-gobby-tasks-subagent.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/block-native-task-tools.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-error-triage.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-task-before-edit.yaml`, `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml`, `src/gobby/servers/websocket/chat/_session.py`, `tests/workflows/test_task_enforcement_rules.py`, `uv.lock`.

### `44efe20794c32955eb114c85039954656a4260ce`

fix: suppress ghost claude_sdk sessions and fix neo4j auth resolution

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/cli/daemon.py`, `src/gobby/install/shared/hooks/hook_dispatcher.py`, `src/gobby/llm/claude.py`, `src/gobby/llm/sdk_utils.py`, `src/gobby/mcp_proxy/importer.py`, `src/gobby/tools/summarizer.py`, `tests/llm/test_claude.py`, `tests/llm/test_claude_provider.py`, `tests/llm/test_llm_claude.py`, `tests/tools/test_tools_summarizer.py`.

### `24c4d53679310131a1c0e977389c3c17e90fb5c2`

chore: deprecate codex review gate, rescue agent, and web chat agent

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/shared/workflows/agents/codex-rescue.yaml`, `src/gobby/install/shared/workflows/agents/default-codex-web-chat.yaml`, `src/gobby/install/shared/workflows/agents/deprecated/codex-rescue.yaml`, `src/gobby/install/shared/workflows/agents/deprecated/default-codex-web-chat.yaml`, `src/gobby/install/shared/workflows/rules/stop-gates/codex-review-gate.yaml`, `src/gobby/install/shared/workflows/rules/stop-gates/deprecated/codex-review-gate.yaml`, `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml`.

### `f6d3e84b4fdf50796dfcae55729353f9f3b573b9`

refactor: remove dead executor layer, streaming code, and in_process mode

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/__init__.py`, `src/gobby/agents/kill.py`, `src/gobby/agents/lifecycle_monitor.py`, `src/gobby/agents/registry.py`, `src/gobby/agents/runner.py`, `src/gobby/agents/runner_models.py`, `src/gobby/agents/spawners/base.py`, `src/gobby/cli/agents.py`, `src/gobby/llm/__init__.py`, `src/gobby/llm/claude.py`, `src/gobby/llm/claude_executor.py`, `src/gobby/llm/claude_streaming.py`, `src/gobby/llm/codex_executor.py`, `src/gobby/llm/executor.py`, `src/gobby/llm/gemini_executor.py`, `src/gobby/llm/litellm_executor.py`, `src/gobby/llm/openai_executor.py`, `src/gobby/llm/resolver.py`, `src/gobby/mcp_proxy/stdio.py`, `src/gobby/mcp_proxy/tools/task_validation.py`, `src/gobby/runner.py`, `src/gobby/runner_init.py`, `tests/agents/test_cost_persistence.py`, `tests/agents/test_kill.py`, `tests/agents/test_lifecycle_monitor.py`, `tests/agents/test_registry.py`, `tests/agents/test_registry_extended.py`, `tests/agents/test_runner.py`, `tests/cli/test_cli_agents.py`, `tests/integration/test_agent_execution.py`, `tests/integration/test_terminal_mode_worktrees.py`, `tests/llm/test_claude.py`, `tests/llm/test_claude_executor.py`, `tests/llm/test_codex_executor.py`, `tests/llm/test_executor.py`, `tests/llm/test_gemini_executor.py`, `tests/llm/test_litellm_executor.py`, `tests/llm/test_openai_executor.py`, `tests/llm/test_resolver.py`, `tests/mcp_proxy/tools/test_tasks_validation.py`, `tests/storage/skills/test_skill_files.py`, `tests/telemetry/test_flow_instrumentation.py`, `web/src/App.tsx`, `web/src/__tests__/App.test.tsx`, `web/src/components/agents/AgentEditForm.tsx`, `web/src/components/terminals/Terminal.tsx`, `web/src/components/workflows/AgentsTab.tsx`, `web/src/hooks/useTerminal.ts`.

### `d4bda7781b97413a8855aa0e3c3d547bc508ec53`

fix: reset is_subagent on each user turn to prevent stale state

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `6c82af66c8e3a8de524f46370a42014f0aa31b46` (#11311 — Add is_subagent variable, rule guards, and integration tests).

Exact path/version evidence:

- `src/gobby/install/shared/workflows/rules/task-enforcement/reset-subagent-flag.yaml`: blob `7e0256560c40c2c0fe3fcaf0d18183de8b002c89`, landed in `6c82af66c8e3a8de524f46370a42014f0aa31b46`.

### `8ddf86c8192221da27b0aeeec92b9d1639673090`

fix: add v186 migration for agent mode renames (terminal→interactive, self→inherit)

Superseded: paths were retired by later landed deletions, or their exact versions landed.

Landed evidence: `0617b44ca5acbe906234981f13b8bfb524c2dce7` (#19427 — Switch Python to gdaemon; delete Python migration machinery); `bd7c871faa9cad1a598ed556f4f35d403ebcff68` (#11205 — Doom loop detection + shadow git checkpoints).

### `f9f31971730b6953fb44065df9f710b5af022c4c`

fix: move neo4j password to bootstrap.yaml and deprecate codex templates

Superseded: paths were retired by later landed deletions, or their exact versions landed.

Landed evidence: `1c47b33e4a53d89e629ad1109c4e691ded7a44ef` (#11301 — Phase 6: Clean up YAML templates + delete deprecated); `3c2ab4dcd6f4d64f98b52105c457e882aa0e82f4` — fix: move neo4j password to bootstrap.yaml and deprecate codex templates.

### `7829ab519325e5e00a5521850f55b2359dbf027a`

[gobby-#11231] fix: pin claude-agent-sdk<=0.1.45, move llama-cpp-python to core deps

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `pyproject.toml`.

### `6467de53b11bf7146492d78e7f45db13d555daff`

[gobby-#11232] fix: restore CLAUDECODE=1 in SDK subprocess env

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/llm/sdk_utils.py`, `uv.lock`.

### `000334f1a563c54aa03ddcebb9247125eb40ca13`

[gobby-#11235] chore: remove redundant session_id parameter from tool calls

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `GEMINI.md`, `docs/guides/mcp-tools.md`, `docs/guides/task-expansion.md`, `docs/guides/tasks.md`, `src/gobby/hooks/event_handlers/_session_start.py`, `src/gobby/install/shared/skills/build-rule/SKILL.md`, `src/gobby/install/shared/skills/deprecated/bug/SKILL.md`, `src/gobby/install/shared/workflows/deprecated/pipelines/coordinator.yaml`, `src/gobby/install/shared/workflows/deprecated/pipelines/dev-loop.yaml`, `src/gobby/install/shared/workflows/pipelines/expand-task.yaml`, `src/gobby/install/shared/workflows/pipelines/nightly-fixes.yaml`, `src/gobby/install/shared/workflows/pipelines/orchestrator.yaml`, `src/gobby/mcp_proxy/server.py`, `src/gobby/mcp_proxy/tools/task_readiness.py`, `src/gobby/mcp_proxy/tools/tasks/_crud.py`, `src/gobby/mcp_proxy/tools/tasks/_session.py`, `src/gobby/mcp_proxy/tools/workflows/_variables.py`, `tests/cli/test_pack.py`, `tests/code_index/test_summarizer.py`, `tests/hooks/test_hook_manager_extra.py`, `tests/mcp_proxy/test_top_level_variables.py`, `tests/mcp_proxy/tools/test_apply_persona.py`, `tests/mcp_proxy/tools/test_claim_task.py`, `tests/mcp_proxy/tools/test_pipelines.py`, `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py`, `tests/mcp_proxy/tools/test_tasks_coverage.py`, `tests/sessions/test_sessions_manager.py`.

### `121640b3e590d6fe7fb5f7447f5aef56b4b2ddfb`

[gobby-#11238] fix: remove source gate on handoff parent lookup and update rules

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/hooks/event_handlers/_session_end.py`, `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml`, `src/gobby/install/shared/workflows/rules/context-handoff/inject-previous-session-summary.yaml`, `tests/hooks/test_event_handlers.py`.

### `b55a28e3a16effdf74f2dad8e0f5bf3784afcbdc`

[gobby-#11235] chore: expand session_id cleanup to all internal MCP tools

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/references/session-lifecycle.yaml`, `src/gobby/mcp_proxy/server.py`, `src/gobby/mcp_proxy/services/tool_proxy.py`, `src/gobby/mcp_proxy/tools/communications.py`, `src/gobby/mcp_proxy/tools/memory.py`, `src/gobby/mcp_proxy/tools/sessions/_commits.py`, `src/gobby/mcp_proxy/tools/sessions/_crud.py`, `src/gobby/mcp_proxy/tools/sessions/_messages.py`, `src/gobby/mcp_proxy/tools/sessions/_terminal.py`, `src/gobby/mcp_proxy/tools/sessions/_transcripts.py`, `src/gobby/mcp_proxy/tools/skills/get_skill.py`, `src/gobby/mcp_proxy/tools/skills/list_skills.py`, `src/gobby/mcp_proxy/tools/skills/search_skills.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`, `src/gobby/mcp_proxy/tools/worktrees/_lifecycle.py`, `tests/mcp_proxy/test_gobby_daemon_tools.py`, `tests/mcp_proxy/tools/test_agents.py`, `tests/mcp_proxy/tools/test_tasks_coverage.py`, `uv.lock`.

### `a7a0e70f03c168ce5cf7c790e364d39985baf2c9`

[gobby-#11242] fix: remove stale session_id kwargs from set/get_variable callers

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/mcp_proxy/server.py`, `src/gobby/servers/routes/workflows.py`, `tests/mcp_proxy/test_top_level_variables.py`, `tests/mcp_proxy/tools/workflows/test_variables.py`.

### `184a1e964258fed862908989d4b9943347af7737`

[gobby-#11243] fix: add --verbose to diagnostic CLI probe

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/llm/claude.py`.

Related landed revisions (insufficient proof): `404e434428d31e98792431a25934f6c747a12624` (#11241 — Clean up stale mode='terminal' references in docs, docstrings, and YAML).

### `5a20e9e5c91c827e37b30050a34272427f642e7b`

[gobby-#11243] fix: pass --bare to SDK CLI subprocess to disable hooks

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/llm/sdk_utils.py`.

### `4c3a205eaa299b74e351b37bb66d503390189841`

[gobby-#11246] fix: add session_id param to set_variable and get_variable in stdio MCP proxy

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/mcp_proxy/stdio.py`.

### `10f6d97f93227baae493bbbb78b41ca01504735e`

[gobby-#11265] chore: bump claude-agent-sdk to >=0.1.56 for CLI 2.1.92 compatibility

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `1193d6fb8c47e479d52dee3e5890dfe158f39f6e` (#11265 — Pin claude-agent-sdk <=0.1.45 (initialize handshake broken in newer versions)).

### `9e46412e93bfb5d2119ea865d326c8ef3ac23032`

[gobby-#11322] chore: clean up remaining litellm string references in docstrings and tests

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `391f9a7877465c4a573f649a52e1cb6b5b6e0e1e` (#11322 — Clean up remaining litellm string references in docstrings and tests).

Exact path/version evidence:

- `src/gobby/llm/service.py`: blob `cda8573c5d9de9820f0d30cc00cbc183759c84a9`, landed in `391f9a7877465c4a573f649a52e1cb6b5b6e0e1e`.
- `tests/config/test_servers.py`: blob `527b0b91915b5cab09ca5a3e238016b663ffd98d`, landed in `391f9a7877465c4a573f649a52e1cb6b5b6e0e1e`.
- `tests/llm/test_claude.py`: blob `77fc64e957774e43342524b6a2ed6a0816b14ddd`, landed in `391f9a7877465c4a573f649a52e1cb6b5b6e0e1e`.
- `tests/servers/routes/test_admin_extended.py`: blob `7deb53bfcb531790b17e9826a5c4b80e4b7fe663`, landed in `391f9a7877465c4a573f649a52e1cb6b5b6e0e1e`.

### `029e42e7008aa53d36f517a4be3589a4f8152780`

[gobby-#11339] fix: Stop hook uses systemMessage instead of hookSpecificOutput

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `8e214cc567d59721f4949182e22f02192ddac988` (#11339 — Fix CodexHooksAdapter: Stop hook rejects hookSpecificOutput).

Exact path/version evidence:

- `src/gobby/adapters/codex_impl/adapter.py`: blob `6195f4e32c88eaa642e8af3c0bf855cd58fa2fb6`, landed in `8e214cc567d59721f4949182e22f02192ddac988`.

### `3d4269f9730a4cb741f3a262a88160e3669c2a29`

[gobby-#11357] refactor: remove Claude Agent SDK and associated code (Phase 7)

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `pyproject.toml`, `src/gobby/adapters/claude_code.py`, `src/gobby/adapters/codex_impl/adapter.py`, `src/gobby/adapters/gemini.py`, `src/gobby/conductor/manager.py`, `src/gobby/llm/sdk_utils.py`, `src/gobby/llm/text_utils.py`, `src/gobby/servers/chat_session.py`, `src/gobby/servers/chat_session_base.py`, `src/gobby/servers/chat_session_helpers.py`, `src/gobby/servers/chat_session_permissions.py`, `src/gobby/servers/cli_chat_session.py`, `src/gobby/servers/codex_chat_session.py`, `src/gobby/servers/codex_chat_session_permissions.py`, `src/gobby/servers/websocket/chat/_lifecycle.py`, `src/gobby/servers/websocket/chat/_messaging.py`, `tests/llm/test_sdk_utils.py`, `tests/llm/test_text_utils.py`, `tests/mcp_proxy/test_proxy_server.py`, `tests/servers/test_chat_session.py`, `tests/servers/test_chat_session_base.py`, `tests/servers/test_chat_session_helpers.py`, `tests/servers/test_chat_session_hooks.py`, `tests/servers/test_chat_session_permissions.py`, `tests/servers/test_chat_session_permissions_plan.py`, `tests/servers/test_chat_session_send_message.py`, `uv.lock`.

### `c741003136533a8a694d185e3210fd3d45117663`

[gobby-#11350] fix: format and update CodexAdapter mock path after rename

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/spawn.py`, `src/gobby/agents/spawners/command_builder.py`, `tests/cli/test_cli_install.py`, `tests/cli/test_install_coverage.py`, `tests/hooks/test_skill_manager.py`, `tests/servers/test_codex_chat_session.py`.

### `b192f2db9b1aa7f8e42ef4f0d4821e4002d5c783`

[gobby-#11389] fix: add --no-incremental --strict to mypy pre-push check

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `a9b334b94705d53b9041115d147fb1b32618199b` (#11437 — Collapse SessionSource enum and migrate backend callsites).

Exact path/version evidence:

- `.gobby/project.json`: blob `2f90c09438c61b9dbc0e2be3573342e122a0f182`, landed in `a9b334b94705d53b9041115d147fb1b32618199b`.

### `2264a2f63cf1315e2a636469cfd0888959467872`

[gobby-#11453] feat: implement CLIChatSession for Claude

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `e51d934bdc2835893ad34aba5758b92fb37997e8` (#11453 — Implement CLIChatSession for Claude).

Exact path/version evidence:

- `src/gobby/servers/cli_chat_session.py`: blob `247e0c509d8756aad54a2a7970488a087173b9f8`, landed in `e51d934bdc2835893ad34aba5758b92fb37997e8`.
- `tests/servers/test_cli_chat_session.py`: blob `9bbebc38d8c8b202d954bb53ecba23e9a3eb4919`, landed in `e51d934bdc2835893ad34aba5758b92fb37997e8`.

### `93b1d320c5c3feaa69b1440261e47c6a87ae8f33`

[gobby-#12754] fix: replay agent daemon-restart wake notifications

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `b3ac6e793c601817b706e09b85c0f57652998172` (#12754 — Implement durable wake messages for agent continuations).

### `2fb027f7010ce7faa1b675931861c0a77556576f`

[gobby-#12965] feat: show local chip on agent cards

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `14e7396cc5fa1a353c6e9ce4c7b9e39f7930ac33` (#12965 — LOCAL chip on Workflows agent cards).

Exact path/version evidence:

- `web/src/components/agents/agents.css`: blob `b9edd0ceb2811ed632d7d64696279199a97d8a22`, landed in `14e7396cc5fa1a353c6e9ce4c7b9e39f7930ac33`.
- `web/src/components/chat/styles/sessions-tab.css`: blob `a5789c8b44e1ed86735d0748dcf8cc49ddf699c1`, landed in `14e7396cc5fa1a353c6e9ce4c7b9e39f7930ac33`.
- `web/src/components/workflows/AgentsTab.tsx`: blob `5d9fbdbaf4ebc99840ee90f9f670d58d4561eb83`, landed in `14e7396cc5fa1a353c6e9ce4c7b9e39f7930ac33`.

### `69bcadbe34ba9284e80dff8c78b416c5624500b0`

[gobby-#13500] feat: add runtime dispatch mutex wrapper

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `c34221cc8029931c67c997166c796eef7677fb1b` (#13500 — [IMPL] Runtime dispatch mutex wrapper).

Exact path/version evidence:

- `src/gobby/dispatch/mutex.py`: blob `f83e34928b864eb65deae03742b8952655f87de5`, landed in `c34221cc8029931c67c997166c796eef7677fb1b`.

### `b2a40f86a7f0120fad510da88652cc98cb625426`

[gobby-#13518] feat: add active developer agent

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `e1b1fd02566c61fc2fc217790d8290279d5243ee` (#13518 — [IMPL] developer agent (active root)).

Exact path/version evidence:

- `src/gobby/dispatch/prompts.py`: blob `267c23cbbd07d2092b33d48fff14d0a3af4e2268`, landed in `e1b1fd02566c61fc2fc217790d8290279d5243ee`.
- `src/gobby/install/shared/workflows/agents/developer.yaml`: blob `24df542c97355b6445e980b84794d2168474a499`, landed in `e1b1fd02566c61fc2fc217790d8290279d5243ee`.
- `tests/agents/test_developer_definition.py`: blob `ec6758ad272cef63fb5f3a924d0823a724ec5842`, landed in `e1b1fd02566c61fc2fc217790d8290279d5243ee`.
- `tests/dispatch/test_prompts.py`: blob `b3bcfae0e4d49ba496cb7ed0145bb816a1ebaef4`, landed in `e1b1fd02566c61fc2fc217790d8290279d5243ee`.

### `a53cd5630188768cae2c83f59505cf76b8320488`

[gobby-#13900] fix: harden build automation readiness

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a6ba526e7fcd713ecdc9ca31682624e1ec6e5326` (#13900 — Prove gobby build automation readiness for expanded epics).

### `be0d25c2e3808762418e04015ae2b7ece5cfe6ad`

[gobby-#14153] feat: add task-scoped build controls

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `642f78003ecc578841f3d6e0b30d1cd308d41efe` (#14153 — Expose allow_automation toggle in update_task MCP tool and add gobby unbuild CLI).

### `50bb5188a0beb59fea8fda0b5122f1823618fd0c`

[gobby-#14364] fix: clear stopped agent task claims

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `64f14dc79ef849df37e20d784bab1979096607ac` (#14364 — fix: stopped agent runs should release task ownership consistently).

### `3fc24d8250833108911d1db73999258e144f6849`

[gobby-#14903] fix: reactivate worktrees on holistic retry

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `2ffc88f91abb0f9dce0b68e65bc46d28eba31426` (#14903 — Fix already-implemented merge detection for branch-only leaf commits).

Exact path/version evidence:

- `src/gobby/storage/tasks/_stage_state_transitions.py`: blob `09416a5f576a7b1d1b37d11dfe053bf940f9a760`, landed in `2ffc88f91abb0f9dce0b68e65bc46d28eba31426`.
- `tests/storage/tasks/test_stage_states.py`: blob `d0a673a4c7fd9ca76c1a7c2ed4cde1a1b62afe88`, landed in `2ffc88f91abb0f9dce0b68e65bc46d28eba31426`.

### `e2537a061ec46e6a55d97a53984bc7f22e5fe1ba`

[gobby-#14912] fix: align merge worker tool surface

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `143592be172c9e99c8cee06d3b998ab922ef4920` (#14912 — Align merge-worker prompts with merge step tool allowlist).

### `4f32b54d590aafaf667e06b0d2f1004252dfb142`

[gobby-#14970] fix: open task CLI against Postgres hub

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `85500c57bd52a0c9ddce5fb1bacccb8f46e265e1` (#14970 — Fix active task listing rows missing status after PostgreSQL cutover).

Exact path/version evidence:

- `src/gobby/cli/tasks/_utils/config.py`: blob `a197f71ecbb638a7afda044df7efed0fd3414f9a`, landed in `85500c57bd52a0c9ddce5fb1bacccb8f46e265e1`.
- `tests/cli/test_tasks_cli.py`: blob `8ca6a43bf04d9899c94b52d888c7397f3cec0ac7`, landed in `85500c57bd52a0c9ddce5fb1bacccb8f46e265e1`.

### `1eea88171fbf6229c91d7927c1e795b4950f5a06`

[gobby-#15052] fix: pass session id to pending message delivery rule

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `bbe8fa9dfcd0664f2dc1bc9e72fd29a2be44bde4` (#15052 — Fix hook deliver_pending_messages calls missing target_session_id).

### `5afd9980e7f60d6c7189f0ffe74adb8e7827f293`

[gobby-#15050] fix: avoid ephemeral session ids after register failure

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `778557ad6d956e99b898bcc646f3f1536a8e0291` (#15050 — Resolve injected wrapper session ids to canonical Gobby sessions).

Exact path/version evidence:

- `src/gobby/hooks/event_handlers/_session_responses.py`: blob `1e0fba9fd6efc0a3abbb21333ee31f3b54fb7ec6`, landed in `778557ad6d956e99b898bcc646f3f1536a8e0291`.
- `src/gobby/hooks/event_handlers/_session_start/flow.py`: blob `4c3d81e3d403102da55e3ca2cdce52c669b57017`, landed in `778557ad6d956e99b898bcc646f3f1536a8e0291`.
- `src/gobby/storage/sessions/_manager.py`: blob `7d9cf94767f83285e571224c1943710887c61233`, landed in `778557ad6d956e99b898bcc646f3f1536a8e0291`.
- `tests/hooks/test_session_events_coverage.py`: blob `2f33b02c2f6b833018229cea0e973ab186dd4810`, landed in `778557ad6d956e99b898bcc646f3f1536a8e0291`.
- `tests/storage/sessions/test_register_fallback.py`: blob `b3f6fc665ebc8b1d3af42515e387305462e4589a`, landed in `778557ad6d956e99b898bcc646f3f1536a8e0291`.

### `97a1830db09fadf6aaf93b84d78a8f9520e70a74`

[gobby-#15115] fix: restore daemon postgres startup

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `c61d326ef73db8210cda53ab5f2381d414f4fb5f` (#15115 — Repair local daemon bootstrap database URL).

Exact path/version evidence:

- `src/gobby/code_index/models.py`: blob `0742431b0128c730fe7a5352b3f231e95a09b8bb`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/code_index/storage.py`: blob `0787b7995c00593aed36205c5977631f473508bd`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/code_index/sync_worker.py`: blob `16789c0f71bcb4f5d5dcdc812c266a3e134b24bf`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/config/bootstrap.py`: blob `70a81f5e25f3435d3d4e156695ca457df79f3a22`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/config/postgres_bootstrap.py`: blob `a99da779ef0183fddc7b09defc906cdd281040c5`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/storage/bin_update_state.py`: blob `9a77618096bd7a8dc2429560ede7247cb16aacc8`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/storage/mcp.py`: blob `cf34f84e61702fb72cef779f73b72772c6342546`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `src/gobby/storage/memories.py`: blob `93713fa57f74aa3bd3aec9540eaf070a743a7ba4`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `tests/code_index/conftest.py`: blob `b1480a504980f2ca3a628585e7dafd2453ad7489`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `tests/code_index/test_storage.py`: blob `1fbc7dbf13575c1989ca3644f13b515bcf812e50`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `tests/code_index/test_sync_worker.py`: blob `36080e2ef9b1a3bf3f5771023467c15aaf067f5e`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `tests/config/test_bootstrap_postgres.py`: blob `54fd77bb3d7112e5c204448a387396c12cd0743f`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `tests/install/test_bin_freshness.py`: blob `1a53f2da16dbaddd3cfdb454a5441436ddb8751b`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.
- `tests/storage/test_storage_mcp.py`: blob `3ac9a2147a745d555d7f6fe5e635a9d56cbd1b43`, landed in `c61d326ef73db8210cda53ab5f2381d414f4fb5f`.

### `c01a1fd2b760f6abb8f964d663ae5ca2cb34d3fe`

[gobby-#15118] fix: remove build coordinator monitoring autoload rule

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `d2b09ad9e90a049573b0b4fc15cf3d08136188d3` (#15118 — Remove build coordinator monitoring autoload rule).

Exact path/version evidence:

- `src/gobby/install/bundled_content_manifest.json`: blob `9e5b134238c63878ba31977d0e72140906b7c58f`, landed in `d2b09ad9e90a049573b0b4fc15cf3d08136188d3`.
- `src/gobby/install/shared/workflows/rules/build-coordinator/require-build-coordinator-monitoring-skill.yaml`: blob `0000000000000000000000000000000000000000`, landed in `d2b09ad9e90a049573b0b4fc15cf3d08136188d3`.
- `tests/workflows/test_agent_monitoring_rules.py`: blob `f8ad524628b97c98f09913c8546ee9b95c11f258`, landed in `d2b09ad9e90a049573b0b4fc15cf3d08136188d3`.

### `f377dda09f7aade20e0d5a5551e05fdf6d519343`

[gobby-#15133] fix: preserve spawned Codex MCP session context

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/spawn_executor.py`, `tests/agents/test_spawn_executor.py`, `tests/mcp_proxy/test_mcp_proxy_stdio_session_context.py`.

### `dc641be8f31df9df11f77861f3f19c5747c171f0`

[gobby-#15132] fix: expose plan artifacts to isolated review agents

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/sync.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_plan_artifacts.py`, `src/gobby/workflows/condition_helpers.py`, `tests/mcp_proxy/tools/spawn_agent/test_plan_artifacts.py`.

### `4d726c9c7a0de9081882d359a044b4186dcbaef9`

[gobby-#15157] fix: make live wake prompt neutral

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `5403b38f39e8e9d3af41321b8c8fded30d5935c9` (#15157 — Make live wake prompt neutral instead of task-completed).

### `a23c46a6f1caf94dccf9072915bc45f6af5dcc6d`

[gobby-#15271] chore: remove ghook guard wrapper

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `e2317b4268c260f82dfaee5fceea6c24d25eb527` (#15271 — Remove gobby Stop hook wrapper installation).

Exact path/version evidence:

- `src/gobby/cli/installers/hook_commands.py`: blob `8e5398400cbc702be5c92829db23b4b2cb43ae9a`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `src/gobby/cli/installers/shared.py`: blob `7b6040967e2f961fecfff08df0db9ae69faecc0c`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `src/gobby/install/bundled_content_manifest.json`: blob `e7e3f1a96f264eafbd083633aa1a585d8ce017d7`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `src/gobby/install/shared/hooks/ghook_guard.py`: blob `0000000000000000000000000000000000000000`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `tests/cli/installers/test_droid.py`: blob `03ed288f3cec32c8a2f0284aaf57579afafc6ab8`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `tests/cli/installers/test_hook_commands.py`: blob `e835c869340b18eb24b6f0bed0b4a2c2f900f675`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `tests/cli/installers/test_shared.py`: blob `77a3b888cb5067459f9d984e1f69ec48320b7c13`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `tests/install/test_droid_template.py`: blob `235ae58957a0c11c96e6f76153cecffe1475c1b2`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.
- `tests/install/test_ghook_guard.py`: blob `0000000000000000000000000000000000000000`, landed in `e2317b4268c260f82dfaee5fceea6c24d25eb527`.

### `d4cf939a6dd9ef9b0e466b501c505dbfe17979c3`

[gobby-#15293] chore: prepare 0.4.11 release

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `CHANGELOG.md`, `docs/guides/code-index.md`, `docs/guides/configuration.md`, `docs/guides/http-endpoints.md`, `docs/guides/search.md`, `pyproject.toml`, `src/gobby/__init__.py`, `src/gobby/install/bundled_content_manifest.json`, `uv.lock`.

### `9d396e5531c4edd94ffb861f1ca23cd5f1f388d7`

[gobby-#15296] feat: add dispatcher dependency base guard

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/build/workspace_git.py`, `src/gobby/build/workspaces.py`, `src/gobby/dispatch/base_guard.py`, `src/gobby/dispatch/dispatcher.py`, `src/gobby/dispatch/spawn.py`, `tests/dispatch/test_dependency_base_guard.py`.

### `89bf2aabcd377de01aaf2eca22b2fc51b033a380`

[gobby-#15329] chore: bump version to 0.4.12

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `pyproject.toml`, `src/gobby/__init__.py`, `uv.lock`.

### `aaafefde75ec5f601e4b3c0e000628b113069e13`

[gobby-#15379] fix: constrain validation failure proximity

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `0ee1c24be783ad887c475784d0466e414218b180` (#15379 — Fix validation feedback false positive).

### `1da46360a856f82eb6006304e31798a422c16e89`

[gobby-#15526] fix: clarify build control output scope

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `08cf51c5620b71c3866c84ab4c17e874532878f3` (#15526 — Fix build stop leaving stopped tree automation eligible).

Exact path/version evidence:

- `src/gobby/cli/build.py`: blob `f50feded105adc4541d6eaa8aca3eb002a04e61f`, landed in `08cf51c5620b71c3866c84ab4c17e874532878f3`.
- `tests/cli/test_build.py`: blob `553b488959323c60ff92aa4d23019103a1ff3b9d`, landed in `08cf51c5620b71c3866c84ab4c17e874532878f3`.

### `dc4f91584963da2f9a7a81f31fcc6df0fa6b2278`

[gobby-#15562] chore: bump version to 0.4.15

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `pyproject.toml`, `src/gobby/__init__.py`.

### `3acabd053770e56a435e48ac05b1f2d4337130fe`

[gobby-#15623] test: cover web-chat plan approve/request-changes action layer

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `7fec17eaeaf6a770b04eabf2d6b5f1fdf73bf696` (#15623 — 1a.2: Frontend — plan approve/request-changes action layer + transport verification).

### `5083a045e42c5f90366f731cfce076212c98b16f`

[gobby-#15617] test: verify ACP request-changes -> revise re-broadcast loop

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `504415e854344f4712a95f2e09fde526b3a09609` (#15617 — 1d: Request-changes revise loop — re-broadcast revised plan after reject).

### `25a7c5662cffcd910e6a8d87fc04ba1d98b7597d`

[gobby-#15658] feat: replace memory cleanup with dream

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `7543e377978645ce89590a15f72bb67e137c23cd` (#15658 — Replace nightly memory cleanup with dream service).

### `d6a5423446037fb17640427fecdb7fee9ab7393b`

[gobby-#15694] feat: expose wiki trust tool

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `1911793c5ff50fdff187da8b0dd6fd2a1fc81e54` (#15694 — Expose gwiki trust through Gobby wiki MCP tools).

### `31059f808e4ca6e77f93b75d47653a97632f8c6d`

[gobby-#15698] fix: surface real plan for managed CLIs; restyle approval surface; mobile 2-up

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `e55cd8f34f8e321f6c1c6fd3c51b98a6e8ee2ec0` (#15698 — Fix Plans-panel content (managed CLIs), approval-surface color, and mobile layout).

### `8fbaa32400f363641854096de26021dec7ba2f0b`

[gobby-#15711] chore: bump version to 0.4.18

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `pyproject.toml`, `src/gobby/__init__.py`, `uv.lock`.

### `c89b1aa746c77505d0e858d3930d9c3ef92b536a`

[gobby-#15734] fix: address coderabbit cleanup findings

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `3905576627cf4e2151b7eb8626bf0e5644872dc8` (#15734 — Address CodeRabbit cleanup findings); `f6f6c8305f4474d22ad8ac4b5719f907be282fa0` (#15733 — Fix CodeRabbit review findings).

Exact path/version evidence:

- `.gobby/wiki/.gwiki/research-session.json`: blob `240df0caeb539846f6072e7fa7a70f34dd6a2ebe`, landed in `3905576627cf4e2151b7eb8626bf0e5644872dc8`.
- `src/gobby/agents/reasoning.py`: blob `9482eb8fcd5a638c7d21709470f789259bdcfb5c`, landed in `f6f6c8305f4474d22ad8ac4b5719f907be282fa0`.
- `tests/agents/test_reasoning.py`: blob `e103e3b5a0d2a938d766608936971efa250aba90`, landed in `f6f6c8305f4474d22ad8ac4b5719f907be282fa0`.
- `web/src/components/chat/PlanApprovalActions.tsx`: blob `bf724d9cbdc564101b0dce529167a901196cc5c2`, landed in `3905576627cf4e2151b7eb8626bf0e5644872dc8`.

### `86fa95db31413b0e523868191622e196bba02d37`

[gobby-#15804] fix: tighten text generation routing

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `98d8bb4e920b3c0aa9ee87ff0e45c5efaa614351` (#15804 — Tighten text generation routing).

### `47e126480b746c60a8ac2b91e1ffd9f9d482e614`

[gobby-#15851] fix: route comms platform IDs through metadata

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6d68cbacde13e6794951baca9b9a04fa7247af9f` (#15851 — Define the CommsMessage.channel_id contract; route platform destination via metadata).

### `3a415df1b5a49e60f747f7477b4f207a109b4eab`

[gobby-#15851] fix: route comms platform IDs through metadata

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6d68cbacde13e6794951baca9b9a04fa7247af9f` (#15851 — Define the CommsMessage.channel_id contract; route platform destination via metadata).

### `fc89631eb4fae9332bcaccead0aa5d69c4cb4d45`

[gobby-#15873] fix: wire gobby chat websocket broadcast

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/runner_lifecycle_subsystems.py`.

Related landed revisions (insufficient proof): `509971987330f702ddb849c8d71e310981f4eeb6` (#15873 — Wire set_websocket_broadcast and fail gobby_chat sends when broadcast is unset).

### `68ce35570bed0b17e0d03f56f0dea3d92db87bdf`

[gobby-#16882] fix: tighten WSL path conversion

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `a1fccec62313f0ae6ca877946e44b81ea976b4eb` (#16882 — Nit sweep: agents).

Exact path/version evidence:

- `src/gobby/agents/tmux/wsl_compat.py`: blob `a7a618ddaab7d641ecd15da2f6a226cae2134782`, landed in `a1fccec62313f0ae6ca877946e44b81ea976b4eb`.
- `tests/agents/tmux/test_wsl_compat.py`: blob `f2ed7e6d59a1b50b05a43df4ce9619aefc3739df`, landed in `a1fccec62313f0ae6ca877946e44b81ea976b4eb`.

### `6785668654baa7cb2850f3d97310c2f31159c770`

[gobby-#17021] feat: add rules yaml editor

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `c0b176a347d5dff074b49efa6b81722a62519e1a` (#17021 — Add the Rules YAML editor view).

### `f2d485d81353de3a0fcc030b48f0e80f32882ef8`

[gobby-#17036] feat: add integrations activity tab

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/src/components/activity/integrations/IntegrationsTabDetailPanel.tsx`, `web/src/components/activity/integrations/IntegrationsTabList.tsx`.

Related landed revisions (insufficient proof): `9fc3e0fb06a6814a1fbf0b6f172038f057daa954` (#17036 — Build the Integrations activity tab).

### `d9b9e2439695536b2b59bc70ea2ce1ee7db95782`

[gobby-#17040] feat: add memory activity tab

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `f6f2e43bf4216604c879052bbd8ebdab49576481` (#17040 — Build the Memory activity tab).

Exact path/version evidence:

- `web/src/components/activity/ActivityPanel.tsx`: blob `f4a5af693703d45815fb6289db7e35f5fabfc6a7`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/ActivityPanelTabs.tsx`: blob `c6e9ae61e5c9dbbefcc43f70e2c679b02de31c2a`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/MemoryTab.tsx`: blob `ef4614926f1edf8243eccbbbf6d0bb406a17fa6a`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/memory/MemoryDetailPanel.tsx`: blob `19007921cbad9d4e939be651d9633ce1d4ee9fcb`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/memory/MemoryTabActions.ts`: blob `889d8f5c706091997bfadb96dbbbf44d431dcc09`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/memory/MemoryTabData.ts`: blob `2cda6b574f3c06aed885ba2b3d6237eee1059c06`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/memory/MemoryTabList.tsx`: blob `870b5daab2f910e3a60b7c52822e20edf8123016`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/memory/__tests__/MemoryTab.test.tsx`: blob `0a0cbff6391707fa9c9783bb4fb112fe37e4ea46`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/components/activity/useActivityPanel.ts`: blob `0b7591fd02edb35f17205b100c7cfb8a41845384`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.
- `web/src/hooks/useMemory.ts`: blob `20bc83b645f17b391e21993b88120a4cdd98f28a`, landed in `f6f2e43bf4216604c879052bbd8ebdab49576481`.

### `8c1f28ac34d4f72ca2f2f2275c4b34a31129ab54`

[gobby-#17115] feat: add recall signal join metadata

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `bdbbe98aa40a5d125b2e53862f87fbde8165387c` (#17115 — Add join key (session_id + recall_request_id) + caller tag to recall-signal log).

### `ff45460b1264693af0957b3f6ddac460d2899318`

[gobby-#17135] feat: apply proportionality at plan altitude (over-engineering dim + adversary loads it)

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `27c8a7461249f278ef507c84329a8183eeefe80b` (#17135 — Apply proportionality at plan altitude (plan-review over-engineering dim + plan-adversary loads it)).

### `26b698d55bfa40001d2b23eccb4998f0cbfe4c05`

[gobby-#15039] fix: refresh grok and agy fidelity evidence

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `5f1e7cabbf173e91c0511aea8628be5acf90d9f2` (#15039 — Refresh adapter fidelity after AGY and Grok integration).

### `a9a239887816022df7f7dc89823078242e81bd3c`

[gobby-#17324] feat: add AGY one-shot text generation

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `728e7169d7be51311e5aa56a765b226ffb391a7c` (#17324 — Add hardened AGY one-shot TEXT_GENERATE adapter).

### `233f51add862e80abe495643a3d11c7303892948`

[gobby-#17342] feat: refresh codewiki for every memory-bearing project

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `9f152a939b95a9c9fdf2ba995c43a7cc75a20047` (#17342 — Automate per-project codewiki freshness for memory-dream truth digests).

### `30bba4f2d0c9028fd1dce972a4b996b1bac139c1`

[gobby-#17362] chore: separate wiki vault from active branches

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `285a24e9108d41479e292ed87e4d900d9db721cc` (#17362 — Separate project wiki vault from active branches).

### `a6a0a91b6ee8562247d17dc1e399ab9af3fe82a8`

[gobby-#17364] feat: add wiki branch publishing setup

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/cli/installers/test_git_hooks_installer.py`, `tests/cli/test_cli_init.py`.

Related landed revisions (insufficient proof): `d1ae71eb55c48aea6df87e4e6aeb6ccd6d76c954` (#17364 — Add wiki branch setup to init and install hooks).

### `133569228396dd1af24b310f6077e436762de780`

[gobby-#17456] fix: repair code-index schema contract

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6c505103a1df6cbbceda02c7ce80ad1da6a9f3a2` (#17456 — Fix code-index maintenance schema drift for retry timestamp columns).

### `9a6edcc43d9b347e7c46dc2ac557f477e71c6078`

[gobby-#17461] fix: sanitize tmux window titles

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/tmux/session_manager.py`, `src/gobby/agents/tmux/titles.py`, `src/gobby/workflows/summary_actions.py`, `tests/agents/test_tmux.py`, `tests/workflows/test_summary_actions.py`.

### `4ee2c63b30906c1c96a1245e1507840c3e56a4bf`

[gobby-#17462] docs: add fable fixes goal prompt

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/goals/fable-fixes.md`.

### `07098af7aa2385b20e8a53b9abda2e06b219a77f`

[gobby-#17457] fix: pass codex tool-chat prompt via stdin

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `97f67b4d6f91253f08d62095e5b51baeaad394cc` (#17457 — Fix codex text-gen adapter: pass prompt via stdin/file, not argv (OSError Argument list too long on large aggregate prompts)).

### `813a3db57640590fa82a56d607d892d75e706b92`

[gobby-#17482] fix: hold gcode index lock across projection sync

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `957675d67ddec3b487eada2018139081300831b5` (#17482 — Hold gcode project index lock across projection sync to prevent concurrent --full --sync-projections).

Exact path/version evidence:

- `crates/gcode/src/commands/index.rs`: blob `4c77c926297210698d4dffc0d3cf0bccec032ce9`, landed in `957675d67ddec3b487eada2018139081300831b5`.

### `bdda3585b083e3e4b086ae928dc76c1656575880`

[gobby-#17520] feat: session-start wiki overview injection rule

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `2206ffdc3508444b25a37f9a95fccb1545d6ac07` (#17520 — Session-start wiki overview injection rule).

Exact path/version evidence:

- `src/gobby/hooks/event_handlers/_session_start/agents.py`: blob `5d5e5cd271dade137d072e75c3eb9d9c23f8ddfa`, landed in `2206ffdc3508444b25a37f9a95fccb1545d6ac07`.
- `src/gobby/hooks/event_handlers/_session_start/flow.py`: blob `831a6df3eef89cd12f0bb9f74a615e006efe3d37`, landed in `2206ffdc3508444b25a37f9a95fccb1545d6ac07`.
- `src/gobby/install/bundled_content_manifest.json`: blob `900fe0065fe16a3b67d2e9da0b13e3e2847b4999`, landed in `2206ffdc3508444b25a37f9a95fccb1545d6ac07`.
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml`: blob `869b7f791d637a7ea61052c33ea91965ab0ba1f0`, landed in `2206ffdc3508444b25a37f9a95fccb1545d6ac07`.
- `tests/hooks/event_handlers/test_wiki_overview_seed.py`: blob `19d576a02f26ef5f7f4ba5bc6cc404e3d52c72c3`, landed in `2206ffdc3508444b25a37f9a95fccb1545d6ac07`.

### `77a190fdd416365da9fe7dd50a4f8dd9e45d2486`

[gobby-#17765] feat: complete codewiki mode with promoted roots and freshness strip

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `ae583ae11d52b03309291a632239ab078e9b48bf` (#17765 — Codewiki mode).

### `8f37a3d5394132af97edcf4222242054f59f8914`

[gobby-#15896] fix: serialize chatterbox inference after cancellation

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `eb8340aedbbc4b73e6d127a5c056301f036381d2` (#15896 — Prevent barge-in cancellation from releasing the synthesis lock and corrupting the inference_turbo monkeypatch).

### `337b2807e1e6252c9cdc52369c535d304757db2c`

[gobby-#16507] test: cover health diagnostic failures

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `dcdb212a0a4a3566dbe1cdff9d5442f2361e7684` (#16507 — Log swallowed exceptions in admin _health.py instead of bare pass).

### `f608ef57200a45e1bfc1ceff3406d53858602aa2`

[gobby-#16647] test: cover graceful shutdown after bind failure

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `ac7641b824564777909296cc329bf2a9e5c8a694` (#16647 — Handle uvicorn early exit: convert server task SystemExit into graceful shutdown).

### `543e41afcabc637adad9fb99f3272e1e45623772`

[gobby-#16688] fix: detect dev mode from repo subdirectories

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `0b4d99850cb7bfba45dbd859b5dfcef93a5ae5cc` (#16688 — Make is_dev_mode walk parent directories per its documented contract).

Exact path/version evidence:

- `src/gobby/utils/dev.py`: blob `3e9dd44740a59a60bed004ce6f8c9a69c15f2b4c`, landed in `0b4d99850cb7bfba45dbd859b5dfcef93a5ae5cc`.
- `tests/utils/test_dev.py`: blob `432881bbc4a440781022e57a4b6b967238ffa50e`, landed in `0b4d99850cb7bfba45dbd859b5dfcef93a5ae5cc`.

### `afe1e1a63aeb694b540cfd2b6006117899626adc`

[gobby-#16071] fix: preserve file edits after save failures

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/vite.config.ts`.

Related landed revisions (insufficient proof): `d1ef17cbab0f4e9b7c1ff2a358f70ab77a3b6a1b` (#16071 — FilesPage: failed save must not brick the editor or strand unsaved edits).

### `eb87fff3f15df412734ea0e49abe10f6bde33c3c`

[gobby-#15918] fix: preserve explicit null project updates

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `d58d1ad15309eb9893421a13118484d85919f346` (#15918 — Make clearing project settings fields actually clear (nulls dropped by exclude_none)).

### `cc8668c93637171c8bd0de01b71bd2605646e049`

[gobby-#15922] fix: scope cron job creation to project

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `f5cd3957086a00d2a68c3b6fcd973bb6ac03ac2e` (#15922 — Thread project_id through cron job creation — jobs persist unscoped and vanish).

### `c0f255ff35828858344dc2a10c138b1bb9b959bc`

[gobby-#16574] fix: reject invalid cron expressions at create

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `ba8e3b20fd3e283a8534cac370436f8b8c2e489b` (#16574 — Surface cron request errors; validate cron expressions at create).

### `66883fd126385deca48d797163a866e306035492`

[gobby-#16567] fix: normalize restored legacy chat mode

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `d72e57c65cc905d8501eccdc31fbe78da8b7c0cd` (#16567 — Normalize accept_edits to 'normal' in continuation restore to match the mode registry).

### `31ae7bf0ae30ecdec385058640d1edd51a86c77e`

[gobby-#16582] fix: add keyboard parity to live web surfaces

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/src/components/activity/FilesTab.tsx`, `web/src/components/activity/SessionsTabMenu.tsx`, `web/src/components/activity/__tests__/ActivityPanel.test.tsx`, `web/src/components/activity/__tests__/FilesTab.test.tsx`.

Related landed revisions (insufficient proof): `169bffaddb3ccf94404774826c265f045576e2c5` (#16582 — Add keyboard parity to clickable divs/rows/SVG across 15+ surfaces).

### `dcd4644bfc5bcd312705c431a5c573c79b6e692d`

[gobby-#16615] fix: restore slider focus indicator

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/vite.config.ts`.

Related landed revisions (insufficient proof): `6019aa7a68221fe828bc89af72fad02569ee62f4` (#16615 — Restore the focus indicator on the font-size slider).

### `30539f3ad5240f3dba3db03d39ec55fc5c4b14d1`

Merge branch '0.5.0' into review-fixes/16965-web-components-b

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. This is a merge with nonempty resolution or an unlanded baseline; ordinary cherry ignores merge patches.

### `8440885b80a737208dcd149239e39e99ff3cead8`

[gobby-#16608] fix: confirm before discarding task drafts

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `8c3af8d15555d6404b694562f9dc9807f388c415` (#16608 — TaskCreateForm: confirm before backdrop/Escape destroys a typed form).

### `cb03962e3ff9f72f81fd5301f2e4d74fd130c738`

[gobby-#18255] fix: align websocket lifecycle test contracts

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `aef78d79c7110d1f98ee9d91978722348ac73777` (#17962 — Make Codex the only daemon-down fail-closed Stop hook in external ghook); `feede5d1066b1f371ded7d3bffd064f7a639f2cd` (#18255 — Fix websocket chat lifecycle test failures: _mark_pending_messages_delivered arity).

Exact path/version evidence:

- `src/gobby/servers/websocket/chat/_session.py`: blob `4f0c8f98e5c2a91649526b8cfa5e485c30460720`, landed in `aef78d79c7110d1f98ee9d91978722348ac73777`.
- `src/gobby/servers/websocket/chat/backends/codex.py`: blob `890cecebac7403ece662a93540f920b13fc50723`, landed in `aef78d79c7110d1f98ee9d91978722348ac73777`.
- `src/gobby/servers/websocket/chat/backends/codex_turns.py`: blob `634e26884ebe41ee12789282eb04082b7f34e4c1`, landed in `aef78d79c7110d1f98ee9d91978722348ac73777`.
- `tests/servers/websocket/chat/test_runtime_manager.py`: blob `10bee451cc67065313badbd00d54fd1d84d37ead`, landed in `feede5d1066b1f371ded7d3bffd064f7a639f2cd`.
- `tests/servers/websocket/chat/test_session_registry.py`: blob `cf33d97a6210bc6b7c6e8ce49b39d07cad7b8f3c`, landed in `feede5d1066b1f371ded7d3bffd064f7a639f2cd`.

### `564fc94db907167aeac3138538d49857a1cb1925`

[gobby-#16617] test: cover pipeline editor failure paths

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `10790f70ec5730dc10dfb81d461e6eada0c7b52c` (#16617 — Add tests for PipelineEditor/PipelinesTab editing paths).

Exact path/version evidence:

- `web/src/components/activity/pipelines/__tests__/PipelineEditor.test.tsx`: blob `dee676d5a3dee85d1559401e06827a99f0dbba12`, landed in `10790f70ec5730dc10dfb81d461e6eada0c7b52c`.
- `web/src/components/activity/pipelines/__tests__/PipelinesDefs.test.tsx`: blob `3af8836c63bb3c357ded2f8195010533c9e4044d`, landed in `10790f70ec5730dc10dfb81d461e6eada0c7b52c`.

### `027d659295ee9e71ecfa2d1aa98c263be41baa65`

[gobby-#16673] fix: promote app activity touch targets

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `d18c92650e2790ba5543c87ad5a54bd79577836f` (#16673 — Add coarse-pointer promotion to current app-shell and activity controls).

### `7e6ebab6e7ee87694493aa8c9096edeb0b3cf764`

[gobby-#16652] fix: key async file saves by identity

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `f28b021bc13fbdf0149061541afb3bf45444d32e` (#16652 — useFiles: key async tab updates by file identity, not positional index).

### `0ec7a307a8daf4b244d60685a4b0a3a62cce13cc`

[gobby-#16665] fix: guard settings configuration requests

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `c66f47c92129e319aa68d96227aadd409259220f` (#16665 — ConfigurationPage Prompts/Variables tabs: surface failures, guard stale responses).

### `66343eb1dcd5f83fdc5db868fdb6c976ee7c8e00`

[gobby-#16685] fix: align state token sibling contract

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `34ed6ff49db377b6ca550afc3ccfd8f3e0370ff2` (#16685 — Align the state-token sibling sets (info/warning) with the token contract).

### `c55b8d87a874e0bb4f35244c6ef8284ec230cdb2`

[gobby-#18258] fix: isolate hook evaluation timeouts

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `715e6b5dad4ac88d6185a65fbf9d56c83c8e06e5` (#18258 — Rules evaluation raises bare TimeoutError under concurrent hook load).

### `404411584cd142bb96cf92e2ba97b0fa060e361f`

[gobby-#16695] fix: preserve UI selections during hydration

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/vite.config.ts`.

Related landed revisions (insufficient proof): `e87998c1399a3fe149184da14e0749e03c97c07c` (#16695 — Stop UI-settings hydration clobbering selections made during load).

### `16a1cca3d2c824c09e15a51d34ff0803f8f4a9f6`

[gobby-#16695] fix: preserve UI selections during hydration

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/src/App.tsx`, `web/src/components/app/__tests__/useAppProjectSelection.test.tsx`, `web/src/components/app/useAppProjectSelection.ts`, `web/vite.config.ts`.

### `fb12bf036b902e06fc0ddbd9a04f37adde1bd26d`

[gobby-#16697] refactor: remove unreachable task components

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `63bdd3575c49319fc0c0238c37d1dc71175ee431` (#16697 — Delete the 25 unreachable tasks/ components (~5,600 lines) and fix the live ready/needs_review color collision).

### `3941eeb74a7d64c0266a292d361f7c219f6d4849`

[gobby-#16739] fix: use valid step badge tint

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `253fb146af458d71b95d987c7e66fed3d732fc82` (#16739 — Fix invalid step-type badge CSS (hex alpha concatenated onto var())).

### `1e4113f440a375269bea4529520e4dd77b233a09`

[gobby-#18478] refactor: migrate dispatch G201 logging

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `52759f06fdecab510840955889575ecf431e6b90` (#18478 — Migrate G201 in dispatch, clones, and worktrees).

### `298013b9cab9be22873439dbb147714da6283278`

[gobby-#18690] fix: stop phantom sessions and replace WebSocket bridge

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `e05caec463bbe97a0e06cc1454f4df1710ed3001` (#18690 — Stop phantom ACP sessions and stabilize Sessions UI WebSocket).

### `13ff9ca030549881b1e196a11ecb821d5d876f1c`

[gobby-#18560] fix: harden pg_search query sanitization

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `8e78486ae1c320833b8f708a3ff5b16b5734d3e8` (#18560 — Harden pg_search sanitization and backend errors).

Exact path/version evidence:

- `crates/gcode/src/search/fts/tests.rs`: blob `ee0ee2640471c083b592f3d13df88026e2c5a272`, landed in `8e78486ae1c320833b8f708a3ff5b16b5734d3e8`.
- `crates/gcore/src/search.rs`: blob `c4fd5362adcac02d0d0631c491abded46e2459b3`, landed in `8e78486ae1c320833b8f708a3ff5b16b5734d3e8`.
- `crates/gwiki/src/search/bm25.rs`: blob `9431f1e2c01c458035abb268bcf1755ceee6f91d`, landed in `8e78486ae1c320833b8f708a3ff5b16b5734d3e8`.
- `crates/gwiki/src/search/mod.rs`: blob `fb7c9fd9e31e11a9f7c8eea2262169b4210ffb4d`, landed in `8e78486ae1c320833b8f708a3ff5b16b5734d3e8`.

### `019e2ed3a3c0082eb3d847f406c2af6b7e41895f`

[gobby-#18954] fix: delegate root tests to web suite

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `6201e2a2a6948d0fa5c31561efce0964928863a8` (#19100 — Integrate audited CodeRabbit fixes and remove stale worktrees).

Exact path/version evidence:

- `package.json`: blob `9b76c47ccf6e98f4e0f661fe6d0a9445c7c776da`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.

### `79486d6f34a8dca1d55f8678bb0b789453021d06`

[gobby-#18970] fix: harden agent pane monitoring

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/idle_check_handler.py`, `src/gobby/agents/lifecycle_monitor.py`, `src/gobby/agents/prompt_detector.py`, `src/gobby/agents/stall_classifier.py`, `src/gobby/agents/terminal_prompt_monitor.py`, `src/gobby/agents/tmux/pane_monitor.py`, `tests/agents/test_attention_state.py`, `tests/agents/test_prompt_detector.py`, `tests/agents/test_stall_classifier.py`, `tests/agents/test_terminal_prompt_monitor.py`, `tests/agents/tmux/test_pane_monitor.py`, `tests/agents/tmux/test_pane_monitor_registry.py`.

### `1e17d2942005e425d9fe305db5b793515462d8fe`

[gobby-#18971] fix: harden managed sandbox startup

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/sandbox.py`, `src/gobby/agents/sandbox_policy.py`, `src/gobby/agents/spawn_executor.py`, `src/gobby/agents/spawn_models.py`, `src/gobby/agents/srt_runner.mjs`, `src/gobby/agents/srt_runtime.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`, `tests/agents/test_srt_runtime.py`, `tests/agents/test_srt_spawn.py`.

### `58e05a93f77fa3e99653be3442b4eb250536b601`

[gobby-#18971] fix: harden managed sandbox startup

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/agents/sandbox.py`, `src/gobby/agents/sandbox_policy.py`, `src/gobby/agents/spawn_executor.py`, `src/gobby/agents/spawn_models.py`, `src/gobby/agents/srt_runner.mjs`, `src/gobby/agents/srt_runtime.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`, `tests/agents/test_srt_runtime.py`, `tests/agents/test_srt_spawn.py`.

### `b81c4a12f811a70f24993507c64ec057ee997c08`

[gobby-#18975] fix: harden embedding switch lifecycle

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `6201e2a2a6948d0fa5c31561efce0964928863a8` (#19100 — Integrate audited CodeRabbit fixes and remove stale worktrees).

Exact path/version evidence:

- `src/gobby/ai/embedding_switch.py`: blob `0b3be438fbc484a52ac1fd5304b498e0f2987f1b`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `src/gobby/ai/embedding_switch_runner.py`: blob `44f5778cbd1141dd4789fc18fff360a62ec166e0`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `src/gobby/ai/embedding_switch_service.py`: blob `932fba7e5edcc083ddb284c00c119ea1b3c5e0a3`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `src/gobby/servers/routes/embeddings.py`: blob `31df87efb5109596d3b951cacae9484da4f5cce7`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `src/gobby/storage/config_store.py`: blob `71c4606c5f0a7d9d6ffa2d853738da2b32048e67`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `tests/ai/test_embedding_switch.py`: blob `d270257c53df1099083008537c55c074f93c793f`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `tests/ai/test_embedding_switch_daemon_lifecycle.py`: blob `a33e9ee29e0e9d4bb1dcaecd23c948bbd059f5c2`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `tests/ai/test_embedding_switch_runner.py`: blob `92084136d8e126693ed8e3a57df2aaa6a425a9de`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `tests/servers/routes/test_embeddings_routes.py`: blob `f92b0afc510d8bf8db884800c6ae4f6aca2e1c9d`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.
- `tests/storage/test_embedding_switch_config_contract.py`: blob `692562f51ac132dad24502d1610ae62939853ea3`, landed in `6201e2a2a6948d0fa5c31561efce0964928863a8`.

### `7daf01cf5552fc65d2e07ffaca06016ac7db6fa5`

[gobby-#19004] fix: harden task and plan review contracts

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/coderabbit-fixes/06-tasks-plans-review-learning.md`, `src/gobby/mcp_proxy/tools/tasks/_plan_review_approval.py`, `src/gobby/mcp_proxy/tools/tasks/_plan_review_backfill.py`, `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`, `src/gobby/plans/digests.py`, `src/gobby/plans/manifest_emitter.py`, `src/gobby/plans/review_coverage.py`, `src/gobby/plans/review_evidence.py`, `src/gobby/plans/review_evidence_io.py`, `src/gobby/plans/review_findings.py`, `src/gobby/review_learning/class_recall.py`, `src/gobby/review_learning/recorders.py`, `src/gobby/review_learning/round_diff.py`, `src/gobby/storage/tasks/_manager.py`, `src/gobby/storage/tasks/_stage_states.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/storage/tasks/_transitions_facade.py`, `src/gobby/tasks/commits.py`, `src/gobby/tasks/criteria_contract.py`, `tests/mcp_proxy/tools/test_plan_review_approval.py`, `tests/mcp_proxy/tools/test_plan_review_backfill.py`, `tests/plans/test_manifest_emitter.py`, `tests/plans/test_review_coverage.py`, `tests/plans/test_review_evidence.py`, `tests/plans/test_review_evidence_io.py`, `tests/review_coverage_helpers.py`, `tests/review_learning/test_class_recall.py`, `tests/review_learning/test_feedback_loop_e2e.py`, `tests/review_learning/test_lessons.py`, `tests/review_learning/test_recall_context.py`, `tests/review_learning/test_retirement.py`, `tests/review_learning/test_round_diff.py`, `tests/storage/tasks/test_manager.py`, `tests/storage/tasks/test_sweep_stale_claims.py`, `tests/storage/test_stage_review_findings.py`, `tests/tasks/test_commits.py`, `tests/tasks/test_criteria_contract.py`.

### `f8dbf6d9291d5071636da6f233ceca75fecbd829`

[gobby-#19005] fix: harden workflow and skill orchestration

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/coderabbit-fixes/07-workflows-skills-orchestration.md`, `src/gobby/adapters/claude_code.py`, `src/gobby/config/skills.py`, `src/gobby/dispatch/prompts.py`, `src/gobby/dispatch/spawn.py`, `src/gobby/review_learning/lessons.py`, `src/gobby/review_learning/service.py`, `src/gobby/runner_init/servers.py`, `src/gobby/runner_init/storage.py`, `src/gobby/runner_lifecycle_subsystems.py`, `src/gobby/runner_maintenance.py`, `src/gobby/skills/hubs/github_topic.py`, `src/gobby/skills/hubs/manager.py`, `src/gobby/storage/pipeline_history.py`, `src/gobby/storage/pipeline_subscribers.py`, `src/gobby/workflows/condition_helpers.py`, `src/gobby/workflows/state_manager.py`, `src/gobby/workflows/summary_actions.py`, `tests/dispatch/test_dispatcher.py`, `tests/dispatch/test_prompts.py`, `tests/runner_init/test_detection_registry_composition.py`, `tests/skills/hubs/test_github_topic.py`, `tests/skills/hubs/test_manager.py`, `tests/skills/test_development_discipline_skill.py`, `tests/skills/test_epic_review_skill.py`, `tests/skills/test_removed_wait_tool_guidance.py`, `tests/skills/test_review_learning_skill.py`, `tests/storage/test_pipeline_storage.py`, `tests/test_runner_lifecycle_subsystems.py`, `tests/test_runner_maintenance_offload.py`, `tests/workflows/test_active_progressive_discovery_guidance.py`, `tests/workflows/test_condition_helpers.py`, `tests/workflows/test_context_handoff_rules.py`, `tests/workflows/test_memory_lifecycle_rules.py`, `tests/workflows/test_memory_recall_gate_rules.py`, `tests/workflows/test_progressive_discovery_rules.py`, `tests/workflows/test_review_learning_rules.py`, `tests/workflows/test_rewrite_rules.py`, `tests/workflows/test_session_variable_manager.py`, `tests/workflows/test_skill_loaded_call_tool_path.py`, `tests/workflows/test_step_enforcement.py`, `tests/workflows/test_summary_actions.py`.

### `2771fbfced9e555e0cc4256ddcf78671738d7b58`

[gobby-#19135] fix: bound related evidence before database gating

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/memory/dream/related.py`, `tests/memory/test_dream_related.py`.

Related landed revisions (insufficient proof): `cc82f7a5a395a0fa033c5bdfa8cf1376b4ced824` (#19133 — Reduce routine daemon INFO noise).

### `6d121d19bf8b8111727d503af2ea00d09dd85430`

[gobby-#19006] fix: harden MCP proxy and Hub services

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/coderabbit-fixes/08-mcp-proxy-hub-services.md`, `src/gobby/mcp_proxy/connection_cleanup.py`, `src/gobby/mcp_proxy/server.py`, `src/gobby/mcp_proxy/services/recommendation.py`, `src/gobby/mcp_proxy/services/result_offload.py`, `src/gobby/mcp_proxy/services/tool_execution.py`, `src/gobby/mcp_proxy/stdio_proxy.py`, `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py`, `src/gobby/mcp_proxy/tools/agents_query_tools.py`, `src/gobby/mcp_proxy/tools/communications.py`, `src/gobby/mcp_proxy/tools/memory_recall.py`, `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/mcp_proxy/tools/results.py`, `src/gobby/mcp_proxy/tools/sessions/_registration.py`, `src/gobby/mcp_proxy/tools/skills/install_skill.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_health.py`, `src/gobby/storage/hub/async_ops.py`, `src/gobby/storage/hub/postgres.py`, `src/gobby/storage/hub/postgres_pool.py`, `src/gobby/storage/sessions/_bulk_update.py`, `src/gobby/storage/sessions/_title_defaults.py`, `src/gobby/tasks/state_semantics.py`, `tests/completion_delivery_helpers.py`, `tests/mcp_proxy/result_offload_test_support.py`, `tests/mcp_proxy/services/test_recommendation_coverage.py`, `tests/mcp_proxy/services/test_result_offload.py`, `tests/mcp_proxy/services/test_tool_proxy_coverage.py`, `tests/mcp_proxy/test_error_tracking_integration.py`, `tests/mcp_proxy/test_gobby_daemon_tools.py`, `tests/mcp_proxy/test_manager_disconnect_cancellation.py`, `tests/mcp_proxy/test_mcp_proxy_stdio.py`, `tests/mcp_proxy/test_results_tools.py`, `tests/mcp_proxy/test_stdio_proxy.py`, `tests/mcp_proxy/test_wait_for_output.py`, `tests/mcp_proxy/tool_capture.py`, `tests/mcp_proxy/tools/sessions/test_compact_self.py`, `tests/mcp_proxy/tools/skills/test_install_skill.py`, `tests/mcp_proxy/tools/spawn_agent/test_dedup.py`, `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py`, `tests/mcp_proxy/tools/spawn_agent/test_health.py`, `tests/mcp_proxy/tools/spawn_agent/test_local_endpoint.py`, `tests/mcp_proxy/tools/test_agent_cancellation.py`, `tests/mcp_proxy/tools/test_agents.py`, `tests/mcp_proxy/tools/test_communications.py`, `tests/mcp_proxy/tools/test_memory_recall.py`, `tests/mcp_proxy/tools/test_plan_review_evidence_errors.py`, `tests/mcp_proxy/tools/test_review_learning.py`, `tests/mcp_proxy/tools/test_tasks_create_coverage.py`, `tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py`, `tests/storage/hub/test_async_ops.py`, `tests/storage/hub/test_postgres_placeholder_remap.py`, `tests/storage/hub/test_protocol_contract.py`, `tests/storage/sessions/test_registration.py`, `tests/storage/test_manager_surface_parity.py`.

### `e95f9be14bcd1d821de8aff8efdea73fdf3536ec`

[gobby-#19007] fix: harden server and session runtime boundaries

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/coderabbit-fixes/09-servers-sessions-runtime.md`, `src/gobby/agents/tmux/session_manager.py`, `src/gobby/app_context.py`, `src/gobby/deployment.py`, `src/gobby/runner_init/servers.py`, `src/gobby/servers/_app_ui.py`, `src/gobby/servers/routes/attention.py`, `src/gobby/servers/routes/configuration_effective.py`, `src/gobby/servers/routes/configuration_generation_endpoints.py`, `src/gobby/servers/routes/configuration_values.py`, `src/gobby/servers/routes/providers.py`, `src/gobby/servers/routes/sessions/lifecycle.py`, `src/gobby/servers/websocket/asgi_adapter.py`, `src/gobby/servers/websocket/broadcast.py`, `src/gobby/servers/websocket/chat/backends/codex.py`, `src/gobby/servers/websocket/chat/backends/codex_turns.py`, `src/gobby/servers/websocket/chat/runtime_manager.py`, `src/gobby/servers/websocket/handlers/session_observe_continue.py`, `src/gobby/servers/websocket/handlers/session_observe_proxy.py`, `src/gobby/servers/websocket/server.py`, `src/gobby/sessions/context_usage.py`, `src/gobby/sessions/processor_lifecycle.py`, `src/gobby/sessions/processor_transcripts.py`, `src/gobby/sessions/processor_types.py`, `src/gobby/sessions/tmux_context.py`, `src/gobby/sessions/transcript_index_sidecar.py`, `src/gobby/storage/agents/_sandbox_records.py`, `src/gobby/storage/communications.py`, `src/gobby/storage/session_lifecycle.py`, `src/gobby/telemetry/providers.py`, `tests/communications/test_attachments.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/servers/routes/mcp_endpoints/test_execution_offload.py`, `tests/servers/routes/test_agent_spawn_routes.py`, `tests/servers/routes/test_configuration_effective_routes.py`, `tests/servers/routes/test_configuration_routes.py`, `tests/servers/routes/test_projects_routes.py`, `tests/servers/routes/test_providers.py`, `tests/servers/routes/test_tasks_routes.py`, `tests/servers/test_attention_respond.py`, `tests/servers/test_attention_roster.py`, `tests/servers/test_tool_approvals.py`, `tests/servers/test_ws_asgi_endpoint.py`, `tests/servers/websocket/chat/test_codex_clear_context.py`, `tests/servers/websocket/chat/test_runtime_manager.py`, `tests/servers/websocket/test_resume_blocked.py`, `tests/sessions/test_acp_lifecycle_service.py`, `tests/sessions/test_codex_nested_exec_outcomes.py`, `tests/sessions/test_sessions_processor_unit.py`, `tests/storage/test_agent_sandbox_records.py`, `tests/telemetry/test_providers.py`, `tests/test_deployment.py`, `tests/test_runner_gate.py`.

### `85668288c0275f4998ad59eed87bc34a9428e4b4`

[gobby-#19142] feat: arbitrate synthetic tmux input safely

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/adapters/plan_keystrokes.py`, `src/gobby/agents/idle_check_handler.py`, `src/gobby/agents/lifecycle_monitor.py`, `src/gobby/agents/sandbox_policy.py`, `src/gobby/agents/stall_classifier.py`, `src/gobby/agents/terminal_prompt_monitor.py`, `src/gobby/agents/tmux/input_arbiter.py`, `src/gobby/agents/watchdog/recovery.py`, `src/gobby/app_context.py`, `src/gobby/events/wake.py`, `src/gobby/hooks/event_handlers/_agent.py`, `src/gobby/hooks/event_handlers/_base.py`, `src/gobby/hooks/event_handlers/_session_end.py`, `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/detection/claude.toml`, `src/gobby/install/shared/detection/codex.toml`, `src/gobby/install/shared/detection/droid.toml`, `src/gobby/install/shared/detection/gemini.toml`, `src/gobby/install/shared/detection/grok.toml`, `src/gobby/install/shared/detection/qwen.toml`, `src/gobby/mcp_proxy/tools/sessions/_terminal.py`, `src/gobby/mcp_proxy/tools/sessions/_terminal_tmux.py`, `src/gobby/runner.py`, `src/gobby/runner_init/orchestration.py`, `src/gobby/runner_init/servers.py`, `src/gobby/servers/routes/attention.py`, `src/gobby/servers/websocket/handlers/core.py`, `src/gobby/servers/websocket/handlers/plan_approval.py`, `src/gobby/servers/websocket/handlers/session_config.py`, `src/gobby/servers/websocket/handlers/session_observe_proxy.py`, `src/gobby/sessions/compact_continuation.py`, `src/gobby/storage/migrations/346_tmux_input_requests.sql`, `src/gobby/storage/tmux_input.py`, `src/gobby/telemetry/instruments.py`, `tests/adapters/test_plan_keystrokes.py`, `tests/agents/detection/test_registry.py`, `tests/agents/test_lifecycle_monitor.py`, `tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py`, `tests/agents/test_terminal_prompt_monitor.py`, `tests/agents/tmux/test_input_arbiter.py`, `tests/agents/tmux/test_input_arbiter_tmux_integration.py`, `tests/agents/tmux/test_input_architecture.py`, `tests/events/test_wake.py`, `tests/events/test_wake_wiring.py`, `tests/mcp_proxy/tools/sessions/test_compact_self.py`, `tests/mcp_proxy/tools/sessions/test_terminal.py`, `tests/servers/test_attention_respond.py`, `tests/servers/test_attention_roster.py`, `tests/servers/test_session_control.py`, `tests/servers/test_tmux_mixin.py`, `tests/servers/websocket/test_attached_plan_approval.py`, `tests/servers/websocket/test_set_agent.py`, `tests/servers/websocket/test_voice_warmup.py`, `tests/sessions/test_compact_continuation.py`, `tests/test_runner_init.py`.

### `29561ebbd894a34bd05c46058f1456b9751de3a2`

[gobby-#20807] fix: unwrap bare MCP content-block lists in tool_output normalization

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `67f16b326cfc534622dce83a08deee7f61473627` (#20807 — Unwrap bare MCP content-block lists in tool_output normalization).

Exact path/version evidence:

- `src/gobby/hooks/_normalization_mcp.py`: blob `abc717768a93a97559ff476fc9c2b86b7b67622c`, landed in `67f16b326cfc534622dce83a08deee7f61473627`.
- `tests/hooks/test_normalization.py`: blob `f69368fc476e6a5b4d6357c79521a16331ec46ec`, landed in `67f16b326cfc534622dce83a08deee7f61473627`.

### `ccc0b43d9d1cc373df7a2c071b08b62092555811`

[gobby-#21014] test: expect code_overlay_project_id on notify_file_changed

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `8358d7bba6973b5ba58ea9340b0437bbb492be46` (#21009 — Retire automatic memory recall injection and delete the dead recall path); `2d0e428d2e7dc47eb1b5bda3e028337ca1935780` (#21014 — Fix stale notify_file_changed expectation in test_tool_handlers).

Exact path/version evidence:

- `src/gobby/hooks/memory_recall_delivery.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/install/shared/prompts/memory/dedup_decision.md`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/install/shared/prompts/memory/fact_extraction.md`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/install/shared/prompts/memory/session_boundary.md`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/memory-recall-on-prompt.yaml`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/require-memory-recall-retrieval.yaml`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/mcp_proxy/tools/memory_recall.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/memory/context.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `src/gobby/memory/recall.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `tests/hooks/test_tool_handlers.py`: blob `2c696f2ad7c7f143c629db5768fa75cc033417cb`, landed in `2d0e428d2e7dc47eb1b5bda3e028337ca1935780`.
- `tests/mcp_proxy/tools/test_memory_recall.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `tests/memory/test_memory_context.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `tests/memory/test_recall.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `tests/prompts/test_dedup_decision_prompt.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `tests/prompts/test_fact_extraction_prompt.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.
- `tests/workflows/test_memory_recall_gate_rules.py`: blob `0000000000000000000000000000000000000000`, landed in `8358d7bba6973b5ba58ea9340b0437bbb492be46`.

### `74feea869d83c7ed87eceaf4b25caf882fe136e4`

[gobby-#20303] feat: add project_checkouts via migration 412

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `crates/gcore/assets/schema/migrations/412_project_checkouts.sql`, `crates/gcore/src/grant/tests.rs`, `crates/gcore/src/schema/assets.rs`, `crates/gcore/src/schema/runner_tests.rs`, `crates/gcore/tests/catalog_manifest_freshness.rs`, `crates/gdaemon/tests/cli_contract.rs`, `src/gobby/storage/schema_expected_identity.json`, `tests/runtime_grants/golden/brokered_datastores.json`, `tests/runtime_grants/golden/direct_datastores.json`, `tests/runtime_grants/golden/old_client_new_grant.json`, `tests/runtime_grants/golden/payload_skew_unknown_field.json`, `tests/runtime_grants/golden/unavailable_datastores.json`.

Related landed revisions (insufficient proof): `0f4873b20783ebf60b7f5ea1d1249d283540244a` (#20303 — Add project_checkouts via 412_project_checkouts.sql); `4ad4a0f04734b1c4cdf1e6cb753496596f24232f` (#20303 — Add project_checkouts via 412_project_checkouts.sql).

### `39a6de0f1427669505c2ad959e84eee61164e285`

[gobby-#20303] test: pin checkout privilege inventory in authorization TDD

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `0f4873b20783ebf60b7f5ea1d1249d283540244a` (#20303 — Add project_checkouts via 412_project_checkouts.sql).

### `bae6772c589f73e66d057a74deb5b80305d4c912`

[gobby-#20311] feat: cut over agents, sessions, and MCP tool roots

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `f4fee26ad7e76a5299157fdaef42a2d0c1d2f680` (#20311 — Cut over agents, sessions, and MCP tool roots); `3ddf9028f1d4c5f87ee6b5d94a15a536ba116585` (#20311 — Cut over agents, sessions, and MCP tool roots); `44d9eed084b1830de76d17d385a5837f71bbefa9` (#20311 — Cut over agents, sessions, and MCP tool roots).

### `1cf663eca8c267bae60cc9e3203e93b2e9997e5a`

[gobby-#20312] feat: cut over filesystem consumers to machine checkouts

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `21485e0074f626eccd92f65abab9284d503b3009` (#20312 — Cut over files, source control, plans, and workflows).

### `0ebdca49dbdf8723092bda3996093d9f943dc175`

[gobby-#21251] fix: renumber project checkout migration to 414

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `crates/gcore/assets/schema/baseline.sql`, `crates/gcore/assets/schema/catalog.manifest.json`, `crates/gcore/assets/schema/migrations/412_mcp_templates_project_secrets.sql`, `crates/gcore/assets/schema/migrations/412_project_checkouts.sql`, `crates/gcore/assets/schema/migrations/413_session_feedback_review.sql`, `crates/gcore/assets/schema/migrations/414_project_checkouts.sql`, `crates/gcore/src/grant/tests.rs`, `crates/gcore/src/schema/assets.rs`, `crates/gcore/src/schema/baseline_refresh.rs`, `crates/gcore/src/schema/runner_tests.rs`, `crates/gcore/tests/catalog_manifest_freshness.rs`, `crates/gcore/tests/schema_contract.rs`, `crates/gdaemon/tests/cli_contract.rs`, `src/gobby/storage/schema_expected_identity.json`, `tests/runtime_grants/golden/brokered_datastores.json`, `tests/runtime_grants/golden/direct_datastores.json`, `tests/runtime_grants/golden/old_client_new_grant.json`, `tests/runtime_grants/golden/payload_skew_unknown_field.json`, `tests/runtime_grants/golden/unavailable_datastores.json`.

### `ae640711eb1738b30ed40b0eeea37964d5e976d0`

[gobby-#21372] test: fix isolated marker fixture

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `17cc24ea475886e2c9e0b752782321580dfd696f` (#21372 — Fix isolated marker missing-path test fixture).

Exact path/version evidence:

- `crates/gcode/src/config/tests.rs`: blob `71bbae2d700b9ca4c3f08d6c5ae1eae6f8c2faed`, landed in `17cc24ea475886e2c9e0b752782321580dfd696f`.

### `39211c1fe71dd8a2616bcf6f320ab7f7ffafcac7`

[gobby-#21414] fix: align secret upsert with project scope

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/storage/secrets.py`.

### `7e8bffd406aada6f2855786955abd55686a2d8a1`

[gobby-#21770] feat: record handoff provenance and complete transcript coverage

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `crates/gcore/assets/schema/catalog.manifest.json`, `crates/gcore/assets/schema/migrations/427_add_handoff_provenance.sql`, `crates/gcore/src/grant/bundle.rs`, `crates/gcore/src/schema/assets.rs`, `crates/gcore/tests/schema_contract.rs`, `crates/gdaemon/tests/cli_contract.rs`, `src/gobby/mcp_proxy/tools/sessions/_terminal.py`, `src/gobby/sessions/handoff_coverage.py`, `src/gobby/sessions/handoff_evidence.py`, `src/gobby/sessions/handoff_provenance.py`, `src/gobby/sessions/handoff_records.py`, `src/gobby/sessions/transcript_epochs.py`, `src/gobby/sessions/transcript_index.py`, `src/gobby/sessions/transcript_index_sidecar.py`, `src/gobby/sessions/transcript_reader.py`, `src/gobby/sessions/transcript_selection.py`, `src/gobby/sessions/transcripts/base.py`, `src/gobby/storage/schema_expected_identity.json`, `tests/sessions/test_handoff.py`, `tests/sessions/test_transcript_selection.py`.

### `88e6e32668d01bf284bf7ceb85374245478fe397`

[gobby-#21781] feat: recover structured work before expired session synthesis

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/cli/sessions.py`, `src/gobby/config/sessions.py`, `src/gobby/hooks/event_handlers/__init__.py`, `src/gobby/hooks/event_handlers/_agent.py`, `src/gobby/hooks/event_handlers/_base.py`, `src/gobby/hooks/hook_manager.py`, `src/gobby/hooks/session_summary_dispatcher.py`, `src/gobby/hooks/session_summary_wiring.py`, `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/session_end.md`, `src/gobby/install/shared/prompts/handoff/transcript_recovery.md`, `src/gobby/servers/routes/sessions/analytics.py`, `src/gobby/sessions/handoff_coverage.py`, `src/gobby/sessions/handoff_evidence.py`, `src/gobby/sessions/handoff_generation.py`, `src/gobby/sessions/handoff_records.py`, `src/gobby/sessions/handoff_store.py`, `src/gobby/sessions/handoff_summary.py`, `src/gobby/sessions/handoff_synthesis.py`, `src/gobby/sessions/summarize.py`, `src/gobby/sessions/summary_context.py`, `src/gobby/sessions/summary_formatting.py`, `src/gobby/sessions/summary_generation.py`, `src/gobby/sessions/summary_transcripts.py`, `src/gobby/sessions/transcript_processing.py`, `src/gobby/sessions/transcript_selection.py`, `src/gobby/utils/injected_context.py`, `tests/cli/test_cli_sessions_coverage.py`, `tests/hooks/test_agent_events_coverage.py`, `tests/hooks/test_hook_manager_extra.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/prompts/test_handoff_session_end_golden.py`, `tests/sessions/test_archival_summary.py`, `tests/sessions/test_handoff_generation.py`, `tests/sessions/test_handoff_synthesis.py`, `tests/sessions/test_machine_scoped_consumers.py`, `tests/sessions/test_sessions_lifecycle.py`, `tests/sessions/test_summary_formatting.py`, `tests/sessions/test_transcript_read_error.py`, `tests/sessions/test_transcript_selection.py`.

### `c37094fba751e72e7be9dc6675663ba6c0885359`

[gobby-#21791] feat: retire session summary revisions with verified recovery

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `crates/gcore/assets/schema/catalog.manifest.json`, `crates/gcore/assets/schema/migrations/428_retire_session_summary_revisions.sql`, `crates/gcore/src/schema/assets.rs`, `crates/gcore/src/schema/runner.rs`, `crates/gcore/src/schema/runner_tests.rs`, `crates/gcore/src/schema/verify.rs`, `crates/gcore/tests/catalog_manifest_freshness.rs`, `crates/gdaemon/tests/cli_contract.rs`, `src/gobby/storage/schema_expected_identity.json`, `src/gobby/storage/session_models.py`, `src/gobby/storage/sessions/_identity_reconciliation.py`, `src/gobby/storage/sessions/_summary_protocols.py`, `src/gobby/storage/sessions/_summary_update.py`, `tests/storage/sessions/test_current_summary.py`, `tests/storage/test_summary_revision_retirement.py`.

Related landed revisions (insufficient proof): `ee2cc9506a4c01c2b9686496dee9998c6eac4a94` (#21791 — Retire summary revisions with a recoverable mechanical migration).

### `2543162900c5e2de370ce0e81aac6a2da02588a0`

[gobby-#21791] fix: remove retired summary pointer from schema comparison

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `5253db5e7d79179165af1b5645d2e418cb655bfa` (#21890 — Fix baseline lineage break and stale references in summary revision retirement).

Exact path/version evidence:

- `scripts/schema_diff.py`: blob `e40b8353464ddc6bed0de17e5ee6b4d6e2d4dad9`, landed in `5253db5e7d79179165af1b5645d2e418cb655bfa`.

### `46843a72909cf5cd1d2b13e3d88732631556f755`

[gobby-#21792] feat: retrieve attributable project-scoped handoff records

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/mcp_proxy/tools/sessions/_factory.py`, `src/gobby/mcp_proxy/tools/sessions/_handoff_queries.py`, `src/gobby/sessions/handoff_search.py`, `tests/sessions/test_handoff_search.py`.

### `4056b4b32f3c54c37b08ea751089097bfd18dc95`

[gobby-#21793] fix: select session summaries by lifecycle status

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/servers/test_servers_sessions_routes.py`, `web/src/components/activity/SessionsTab.tsx`, `web/src/components/activity/__tests__/SessionsTab.test.tsx`.

### `eb53e743b4e268e42e2fd46b85aabd413f4acb6d`

[gobby-#21820] fix: rotate bounded archive-backed summary retries

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/sessions/test_handoff_generation.py`, `tests/storage/sessions/test_pruning.py`.

Related landed revisions (insufficient proof): `768d871672267c3b2bd3ced27680439d4b22cdaa` (#21820 — Schedule archive-backed summary retries without starving later sessions); `c3e0e7d33695b9c19a01d5dd4bd996dfbb722f18` (#21820 — Schedule archive-backed summary retries without starving later sessions).

### `2a76e1d76e6bedbc8726c96532eaafb14589f7d0`

[gobby-#21826] fix: accept fenced handoff model JSON

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/handoff_synthesis.py`, `tests/sessions/test_handoff_synthesis.py`.

### `3e1c1e95841fea08e029533bea2893f6c1a9221a`

[gobby-#21832] fix: reuse summary generation with parsed work recovery

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/config/sessions.py`, `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/session_end.md`, `src/gobby/sessions/handoff_coverage.py`, `src/gobby/sessions/handoff_evidence.py`, `src/gobby/sessions/handoff_generation.py`, `src/gobby/sessions/handoff_synthesis.py`, `src/gobby/sessions/summary_generation.py`, `src/gobby/sessions/transcript_selection.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/prompts/test_handoff_registry.py`, `tests/prompts/test_handoff_session_end_golden.py`, `tests/sessions/test_handoff_generation.py`, `tests/sessions/test_handoff_synthesis.py`, `tests/sessions/test_transcript_selection.py`.

### `e416a262151a9edef6609d1ae4578f32acaf8c2b`

[gobby-#21842] test: align prompt sync checks with current session prompts

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/test-types-baseline.json`, `tests/prompts/test_prompt_sync.py`.

### `39d5fc517bc47f2f1558a5685087ddb01a11abcc`

[gobby-#21847] fix: exclude session and schema references from task evidence

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/handoff_evidence.py`, `tests/sessions/test_transcript_selection.py`.

### `12abc42f165a18d8f6f0ef004a7d12b985cecb03`

[gobby-#21794] docs: document structured handoff rollout and review boundaries

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/contracts/session-boundary.md`, `docs/guides/mcp-tools.md`, `docs/guides/session-handoff-rollout.md`, `docs/guides/sessions.md`.

### `d10d2f7f6fe842675fe937cfed847be3839d895d`

[gobby-#21850] fix: preserve source attribution and exact recovery references

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/session_end.md`, `src/gobby/install/shared/prompts/handoff/transcript_recovery.md`, `src/gobby/sessions/handoff_synthesis.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/prompts/test_handoff_registry.py`, `tests/sessions/test_handoff_synthesis.py`.

### `9b925f9c2739077bf9ce3a74f87aebf89d201cf7`

[gobby-#21850] fix: reconcile partial recovery and fingerprint combination guidance

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/transcript_recovery.md`, `src/gobby/sessions/handoff_generation.py`, `src/gobby/sessions/handoff_synthesis.py`, `tests/prompts/test_handoff_registry.py`.

### `20308164c0dbf5e48dff8aeba5509f1cfbcf214b`

[gobby-#21794] test: replay real handoff recovery in isolated state

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/sessions/test_handoff_real_replay.py`.

### `410c9506017f3ed4c924c6ef7d36e2bdb6b5c5bc`

[gobby-#21860] fix: give recovery output headroom below its hard limit

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/handoff_synthesis.py`, `tests/sessions/test_handoff_synthesis.py`.

### `fa12a9de439479accd2bbcd37e0d3601821712b0`

[gobby-#21794] docs: record qualified summary model and rollout constraints

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/guides/session-handoff-rollout.md`.

### `103a07c698eb1e4cbcf86a05f9d213c58bf5722f`

[gobby-#21867] fix: separate task evidence from session identities and examples

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/handoff_evidence.py`, `src/gobby/sessions/transcript_selection.py`, `tests/sessions/test_handoff_real_replay.py`, `tests/sessions/test_transcript_selection.py`.

### `0acc7cef777035f57136a68d575653a468370b54`

[gobby-#21875] fix: keep optional review candidates out of remaining work

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/session_end.md`, `src/gobby/install/shared/prompts/handoff/transcript_recovery.md`, `src/gobby/sessions/handoff_synthesis.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/prompts/test_handoff_registry.py`, `tests/sessions/test_handoff_real_replay.py`.

### `5fa4992e9e81d735f548126bb3b9848a65feb2f8`

[gobby-#21881] fix: retain operation scope in recovered session facts

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/session_end.md`, `src/gobby/install/shared/prompts/handoff/transcript_recovery.md`, `src/gobby/sessions/handoff_synthesis.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/prompts/test_handoff_registry.py`, `tests/sessions/test_handoff_real_replay.py`.

### `19ad378d4e65347ec6406b04d83597c57ed2e342`

[gobby-#21886] fix: preserve receipt evidence in recovered work

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/bundled_content_manifest.json`, `src/gobby/install/shared/prompts/handoff/session_end.md`, `src/gobby/install/shared/prompts/handoff/transcript_recovery.md`, `src/gobby/sessions/handoff_synthesis.py`, `tests/prompts/fixtures/handoff_session_end_golden.md`, `tests/prompts/test_handoff_registry.py`, `tests/sessions/test_handoff_real_replay.py`.

### `dea81c11c8c746999952a480bbed6321e0580c58`

[gobby-#22039] chore: checkpoint terminal agent worktree

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/dream/49640230-c32a-4547-957c-f23d36e39109.md`.

### `f7cac386b2dc5b11fe0af346ab01259555f51735`

[gobby-#22039] docs: publish dream synthesis report

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/dream/49640230-c32a-4547-957c-f23d36e39109.md`.

### `26aa30ddb88fca28e26f15c6d2adb57e601dcc34`

[gobby-#22051] chore: checkpoint terminal agent worktree

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/feedback/ef48b0b1-dd58-403a-ac84-52a28675d289.md`.

### `dbe7162df67a9f00b5df939005d849cc5b19ac70`

[gobby-#22051] docs: publish feedback synthesis report

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/feedback/ef48b0b1-dd58-403a-ac84-52a28675d289.md`.

### `e9cdbca0c37dc5bc406732963a4e1c56e35b114a`

[gobby-#22040] chore: checkpoint terminal agent worktree

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/feedback/e3b255c6-044e-48fc-a49b-297d821d016a.md`.

### `8370c53dea76479567e359677c0126e909587764`

[gobby-#22069] docs: publish dream synthesis report

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/dream/c19e1126-cbf4-436e-b956-ada5631bd49c.md`.

### `c06f8df8e42f2a1362a8f8717fd8a9de1e134ecb`

[gobby-#22071] docs: publish feedback synthesis report

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/reports/feedback/15052df6-3b06-450d-9f4e-aa75b4e03917.md`.

### `1621d64f027013f9c2384a607354cb524de1e167`

[gobby-#22228] docs: vendor simdutf licenses

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `4def6a6bf48f4daf08fa86926d13f5dfb7a77152` (#22228 — Vendor a license file for the in-tree simdutf amalgamation).

Exact path/version evidence:

- `crates/gterminal/NOTICE.md`: blob `f936fb72aed66f29017e9d9e52e48924ad91fcab`, landed in `4def6a6bf48f4daf08fa86926d13f5dfb7a77152`.
- `crates/gterminal/vendor/libghostty-vt/pkg/simdutf/LICENSE-APACHE`: blob `fd2496567836776968b853c49be0832369ebf55b`, landed in `4def6a6bf48f4daf08fa86926d13f5dfb7a77152`.
- `crates/gterminal/vendor/libghostty-vt/pkg/simdutf/LICENSE-MIT`: blob `74c8302ba92830e170dfae35957468edd2e25271`, landed in `4def6a6bf48f4daf08fa86926d13f5dfb7a77152`.
- `crates/gterminal/vendor/libghostty-vt/pkg/simdutf/build.zig.zon`: blob `1eb7b080ba5b1451fc6d2f3f98a2e9d7d8e0adb4`, landed in `4def6a6bf48f4daf08fa86926d13f5dfb7a77152`.

### `ae65650a5e80e2e237c2ef411a331cc84ab7a329`

[gobby-#22319] fix: harden isolated daemon health wait

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/e2e/conftest.py`, `tests/e2e/test_crash_recovery.py`, `tests/e2e/test_daemon_health_wait.py`, `tests/e2e/test_daemon_lifecycle.py`, `tests/e2e/test_full_workflow.py`, `tests/e2e/test_runtime_boundary.py`, `tests/e2e/test_session_tracking.py`, `tests/e2e/test_single_active_daemon.py`, `tests/terminals/test_runtime_contract.py`.

### `624e3623dde7e293b43892a1951686b4bd1a4bf8`

[gobby-#22326] fix: repair test drift behind context tiers, review-evidence gate, and deferral shape

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/review_learning/test_round_diff.py`, `tests/storage/stage_review_helpers.py`, `tests/tasks/test_expansion_service_compile_minimal.py`, `tests/workflows/test_database_deadline_retry.py`.

### `707cbc1735d011933162899ee8f2d1d749540bd2`

[gobby-#22093] fix: isolate gterm test hosts from long sockets and cargo rebuilds

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `b654665de62f6a47f8c16d4a5d1685fb5a837c22` (#22093 — Lint policy without blanket allows); `ecd93e4f514fb8d6bd4e27ba3cc35c2d7dadfb04` (#22093 — Lint policy without blanket allows).

### `26387185f291a4b4596d89b67f622de31aadf06b`

[gobby-#22093] fix: isolate gterm test hosts from long sockets and cargo rebuilds

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `b654665de62f6a47f8c16d4a5d1685fb5a837c22` (#22093 — Lint policy without blanket allows); `ecd93e4f514fb8d6bd4e27ba3cc35c2d7dadfb04` (#22093 — Lint policy without blanket allows).

### `486fb22d37d0460196ee7574534c53a758b8df60`

[gobby-#22389] fix: omit retired github/linear columns from task insert

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/storage/tasks/_creation.py`.

### `d95a76a5054c36ed53edc7e30da467be99d47733`

[gobby-#22426] docs: add a chocolate chip cookie recipe reference

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/chocolate-chip-cookies.md`.

### `31f53f3166cc4e00bee8d5469b58d1a7cd84013e`

[gobby-#22434] docs: record the parent-supplied probe word for the blocker round trip

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/droid-probe-blocker.md`.

### `d750a0a953b2f10ab662c18d12a8f82a06234ff3`

[gobby-#22468] fix: reject RTK rewrites that drop a package-manager launcher

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `843bbd7a8ea5b52ee60ea0e286ee3b3a45c5ade1` (#22468 — Preserve package-manager executable resolution in RTK rewrites).

### `ec9898ff6147e85c0075caba465df7a1ae82bf3e`

[gobby-#22894] fix: verify session role fallback and update spawn policy

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `017bb1cc5e9da8a7dae875889166e04954df5d81` (#22894 — Load per-session role files from default agent profile).

Exact path/version evidence:

- `.gobby/plans/research/session-role-acceptance-2026-09-25.md`: blob `f0d3a4fb680cf8f2dad0f8d85de7e474bb541763`, landed in `017bb1cc5e9da8a7dae875889166e04954df5d81`.
- `.gobby/roles/_common.md`: blob `7233fb3e07c0d1347986c010be0023a8f04a63f9`, landed in `017bb1cc5e9da8a7dae875889166e04954df5d81`.
- `.gobby/roles/lane-manager.md`: blob `d4af6a3cece9786dee0e28cd10287c7e030a1b16`, landed in `017bb1cc5e9da8a7dae875889166e04954df5d81`.
- `tests/workflows/test_default_agent_role_contract.py`: blob `238fb2b651e112cabc5404847f0abdc467e02061`, landed in `017bb1cc5e9da8a7dae875889166e04954df5d81`.

### `0902d3c0b2d391b3aacd4adbc7ea19529d536bb6`

[gobby-#22895] docs: repair deploy_runbook plan after adversary round 1

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/research/deploy-runbook-research.md`.

Related landed revisions (insufficient proof): `789cf4a7fa383f8ab59f5ee72581369f62e1b42d` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `bdd246f27d30c173b9917cb1c29805943646943f` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `6612cf3e1307aca5cb2e9e26af921c9f9a7f3abd` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `d17801bb12ee8c8fbd85c0ecae98b107e3ad72bd` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `483f4a1f62210b6f44d77184a7621d7906b8a9e5` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `74b91ba1de7746b220d1dabd76fc3ce873520465` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `99b2e96dab39bd3c0378e8de3e2d6e4f75e177c5` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `0051ffa4c7da669c1f0a80252777ce5a68f0f01f` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `4fbd036a0f4ccdf0b77e2969d5fa043a8c89673e` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `04a68a48ae1f47a1d75051c63d9c143e231c2b75` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `f678c75ada9307070261faa7695c792f0b8f1b13` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `bccc937c30f1fe5c1b9a1c447d3b2f7dd932d568` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `20efd6d930853f0db3a66941fe737df82a048785` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `e28e0949fe2dde35471899508f2f14b5fed1e4c7` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `f713f1efa467b878e543f246866e0edacf9a9486` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `a64017b0e76867748901cf4e8ea944a00a207e02` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `2e4b6ad2530e4feecf3377552d8380c4f6432c90` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `3aa814ece68239861670db063319ffaacd8a099f` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `3acd5c126c4d042b568d191fa883eeba71afb32d` (#22895 — Plan runbooks as existing pipelines with placed agent launch).

### `523f487870188416b063910d65a473a2fa184ff5`

[gobby-#22895] docs: repair deploy_runbook plan after adversary round 2

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/research/deploy-runbook-research.md`.

Related landed revisions (insufficient proof): `789cf4a7fa383f8ab59f5ee72581369f62e1b42d` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `bdd246f27d30c173b9917cb1c29805943646943f` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `6612cf3e1307aca5cb2e9e26af921c9f9a7f3abd` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `d17801bb12ee8c8fbd85c0ecae98b107e3ad72bd` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `483f4a1f62210b6f44d77184a7621d7906b8a9e5` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `74b91ba1de7746b220d1dabd76fc3ce873520465` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `99b2e96dab39bd3c0378e8de3e2d6e4f75e177c5` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `0051ffa4c7da669c1f0a80252777ce5a68f0f01f` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `4fbd036a0f4ccdf0b77e2969d5fa043a8c89673e` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `04a68a48ae1f47a1d75051c63d9c143e231c2b75` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `f678c75ada9307070261faa7695c792f0b8f1b13` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `bccc937c30f1fe5c1b9a1c447d3b2f7dd932d568` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `20efd6d930853f0db3a66941fe737df82a048785` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `e28e0949fe2dde35471899508f2f14b5fed1e4c7` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `f713f1efa467b878e543f246866e0edacf9a9486` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `a64017b0e76867748901cf4e8ea944a00a207e02` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `2e4b6ad2530e4feecf3377552d8380c4f6432c90` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `3aa814ece68239861670db063319ffaacd8a099f` (#22895 — Plan runbooks as existing pipelines with placed agent launch); `3acd5c126c4d042b568d191fa883eeba71afb32d` (#22895 — Plan runbooks as existing pipelines with placed agent launch).

### `d79046696deffcc85caf485f4f3947c47f90902d`

[gobby-#22895] docs: add update_task permission guidance from Josh

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `bdd246f27d30c173b9917cb1c29805943646943f` (#22895 — Plan runbooks as existing pipelines with placed agent launch).

### `92fb1823da1ac8569f82bb088c589c062033b17a`

[gobby-#22927] fix: retire native goals from lane guidance

Superseded: paths were retired by later landed deletions, or their exact versions landed.

Landed evidence: `6811afc506f93de491e628be01fd9241f1772523` (#23039 — Write Josh's triage rules into the Orchestrator role); `69bb3777db1732d36413536daac382b74c35071e` (#23907 — Rewrite seat agent definitions for the operator, retire .gobby/roles, rename crew to team).

### `b472e4cb9ac540a091354695103b287609c87960`

[gobby-#22895] docs: repair deploy_runbook plan after adversary round 3

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `bdd246f27d30c173b9917cb1c29805943646943f` (#22895 — Plan runbooks as existing pipelines with placed agent launch).

### `04772b5e6437a431e15b300e2f02ebff7f342705`

[gobby-#22927] docs: add prompt-only task edit guideline

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `45c2910725d58db3b83d4a047b04604cdabf2086` (#22927 — Retire lane /goal and make Lane Manager recover stalled lanes).

### `4cc1aad2450ed2616867920727b61bece9f271ea`

[gobby-#22761] docs: update gclient guide for Chrome interface

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/guides/gclient-user-guide.md`.

### `769e42d910f4a884e99754a6eadd501418b1c4bb`

[gobby-#22940] docs: renumber the runbooks migration to 455 and annotate the update_task guideline

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/runbooks.md`.

### `759ab55e92f07468f13017b65eaad904c8265364`

[gobby-#22918] chore: stage temporary attention runtime baseline

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `4dcbe9a5199f8f64431503282203c7fcc2059430` (#22710 — Close gates 11-12 TDD evidence and criteria parsing: credit write_stdin PTY runs, parse inline test refs, follow git renames, reject prose-shaped criterion commands).

Exact path/version evidence:

- `src/gobby/storage/attention.py`: blob `92b2b0132db2f3d1e42abd93880a3329ae893ef7`, landed in `4dcbe9a5199f8f64431503282203c7fcc2059430`.

### `78da6fe00d7fbfb07652499bb8c7568e4175414b`

[gobby-#22918] chore: restore attention pruning after baseline sample

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `ef8b9de6ceabdf7a376b29ade5c7453b1ef5699c` (#22918 — Prune empty attention rows and bound roster snapshots).

Exact path/version evidence:

- `src/gobby/storage/attention.py`: blob `593f48031cd406de4b8e08c71b2a94284d2f3af8`, landed in `ef8b9de6ceabdf7a376b29ade5c7453b1ef5699c`.

### `433fb63b352426dbc96e1c195681d138d7b0831d`

[gobby-#22663] docs: reconcile gclient-daemon-resilience with shipped work at 0.5.0 7d33430cc2

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `8f184b40c575c0e6177c95752cb0c502aac5ed59`

[gobby-#22663] docs: restore gclient-daemon-resilience plan from completed

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `1cddc5273e5ce60a977615e07654acafd94b684b` (#23300 — Remove the stale completed/ duplicate of the gclient-daemon-resilience plan); `57b16bf80008c51576f470ee8e08bbdc4f3bad35` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

Exact path/version evidence:

- `.gobby/plans/completed/gclient-daemon-resilience.md`: blob `0000000000000000000000000000000000000000`, landed in `1cddc5273e5ce60a977615e07654acafd94b684b`.
- `.gobby/plans/gclient-daemon-resilience.md`: blob `8c625d8cc3ef3d6fede606dd1dc3c2fb6e0ef616`, landed in `57b16bf80008c51576f470ee8e08bbdc4f3bad35`.

### `f788377a52fef090fee70a389f98cfce82ffa96d`

[gobby-#22663] docs: correct stale line anchors found in self-review

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `3c24dc673eed541c638cb0de1b28355ce3c7c174`

[gobby-#22918] chore: stage current-source attention A/B baseline

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `4dcbe9a5199f8f64431503282203c7fcc2059430` (#22710 — Close gates 11-12 TDD evidence and criteria parsing: credit write_stdin PTY runs, parse inline test refs, follow git renames, reject prose-shaped criterion commands).

Exact path/version evidence:

- `src/gobby/storage/attention.py`: blob `92b2b0132db2f3d1e42abd93880a3329ae893ef7`, landed in `4dcbe9a5199f8f64431503282203c7fcc2059430`.

### `28bc26cd8b77cf9c0880a45c90baac9c61cf61f1`

[gobby-#22918] chore: restore current attention source after A/B

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `714fa7ccf7461558237dc9252599a20a7c75cc86` (#22686 — A turn killed by a provider API error is recorded as a normal 'paused' turn end and nothing resumes it).

Exact path/version evidence:

- `src/gobby/storage/attention.py`: blob `fce6b4cec696102dde2e7b173473d33036510311`, landed in `714fa7ccf7461558237dc9252599a20a7c75cc86`.

### `dd85262f3b66f6d71370fa7b2bafcc8a5239fa05`

[gobby-#22663] docs: apply PD review repairs to gclient-daemon-resilience

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `207548db2e801ad49de8fc1e64619e9e2b3fb650`

[gobby-#22663] docs: keep delivery uncertainty in A2 and bound the D4 census

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `c84ab35b565932e83d29af0da743893089078830`

[gobby-#22918] chore: carry attention A/B baseline onto current main

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `4dcbe9a5199f8f64431503282203c7fcc2059430` (#22710 — Close gates 11-12 TDD evidence and criteria parsing: credit write_stdin PTY runs, parse inline test refs, follow git renames, reject prose-shaped criterion commands).

Exact path/version evidence:

- `src/gobby/storage/attention.py`: blob `92b2b0132db2f3d1e42abd93880a3329ae893ef7`, landed in `4dcbe9a5199f8f64431503282203c7fcc2059430`.

### `0fc7cafbf614021f17d2144852c18d4682d72008`

[gobby-#22918] chore: restore attention source with transcript fix

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `714fa7ccf7461558237dc9252599a20a7c75cc86` (#22686 — A turn killed by a provider API error is recorded as a normal 'paused' turn end and nothing resumes it).

Exact path/version evidence:

- `src/gobby/storage/attention.py`: blob `fce6b4cec696102dde2e7b173473d33036510311`, landed in `714fa7ccf7461558237dc9252599a20a7c75cc86`.

### `1029c2f91d0e37e6c526f7ed0f1be5fef8b9d7ea`

[gobby-#22663] docs: resolve Plan Adversary findings GDR-14 to GDR-26

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `2643406ce598fe5a29d78708aa69a5c5a56f7cf4`

[gobby-#22663] docs: resolve Plan Adversary recheck residuals GDR-16 and GDR-17

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `6002c063cefa1fa861f46a51d55a6382bb7aeae1`

[gobby-#22663] docs: make D3 retirement generation-specific and complete the no-replay sweep

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `4aa520f2a4732454c919595c82f8a56006f09a31`

[gobby-#22663] docs: record Plan Adversary consensus

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `748cd5151a627eaa8a053fbd6e7f0d661ab78107`

[gobby-#22663] docs: add the Adversary-applied M1 task manifest

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6645cbb17bdfd31a433182eb2ed3853bf3ff1892` (#22663 — Amend gclient-daemon-resilience plan with the split-right freeze gaps).

### `b5e5fe5837ce8438b8df5ae41b64d6bbdcb1549e`

[gobby-#22764] fix: stop useChat restore races and timed-out test cascades

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `web/src/hooks/__tests__/useChat.sessions.test.ts`, `web/src/hooks/__tests__/useChat.setup.test.ts`, `web/src/hooks/__tests__/useChat.setup.ts`, `web/src/hooks/useChat/sessionViewing.ts`.

### `04c3b8cc6d6f44b542a884a44854c53285765a4d`

[gobby-#22951] docs: repair five adversary findings in the front-door plan

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `753fb3310286e2918224abc5ce5823f76a3ff804`

[gobby-#22951] docs: repair adversary recheck items A-D in the front-door plan

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `4573796764859eb434b6e7b688d766eaf19a010a`

[gobby-#22951] docs: scope 5.2.19 drain bound and 5.2.20 no-runner claim

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `9774514ae23be4c91b6b6c8748633c1c37bf08a9`

[gobby-#22951] docs: record writer-adversary consensus in the front-door plan

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `c7274d2be24faa4fde01f9c68fe038f32d80c862`

[gobby-#22951] docs: apply adversary-derived M1 to the front-door plan

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `efe2a739fd5ebabe96d1bdb1d83df6a6a5724736`

[gobby-#23058] docs: provisional front-door refresh for #23055 Ask retirement

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `3792fa484a0417a6b81d1c87fa705b63a8efd7eb`

[gobby-#23058] docs: split golden-vector acceptance into verify and reject items

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `.gobby/plans/gdaemon-front-door.md`.

### `de91a6b7f6ed2cdd250f946dc5a00a3e46d5e409`

[gobby-#23055] refactor: retire Ask while preserving evidence

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `d175a1b9801252f3118bfd112a4ec5aa7a969cb8` (#23055 — Retire gcode Ask and gobby-ask while preserving the evidence CLI); `30d66635c10168ad930bf6acc85d63b41ca4f9f3` (#23055 — Retire gcode Ask and gobby-ask while preserving the evidence CLI).

### `312efa5f8f171bb604b1b74b37497ed1c168cb82`

[gobby-#22706] fix: de-conflate workflow-evaluation cancellation source

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/hooks.py`, `tests/workflows/test_hooks.py`.

Related landed revisions (insufficient proof): `fe15d566e60ef3a4977f267feaeb0b6e358fd465` (#22706 — BEFORE_AGENT workflow evaluation cancelled: find and fix the canceller, keep inbox receipts and the post-compact handoff gate intact, resubmit continuations until BEFORE_AGENT arrives).

### `39774017b2b21aa334dc9dda84efa764a52a6299`

[gobby-#22706] fix: name session and source in the workflow evaluation cancellation warning

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/hooks.py`, `tests/workflows/test_hooks.py`.

Related landed revisions (insufficient proof): `fe15d566e60ef3a4977f267feaeb0b6e358fd465` (#22706 — BEFORE_AGENT workflow evaluation cancelled: find and fix the canceller, keep inbox receipts and the post-compact handoff gate intact, resubmit continuations until BEFORE_AGENT arrives).

### `bdcf9cf76117e182536226aad9d6e3be40f8517c`

[gobby-#22706] fix: confirm handoff continuation by BEFORE_AGENT and prove receipt safety

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/compact_continuation.py`, `src/gobby/sessions/continuation_retry.py`, `tests/hooks/test_terminal_handoff_delivery.py`, `tests/servers/routes/mcp/test_hook_cancelled_receipt_safety.py`.

Related landed revisions (insufficient proof): `fe15d566e60ef3a4977f267feaeb0b6e358fd465` (#22706 — BEFORE_AGENT workflow evaluation cancelled: find and fix the canceller, keep inbox receipts and the post-compact handoff gate intact, resubmit continuations until BEFORE_AGENT arrives).

### `240d4d698f5bddd7dde80283b2763b3d65386f99`

[gobby-#22706] fix: release the compact handoff gate at the hydration boundary

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml`, `tests/workflows/test_block_tools_after_handoff_compact.py`, `tests/workflows/test_context_handoff_rules.py`.

Related landed revisions (insufficient proof): `fe15d566e60ef3a4977f267feaeb0b6e358fd465` (#22706 — BEFORE_AGENT workflow evaluation cancelled: find and fix the canceller, keep inbox receipts and the post-compact handoff gate intact, resubmit continuations until BEFORE_AGENT arrives).

### `9093dfb2d6c299100585632bd60a9609d3acbd20`

[gobby-#22706] fix: attribute the evaluation cancellation to runtime teardown and reproduce it

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/evaluation_runtime.py`, `tests/workflows/test_hooks.py`.

### `437f829bfb5a9ed966867f652fe3fc38da73819d`

[gobby-#22706] fix: route Codex continuation through confirmation and re-paste a lost prompt

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/compact_continuation.py`, `src/gobby/sessions/continuation_retry.py`, `tests/hooks/test_terminal_handoff_delivery.py`, `tests/workflows/test_block_tools_after_handoff_compact.py`.

### `66c9fc46726b3197a5db64505b4858ea8b704d3c`

[gobby-#22706] fix: stop an in-band child cancel from masquerading as a cancelled evaluation

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/evaluation_runtime.py`, `src/gobby/workflows/hooks.py`, `tests/workflows/test_evaluation_runtime.py`, `tests/workflows/test_hooks.py`.

### `212faa205f12314b63f15ca62496a388bc727e06`

[gobby-#23063] fix: read the hook's session row once during session resolution

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `3f26068c54860857b623ad022dce8a1f60aca333` (#23063 — UserPromptSubmit rule_engine latency: time the rule-loop bridge and bridged mcp_call effects, then fix the root cause); `84af787c2bccecc7cfb6623cb5d88ebf30f9b4da` (#23063 — UserPromptSubmit rule_engine latency: time the rule-loop bridge and bridged mcp_call effects, then fix the root cause).

### `4609f492b45e2ced3b83f9926cd5d4fb82f83665`

[gobby-#22706] fix: make the child-cancel retry effect-safe and confirm continuations

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/clear_continuation.py`, `src/gobby/sessions/compact_continuation.py`, `src/gobby/sessions/continuation_retry.py`, `src/gobby/sessions/session_variable_store.py`, `src/gobby/workflows/engine/core.py`, `src/gobby/workflows/engine/evaluation.py`, `src/gobby/workflows/evaluation_runtime.py`, `src/gobby/workflows/hooks.py`, `src/gobby/workflows/observer_dispatch.py`, `tests/hooks/test_terminal_handoff_delivery.py`, `tests/sessions/test_compact_continuation.py`, `tests/workflows/test_evaluation_runtime.py`, `tests/workflows/test_hooks.py`.

### `0ebabda96226386416917cd6abd114bcb7d1b797`

[gobby-#22706] fix: name the inline-mcp child-cancel path and reproduce it

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/evaluation_runtime.py`, `src/gobby/workflows/hooks.py`, `tests/workflows/test_evaluation_runtime.py`, `tests/workflows/test_hooks.py`.

### `ba2d1c3e5453b77cf08982493e2c23284874af3e`

[gobby-#22706] fix: preserve rule outputs across child cancellation

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/engine/core.py`, `src/gobby/workflows/engine/evaluation.py`, `src/gobby/workflows/evaluation_runtime.py`, `src/gobby/workflows/hooks.py`, `tests/workflows/test_evaluation_runtime.py`, `tests/workflows/test_hooks.py`.

### `9af079731a4e3630a8ebecbbcaab99061a3ce4a4`

[gobby-#23076] fix: gclient reconnects straight to the host

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `6af542a6b18371efb4a337ea58980286a7cc5e2a` (#23076 — gclient reconnects straight to the host); `81e081264b0426264cb52195babe1550df99ab54` (#23076 — gclient reconnects straight to the host); `b0240a9b3cd261107ce6837d761b97cc10b038ce` (#23076 — gclient reconnects straight to the host); `10b8b1bc136ef865cd24bc2ef104df821b531255` (#23076 — gclient reconnects straight to the host); `926f9027179e0e034b03f5a54697494ae4321a95` (#23076 — gclient reconnects straight to the host); `8c4e25c52cbd120cb2622f09fb32e6ddb18bb3f8` (#23076 — gclient reconnects straight to the host).

### `0a17973545326d24419fdfaf634e4074605ff14c`

[gobby-#22706] fix: isolate embedding fill cancellation and preserve inline blocking policy

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/ai/embedding_cache.py`, `src/gobby/ai/embeddings.py`, `src/gobby/workflows/engine/evaluation.py`, `tests/search/test_embedding_cache.py`, `tests/search/test_embedding_dim_validation.py`, `tests/search/test_embedding_provider_policy.py`, `tests/search/test_embeddings_availability.py`, `tests/search/test_nomic_prefix.py`, `tests/workflows/test_hooks.py`.

### `b8fa20f6a339132f9791a23c907e2a82dbeb25ed`

[gobby-#22706] fix: type cancellation embedding tests

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `bb308056a0364bbb17d6b73f2a5575557ea78f09` (#23303 — Rebase #22706 BEFORE_AGENT eval cancellation onto e30de69f7f and strip daemon logging).

Exact path/version evidence:

- `tests/search/test_embedding_cache.py`: blob `afded8c42eb88655b7ef4280ff6bca56aa7c9965`, landed in `bb308056a0364bbb17d6b73f2a5575557ea78f09`.
- `tests/search/test_embedding_dim_validation.py`: blob `3236691b5c1cdca313c386178e763d9a64665126`, landed in `bb308056a0364bbb17d6b73f2a5575557ea78f09`.
- `tests/search/test_embeddings_availability.py`: blob `6934dd51770ca7e7260f505c700ff5c1519e699b`, landed in `bb308056a0364bbb17d6b73f2a5575557ea78f09`.
- `tests/search/test_nomic_prefix.py`: blob `ee29c46be582310c684d19316997fd80c13e0b8d`, landed in `bb308056a0364bbb17d6b73f2a5575557ea78f09`.

### `3f8d7baf3bedb75d3449e5154d40b2170864e54e`

[gobby-#23192] docs: merge #22951 front-door repairs onto the #23098 slice plans

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. This is a merge with nonempty resolution or an unlanded baseline; ordinary cherry ignores merge patches.

Related landed revisions (insufficient proof): `2d401af64d6aa78406d90e5c491c215561cf15fe` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan); `a10e3f1a57a887087d5d3ce6b4efcffe85cf3c54` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan); `18f11572881dccb79c053c123cc24de05af1e241` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan); `503240052541ba6a42cb33ed7c82b953808a1ffd` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan); `3900e3fa16b9a9e3b7ca5c2527f36253753b0cc3` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan); `e5c4c84d15b65b5ae9a576e2b33d06a25731351c` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan); `9bd95763428e2c5755a2d7eef10428d2fdb7cf5d` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan).

### `aaf14701926e9f7a48951c6a2861adf00211a8de`

[gobby-#23192] docs: port #22951 corpus repairs as corpus leaf 3.3; keep 5.2 harness unsupervised

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a10e3f1a57a887087d5d3ce6b4efcffe85cf3c54` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan).

### `d5741fc24727471ac8949992331c550584c2ff3d`

[gobby-#23076] fix: preserve authorized native input across daemon shutdown

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `573f4cb19b7fe78edec45f7c88d9bb81a1b3ebe6` (#23076 — gclient reconnects straight to the host); `6af542a6b18371efb4a337ea58980286a7cc5e2a` (#23076 — gclient reconnects straight to the host); `20ab85bf1dd471dd220eeeb14b1e6b029b1fff14` (#23076 — gclient reconnects straight to the host); `b0240a9b3cd261107ce6837d761b97cc10b038ce` (#23076 — gclient reconnects straight to the host); `10b8b1bc136ef865cd24bc2ef104df821b531255` (#23076 — gclient reconnects straight to the host).

### `f36e62274aeecd94d2ee24307f90f6d1e612e92f`

[gobby-#23192] chore: merge 0.5.0 into front-door/corpus reconcile

Superseded administrative merge: second parent is already landed and the remerge diff contains no unique resolution.

Landed evidence: `8fd2bfd3f2521b512cbeef97eb4d18ca49675f50` (#23184 — Land reviewed SRT violation cursor alone on 0.5.0).

### `a30789fbec328ac6bc41519b2862c5c0ef91d81d`

[gobby-#23192] docs: withdraw stale M1s and retarget both plans against 0.5.0

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a10e3f1a57a887087d5d3ce6b4efcffe85cf3c54` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan).

### `b9015c8be267fbe3c2e05f3e108f8623f755a81e`

[gobby-#23192] docs: 4.3 advances every corpus case to schema_version 2; order 3.3 before 4.3 and 5.2

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a10e3f1a57a887087d5d3ce6b4efcffe85cf3c54` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan).

### `a5d275a0591cf3bc8f572b41edd6741086e3da7a`

[gobby-#23192] docs: record renewed Adversary consensus on both plans

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a10e3f1a57a887087d5d3ce6b4efcffe85cf3c54` (#23192 — Reconcile gdaemon front-door plan with the #23098 contract-corpus slice plan).

### `ba18e3174ef0afa76b8ef7865b62eefd9a75da01`

[gobby-#23182] fix: match runner test mocks to production shapes and stop the live-hub sweep

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `706f9eea90cc9b498de053341a071e9c445746dc` (#23182 — Runner lifecycle and shutdown tests log ERRORs from mis-shaped mocks).

### `2bff65e61cf980e11f941de9e5e19b5885952ae4`

[gobby-#23182] fix: match runner test mocks to production shapes

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `706f9eea90cc9b498de053341a071e9c445746dc` (#23182 — Runner lifecycle and shutdown tests log ERRORs from mis-shaped mocks).

### `af3423ce740e1c5f7da2f7460d3c95fcd3c6a686`

[gobby-#23198] test: prove unsandboxed builds stay out of the sandbox cache

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `7974276cb11c283f50e8dd4fdc07a73303597082` (#23198 — Stop unsandboxed cargo fingerprint thrash between operator-shell and agent builds).

### `ecf27cff5af8cf6cafde1516f9ccde22db7ef90b`

[gobby-#23076] chore: bump gclient to 0.1.17

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `Cargo.lock`, `crates/gclient/Cargo.toml`, `src/gobby/install/version_pins.py`.

Related landed revisions (insufficient proof): `3ef33f9554050c286020dd7a8723c7759f2e7b95` (#23076 — gclient reconnects straight to the host); `124958d8b4e7414c7a462f04714042dba694d6a2` (#23076 — gclient reconnects straight to the host); `573f4cb19b7fe78edec45f7c88d9bb81a1b3ebe6` (#23076 — gclient reconnects straight to the host); `6af542a6b18371efb4a337ea58980286a7cc5e2a` (#23076 — gclient reconnects straight to the host); `20ab85bf1dd471dd220eeeb14b1e6b029b1fff14` (#23076 — gclient reconnects straight to the host); `81e081264b0426264cb52195babe1550df99ab54` (#23076 — gclient reconnects straight to the host); `b0240a9b3cd261107ce6837d761b97cc10b038ce` (#23076 — gclient reconnects straight to the host); `4ec37301d3bf5d3ebad1ae208ede48eaad1987f8` (#23076 — gclient reconnects straight to the host); `10b8b1bc136ef865cd24bc2ef104df821b531255` (#23076 — gclient reconnects straight to the host); `547319664dfd2974d1989dd2db3702afba174866` (#23076 — gclient reconnects straight to the host); `02bb90408b95518a110489db4c3d3ca462cdc2d4` (#23076 — gclient reconnects straight to the host); `926f9027179e0e034b03f5a54697494ae4321a95` (#23076 — gclient reconnects straight to the host); `5edb4f2b77c19963c515e79757e15c14d47ee95c` (#23076 — gclient reconnects straight to the host); `8c4e25c52cbd120cb2622f09fb32e6ddb18bb3f8` (#23076 — gclient reconnects straight to the host).

### `a4ba740b17837103f7ae5351c5f10b6d656375dc`

[gobby-#23182] fix: match runner test mocks to production shapes

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `706f9eea90cc9b498de053341a071e9c445746dc` (#23182 — Runner lifecycle and shutdown tests log ERRORs from mis-shaped mocks).

### `81a71a4d8088028f4dabea73cb209109bfb3f19c`

[gobby-#23201] fix: withdraw a reopened task's queued memory review

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/storage/tasks/_stage_state_transitions.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/workflows/state_manager.py`, `tests/storage/tasks/test_reopen_build_state.py`, `tests/workflows/test_memory_lifecycle_rules.py`.

### `3a2f06df003ed314bcdca376c86812c54d97ad56`

[gobby-#23225] refactor: split near-ceiling gclient loop files into focused modules

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `crates/gclient/src/app/live_loop/signals.rs`.

Related landed revisions (insufficient proof): `5eebb797287e1a66747154ccb534e477970f45be` (#23225 — Decompose the near-ceiling gclient files).

### `2c88be74b6f6ad12f531bf93480000f61ca06725`

[gobby-#23246] feat: log a bounded pool activity census and sanitized psycopg.pool warnings

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/storage/hub/postgres_pool.py`, `src/gobby/telemetry/logging.py`, `tests/storage/hub/test_postgres_pool_backoff.py`, `tests/telemetry/test_logging.py`.

### `0b125f0fe83a10bb05277af4ef503c647eb2b4b4`

[gobby-#23252] fix: never register an acceptance-artifact span as a criterion command

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/tasks/criterion_commands.py`, `tests/tasks/test_criterion_commands.py`.

### `fb9e73ef724761da5b2b39ccadb4065d7f8dc2ad`

[gobby-#23252] fix: exclude only spans close gate 11 parses as artifact references

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/tasks/acceptance_artifacts.py`, `src/gobby/tasks/criterion_commands.py`, `tests/tasks/test_criterion_commands.py`.

### `4f09729438bbee580efc1f53fb566fbc82aa7493`

[gobby-#23253] fix: route psycopg_pool.* loggers through the psycopg pool sanitizer

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `269596745e4be8308d5faff354cd59d2f4dfded4` (#23299 — Re-cut #23253 psycopg_pool logger sanitizer routing onto 0.5.0 without #23246's census).

Exact path/version evidence:

- `src/gobby/telemetry/logging.py`: blob `7990ec0455e60ba3501c650e1386089120ef2157`, landed in `269596745e4be8308d5faff354cd59d2f4dfded4`.
- `tests/telemetry/test_logging.py`: blob `8bf3f396420320e13166caf4a7007a0ea6c4c636`, landed in `269596745e4be8308d5faff354cd59d2f4dfded4`.

### `91a483db0d906938cd6adecc4c8f9ba89e90980d`

[gobby-#23269] feat: refuse the Python daemon on node bootstraps

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `docs/guides/shared-stack.md`, `src/gobby/cli/cutover.py`, `src/gobby/cli/daemon_preflight.py`, `src/gobby/cli/daemon_start.py`, `src/gobby/config/bootstrap.py`, `src/gobby/runner.py`, `tests/cli/test_cutover.py`, `tests/cli/test_daemon_preflight.py`, `tests/cli/test_daemon_remote_mode.py`, `tests/test_runner_lease_lifecycle.py`.

### `e2f9674a132a1f884ce07af2d1011d181db1100e`

[gobby-#23303] fix: land #22706 BEFORE_AGENT cancellation on e30de69f7f under the logging ban

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/sessions/clear_continuation.py`, `src/gobby/sessions/session_variable_store.py`.

Related landed revisions (insufficient proof): `bb308056a0364bbb17d6b73f2a5575557ea78f09` (#23303 — Rebase #22706 BEFORE_AGENT eval cancellation onto e30de69f7f and strip daemon logging).

### `e69566e1d5c1d1ed294ffd4d286e6e0baa2f1645`

[gobby-#23304] fix: skip direct pytest usage errors in found-work terminal failures

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/found_work_gate.py`.

Related landed revisions (insufficient proof): `2c8f9db6d5c7c9b9b8cc5c30b6ff09362e4a3510` (#23304 — Found-work stop gate treats a direct pytest usage error (exit 4) as an uncoverable terminal validation failure).

### `546d4a8816a91dd7b67423025b80db1e883ea06c`

[gobby-#23304] fix: limit stop-gate usage-error exemption to one direct pytest command

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/found_work_gate.py`.

Related landed revisions (insufficient proof): `2c8f9db6d5c7c9b9b8cc5c30b6ff09362e4a3510` (#23304 — Found-work stop gate treats a direct pytest usage error (exit 4) as an uncoverable terminal validation failure).

### `babceccb367c6d05d7e6cec4c10fc14cbdc55c12`

[gobby-#22856] refactor: remove the dead tmux spawn and launch code

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `b32a993921c79bd7aec36e9e22a66f5cc4cbef08` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `0048987b326ed2f4fe2f287a4ba286d4d8a97658` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `c680ec2453d468adb7a54aece1e00aefcbeca2c2` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces).

### `ffc6a61f7ddfdf684b6e07f9bf246f2ecd180a0c`

[gobby-#22856] refactor: remove the dead tmux spawn and launch code

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `b32a993921c79bd7aec36e9e22a66f5cc4cbef08` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `0048987b326ed2f4fe2f287a4ba286d4d8a97658` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `c680ec2453d468adb7a54aece1e00aefcbeca2c2` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces).

### `e37c011a8631b54e95d5a35ca67d7feac976fe04`

[gobby-#22856] fix: let live tmux rows attach over the terminal WebSocket

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `0048987b326ed2f4fe2f287a4ba286d4d8a97658` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces).

Exact path/version evidence:

- `src/gobby/servers/websocket/terminal_ws.py`: blob `1a5bed99ce7de45fa462689e91c1749b6736e476`, landed in `0048987b326ed2f4fe2f287a4ba286d4d8a97658`.
- `tests/servers/test_native_web_proxy.py`: blob `4a09b3c6ed36a02f42db4c4e0e158b3b5f1dec7a`, landed in `0048987b326ed2f4fe2f287a4ba286d4d8a97658`.
- `tests/servers/test_terminal_ws_attach_honesty.py`: blob `b7738f8d11f23b1e93213cfb8a14ca6e47974cd0`, landed in `0048987b326ed2f4fe2f287a4ba286d4d8a97658`.

### `28509dcd7ea504fd6190a0f3d223bed3da775733`

[gobby-#22856] fix: expire a session whose external tmux pane is gone

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `0048987b326ed2f4fe2f287a4ba286d4d8a97658` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces).

### `241ac2a609968b7eb16a339766fb99ed765bfb82`

[gobby-#23345] fix: keep landed plan sections valid

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/tasks/test_expansion_compile_validate.py`.

Related landed revisions (insufficient proof): `a933b422b0b0655d706055785239c0c7205051dd` (#23345 — Plan semantic lint rejects plans after their leaves land); `4c7197d7ff811cc660f3baddc8d3a59a1cdfa08e` (#23345 — Plan semantic lint rejects plans after their leaves land).

### `6b15e2c08661ef1763159bafe7345f3b3030e591`

[gobby-#22856] fix: drop the retired tmux registration timeout from the placed fixture

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/workflows/test_placed_pipeline_fixture.py`.

Related landed revisions (insufficient proof): `b32a993921c79bd7aec36e9e22a66f5cc4cbef08` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `8c6a44c56dd17056aa7cbcc999546da788b673db` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `6af4d8aef90a7262466cfffe1e9e2c5d7cb2fb25` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `83570b1326d52f4452f8eebd36c4256d145000a3` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `80740f0f40eb7b7d99a6c8fbec695163f94ac198` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces); `4975051778b72112352a057cb231cc861e384d0f` (#22856 — Remove the dead tmux spawn and launch code; keep the external-tmux surfaces).

### `e73a0a043cc0e25115f233703c4a2a124d97e73b`

[gobby-#23280] fix: set the About tagline to "Fleet management for AI agents"

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `46520d61234ba396667849b6ef51744cf4c92ae2` (#23280 — gclient chrome: goblin eyes and halo, About braille layout, sidebar colour, worktree indicators, sentence case).

### `f1a0b777dbafd323e5b0d4fe25a84024f0355276`

[gobby-#23228] feat: typed input and control path off the loop

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `018898c4b2e02b498fb4ad4d2ea941472911750a` (#23228 — Typed input and control path); `ec880b18dff7b36224c495ba9a994e5edf02eb0d` (#23228 — Typed input and control path); `80c841cdc74da61c8f8bc2b39c0c3697c01301f0` (#23228 — Typed input and control path); `528cf50cc04969fb63c0fad7a28a731e9273f9bf` (#23228 — Typed input and control path).

### `ac8f1ae9f65e68da2b6762300d1c35a8bb6d5056`

[gobby-#23228] fix: never lose a release, bound the direct release, close and re-attach behind typed bytes

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `3b5ab684a61067484806c72467bee501d05e22f6` (#23228 — Typed input and control path); `018898c4b2e02b498fb4ad4d2ea941472911750a` (#23228 — Typed input and control path); `ec880b18dff7b36224c495ba9a994e5edf02eb0d` (#23228 — Typed input and control path); `80c841cdc74da61c8f8bc2b39c0c3697c01301f0` (#23228 — Typed input and control path).

### `a1e03365e7c5c9c9077814b2ec19c26a4d896e03`

[gobby-#23401] feat: require a fresh vitest related run for changed web/src files

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `0a25c8aa4fc82754c6dc114468acf0cd1e552ee9` (#23401 — Require a fresh passing vitest related run at close for changed web/src files); `86ed79d533b97423463a0f7c346bbc4d2e57467e` (#23401 — Require a fresh passing vitest related run at close for changed web/src files).

### `c19a7be9bd5d0f48d3bf0d8720d9fc6cfbbc668c`

[gobby-#23401] fix: credit vitest related --passWithNoTests so type-only closes cannot deadlock

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `0a25c8aa4fc82754c6dc114468acf0cd1e552ee9` (#23401 — Require a fresh passing vitest related run at close for changed web/src files).

### `56c6ca2cca3d14d8bf13553e1645c273513c843f`

[gobby-#23134] fix: isolate executed heredoc quote contexts

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `e6b8ddcb37eb421d880f41ad87e68d208753e8e0` (#23134 — Prevent task CLI guard from matching quoted heredoc data); `6b66f49f6fb77d24d48f66cb1ab8535b68b728f2` (#23134 — Prevent task CLI guard from matching quoted heredoc data).

### `d1aee96628b352470bc6dc6ec46828e8a2cbcc87`

[gobby-#23304] fix: skip direct pytest usage errors in found-work terminal failures

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/found_work_gate.py`.

Related landed revisions (insufficient proof): `2c8f9db6d5c7c9b9b8cc5c30b6ff09362e4a3510` (#23304 — Found-work stop gate treats a direct pytest usage error (exit 4) as an uncoverable terminal validation failure).

### `02147d2b079e5718a9289b2e2350ed202560b6b6`

[gobby-#23304] fix: limit stop-gate usage-error exemption to one direct pytest command

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `src/gobby/workflows/found_work_gate.py`.

Related landed revisions (insufficient proof): `2c8f9db6d5c7c9b9b8cc5c30b6ff09362e4a3510` (#23304 — Found-work stop gate treats a direct pytest usage error (exit 4) as an uncoverable terminal validation failure).

### `32658b9baf38461a0b513163569b9ad5d364e743`

[gobby-#23519] chore: merge startup recovery validation base

Superseded administrative merge: second parent is already landed and the remerge diff contains no unique resolution.

Landed evidence: `7d2738d0136a1ab2b62c6424b3eaec9bbc6d28c3` — chore: land reviewed 95ccdbaae2 for #23736.

### `0d903330ac95fa7b802554ee13d38c796d2afedf`

[gobby-#23754] feat: resume existing provider threads on agent relaunch

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `88fda9494010c7fdfad75c04d6bddede5038a42d` (#23754 — spawn_agent resumes an existing provider thread for crew relaunch).

### `80a89534e6289b582eeb1545162c1eb0b62ad507`

[gobby-#23437] chore: merge activated C11 validation baseline

Superseded administrative merge: second parent is already landed and the remerge diff contains no unique resolution.

Landed evidence: `c3d5c4ebe787335f34a539cef4181f493829c4c1` — chore: land reviewed 3d6578ced2 for #23734.

### `0a9331ed53d62b44a70a951ad5ef3a3b0fde65d9`

[gobby-#23437] chore: merge landed startup bootstrap fix for validation

Superseded administrative merge: second parent is already landed and the remerge diff contains no unique resolution.

Landed evidence: `fdaedc5a10cdc25b068128054d707bea490714e2` — chore: land reviewed e3071bf2e6 for #23760.

### `11a170f910bed8338386d0a83719e0883bfb8d0a`

[gobby-#23437] chore: merge C12 baseline for close validation

Superseded administrative merge: second parent is already landed and the remerge diff contains no unique resolution.

Landed evidence: `d261d29988cd6fa430597168ab666cfaab95c106` — chore: land reviewed b7de0b5cb7 for #23758.

### `aa9f081623c6506d644388f9a378760b52dfa3b4`

[gobby-#23001] chore: point seat roles at definitions with prompt parity

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `dfb9b0e69e36f5009a94969a8362028d390ddb31` (#23001 — Role files point at seat definitions); `1f1458434d1b957eeee2d23c4ced7e794aa261f7` (#23001 — Role files point at seat definitions).

### `3500332348a992eb19d9e84e9d4c8001ae28b6d5`

[gobby-#23750] fix: expose approval resume failure diagnostics

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a9f6915bd07ca06e1b37cd7e6be955d3b1d0757a` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh).

### `cbc6d601dd6e7bee720ca9dbc1d4eaa049740b0f`

[gobby-#23750] fix: classify bundled agent activation as reload

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `871cd5df497b986b24e356b1a3af474693871d2f` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh); `1f2651abb6a3fa7fc50aa46ed564b0081ae8336c` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh).

### `0984d396366f2dbdd2c534cc0c43e17d1dab57be`

[gobby-#23750] fix: preserve typed approval failures

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a9f6915bd07ca06e1b37cd7e6be955d3b1d0757a` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh).

### `d2be1da7bf468934b99f4cdd8da02ed37c8c9f30`

[gobby-#23750] fix: reload bundled agent definitions on activation

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `871cd5df497b986b24e356b1a3af474693871d2f` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh); `397657808f30b355442b0d7d09c99ad2eafa3d13` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh); `1f2651abb6a3fa7fc50aa46ed564b0081ae8336c` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh).

### `3f6f8e1ab23f689695dd3d10b225c248146258a6`

[gobby-#23798] feat: infer crew lane launch inputs

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `77e71acaee2f11574b6d1f0d03efce07bf127005` (#23798 — crew-lane infers its inputs from the lane: seats and lane are the whole command); `8b3ff7bafc7a419b2cab71edef1d48fdb0cb0ada` (#23798 — crew-lane infers its inputs from the lane: seats and lane are the whole command); `360a5fc425c9d2816cd526898ce4fa87514f1d84` (#23798 — crew-lane infers its inputs from the lane: seats and lane are the whole command).

### `ce1d4061858ef1df3e064881a57433e22987e119`

[gobby-#23208] feat: supervise native terminal epochs and preserve restarts

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `424cdb33cfa98be3d6929f6e53ea3e7bee31f9c0` (#23208 — Epoch adoption, single-owner supervision, and restart preservation); `8d0d034ece695458895d60c13332dc0202f6dfb2` (#23208 — Epoch adoption, single-owner supervision, and restart preservation).

### `3552b143917dfb30286396de36574434ae982094`

[gobby-#23208] feat: supervise native terminal epochs and preserve restarts

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `424cdb33cfa98be3d6929f6e53ea3e7bee31f9c0` (#23208 — Epoch adoption, single-owner supervision, and restart preservation); `8d0d034ece695458895d60c13332dc0202f6dfb2` (#23208 — Epoch adoption, single-owner supervision, and restart preservation).

### `438f21415c54dcf06fd8b4e1b6bab4ca60e1d9a7`

[gobby-#23750] fix: pin staged CONNECT and mux drain patches

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `e538a255fbb8f5e5e96d17f8cd2faf419f68b880` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh); `91e9c53be045fb8d21f12e5bc92be655ecbdbde8` (#23750 — Team relaunch runbook pipelines: coordination, planning and lane pipelines built step by step with Josh).

### `8594953482b1438254d20b0cc2395da89066a2e2`

[gobby-#23519] docs: retire shared-token agent and operator instructions

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `3f6dc72463883705f8384a0115522088082e424d` (#23519 — Shared-token cutover: gdaemon validates keys and Python trusts only the front door).

### `cc5581cd4f46a77503e2bc93b0356069eac14e72`

[gobby-#23519] fix: isolate pytest home before bootstrap metadata probes

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. Uncovered paths: `tests/conftest.py`, `tests/test_conftest_isolation.py`.

Related landed revisions (insufficient proof): `f33e0ed77b16d784ec2901aba783007c26630f65` (#23519 — Shared-token cutover: gdaemon validates keys and Python trusts only the front door); `bb79dc39074c213ebb28ff039fcb96a8cf0eae3d` (#23519 — Shared-token cutover: gdaemon validates keys and Python trusts only the front door).

### `80d5923ae04c0432043a1706a6ea87bdd74f648a`

[gobby-#23626] chore: merge C29 provider removal with gclient 0.1.28

Missing linked revision, semantic supersession not proven.

No complete landed replacement was established. This is a merge with nonempty resolution or an unlanded baseline; ordinary cherry ignores merge patches.

Related landed revisions (insufficient proof): `ce70d41eee70e22126b676b4c9cff25398d25036` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `22b4d0beefee7cd80b08096fb94f844887dabaaf` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `e4ebfc3ba0d286364e943951ba60c73a0626dbe1` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `ea53f3cd8700deb465d00e659676a72cc5e388b1` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `e416f3cf60c6a812947b6c06717db39dde8ff322` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `9a1d2132a7f5a8da10c736a07aededaf8f2ff11d` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `5838fb073888d973ba557aeff570d192d92577c6` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `82a2034d94ef005768fc9d967f20a992435e5206` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename); `c4b4ad99b8e3ee2c9bf10a7d2274118fef619000` (#23626 — gclient chrome fixes, design first: lane-title ticker, Help > Keybinds wheel scroll, header and tab colours, theme rename).

### `7405e7e42c48ab6b7eba3e1e10032f299d951991`

[gobby-#23585] fix: isolate Git hook installer test environments

Superseded/present: every changed path, file mode and blob version appears in landed history.

Landed evidence: `d522b5e9dd53c4722c192c02a3893456e7a5281a` (#23585 — Install paths ignore GOBBY_HOME: ensure_daemon_config and the Claude hook installer hardcode ~/.gobby).

Exact path/version evidence:

- `tests/cli/installers/test_git_hooks_installer.py`: blob `00614c23436e66eeda51185dc215c82726a73b1f`, landed in `d522b5e9dd53c4722c192c02a3893456e7a5281a`.

### `347fa6d85f94defb3b73dd6c2c8f2462a3cb39e4`

[gobby-#23923] fix: keep managed agent tokens in private envelopes

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `a21ac758d8790886151b57ef5e3e4e3ce3a5cd22` (#23923 — Sandboxed agents can read every same-user process's environment, including agent tokens and DATABASE_URL); `98118b83f01eb654f6cd6c3c5c480dae1a4d2ca6` (#23923 — Sandboxed agents can read every same-user process's environment, including agent tokens and DATABASE_URL); `1ea8e1eccc5eb505c6b8308ca36061c5ae33a88c` (#23923 — Sandboxed agents can read every same-user process's environment, including agent tokens and DATABASE_URL).

### `c392957f18420c74784a0bec2a81fd9cbd4b1458`

[gobby-#23522] docs: record reviewed rollback rehearsal proof

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `bbce1e465f7eaa7c7a9a68e9cf99045d22b9db14` (#23522 — Rollback rehearsal keeps the operator a way in).

### `a260885bb0b2d1d7330c9a49f0db9fd4d7a7d81b`

[gobby-#23985] fix: preserve shell scope across wrappers and branches

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `cf45a0369d34283e0708aacc4c8012363fdd2518` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `399cbf1df3788ca07a56f60a908185faefba075e` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `b8060bc11daa8cea4d28a6cc30603a6357089279` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `1b03aa21f115a93b9248dd6ee99823b0ff4f243a` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `b20bf2fdb813a1afcff407b25695482d7a320b80` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `54de86bfcf102d3198400562a82a48e2f8873ee4` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `4cf3bac9344d6bae4c98e12c821de9104cf110d7` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `549de3f8c4b618f425ed7bf5c02e9c8e64b256a5` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep).

### `a3d7fc298902e5187c72a8f6016e49757952d370`

[gobby-#23985] fix: fail closed on loop directory carry-over

Superseded draft: later landed revision of the same task covers every changed path.

Landed evidence: `cf45a0369d34283e0708aacc4c8012363fdd2518` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `399cbf1df3788ca07a56f60a908185faefba075e` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `1b03aa21f115a93b9248dd6ee99823b0ff4f243a` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep); `b20bf2fdb813a1afcff407b25695482d7a320b80` (#23985 — Shell attribution and classification: env options, nohup/timeout/nice wrappers, cd leaks across branches, close-review LOWs, git -C grep).

## Unavailable objects: every affected closed task

These 512 tasks contain 491 distinct SHA strings absent from the main checkout object store. An absent object cannot be tested for ancestry/equivalence or mapped to a holding branch/worktree. Verdict for every row: **object unavailable; landed/superseded/missing content not determinable**.

| Task | Closed (UTC) | Unavailable linked SHAs | Holding branch/worktree |
| --- | --- | --- | --- |
| #648 — Implement markdown heading parser | 2026-01-11T01:26:15.123967+00:00 | `315ded1` | Cannot resolve: object absent |
| #649 — Implement checkbox extractor | 2026-01-11T01:26:15.124636+00:00 | `329e314` | Cannot resolve: object absent |
| #651 — Detect parallelizable phases from structure | 2026-01-11T01:26:15.124853+00:00 | `43ba26d` | Cannot resolve: object absent |
| #1078 — Fix pre-commit mypy type: ignore conflicts | 2026-01-11T01:26:14.926395+00:00 | `23695b6` | Cannot resolve: object absent |
| #1136 — Implement MergeResolver with tiered strategy | 2026-01-11T01:26:15.209703+00:00 | `266c5f2` | Cannot resolve: object absent |
| #1193 — Implement hash-based caching for compression results | 2026-01-11T01:26:16.056314+00:00 | `ed44be5` | Cannot resolve: object absent |
| #1194 — Implement compress() method with context-aware ratios | 2026-01-11T01:26:16.054632+00:00 | `ed44be5` | Cannot resolve: object absent |
| #1195 — Implement fallback truncation for graceful degradation | 2026-01-11T01:26:16.055591+00:00 | `ed44be5` | Cannot resolve: object absent |
| #1196 — Update compression __init__.py to export TextCompressor | 2026-01-11T01:26:16.055948+00:00 | `24f5553` | Cannot resolve: object absent |
| #1355 — Decompose mcp.py routes using Strangler Fig pattern | 2026-01-11T01:26:14.886043+00:00 | `5a5d346` | Cannot resolve: object absent |
| #1377 — Run full test suite and document changes | 2026-01-25T01:43:54.000000+00:00 | `5a5d346` | Cannot resolve: object absent |
| #1490 — Add terminal context output to GeminiAdapter | 2026-01-11T01:26:14.941308+00:00 | `98b5260` | Cannot resolve: object absent |
| #1935 — Fix todo_list KeyError in session summary generation | 2026-01-12T05:45:27.944721+00:00 | `03ca4c4e` | Cannot resolve: object absent |
| #1942 — Add shorthand options to CLI commands | 2026-01-11T22:32:35.432455+00:00 | `1bf283d4` | Cannot resolve: object absent |
| #2033 — Remove embedding code from memory system | 2026-01-12T05:29:20.085775+00:00 | `e9c306a6` | Cannot resolve: object absent |
| #2038 — create_task response should include seq_num/ref | 2026-01-12T00:27:13.473387+00:00 | `7742da50` | Cannot resolve: object absent |
| #2079 — spawn_agent_in_worktree ignores terminal parameter, spawns headless | 2026-01-12T06:16:17.512437+00:00 | `67432de4`, `c204e4e2` | Cannot resolve: object absent |
| #2102 — Remove embedding code from memory system | 2026-01-12T04:50:21.767044+00:00 | `9fbd23b8` | Cannot resolve: object absent |
| #3327 — Fix generate-criteria CLI output format | 2026-01-13T05:18:48.604046+00:00 | `2a55990b`, `34cf1d06` | Cannot resolve: object absent |
| #3344 — Fix Markdown Lint Errors in task-expansion-v2.md | 2026-01-14T02:38:55.092735+00:00 | `b8beaf57` | Cannot resolve: object absent |
| #3345 — Fix MD060/table-column-style in task-expansion-v2.md | 2026-01-14T02:38:55.751222+00:00 | `b8beaf57` | Cannot resolve: object absent |
| #3346 — Fix on_output type annotation in headless.py | 2026-01-13T21:23:00.735586+00:00 | `5efd6961` | Cannot resolve: object absent |
| #3347 — Cleanup UUID Arguments in CLI | 2026-01-14T02:38:56.354055+00:00 | `d17ec8b8` | Cannot resolve: object absent |
| #3348 — Update CLAUDE.md with current codebase information | 2026-01-14T01:54:06.991928+00:00 | `5e058513` | Cannot resolve: object absent |
| #3350 — Refactor CLI to use ID resolution helpers instead of raw UUIDs | 2026-01-14T02:39:05.343939+00:00 | `d17ec8b8` | Cannot resolve: object absent |
| #3352 — Fix CLAUDE.md issues: tables, MCP discovery, pytest guidance | 2026-01-14T02:22:47.795660+00:00 | `75b3509d` | Cannot resolve: object absent |
| #3354 — Fix MD040/fenced-code-language in CLAUDE.md | 2026-01-14T02:32:00.631739+00:00 | `6950c16c` | Cannot resolve: object absent |
| #3355 — Update close CLI command to accept a list of task identifiers | 2026-01-14T02:42:15.231795+00:00 | `38d1c25b` | Cannot resolve: object absent |
| #3356 — Update ROADMAP.md to reflect recent changes | 2026-01-14T03:20:06.803821+00:00 | `15e38f7f` | Cannot resolve: object absent |
| #3357 — Add Gemini session ID preflight capture | 2026-01-14T04:57:41.686838+00:00 | `e712a72e` | Cannot resolve: object absent |
| #3359 — Fix double success field in MCP tool responses | 2026-01-14T04:58:06.191094+00:00 | `7fc845c5` | Cannot resolve: object absent |
| #3382 — Inject LLMService and DaemonConfig into TaskHierarchyBuilder | 2026-01-15T06:21:40.344753+00:00 | `b5dd31d` | Cannot resolve: object absent |
| #3388 — Add get_current() tool to gobby-sessions | 2026-01-14T19:53:31.865551+00:00 | `5710b79d` | Cannot resolve: object absent |
| #3494 — Fix session title synthesis not running | 2026-01-15T20:59:33.489489+00:00 | `b6d81a02` | Cannot resolve: object absent |
| #4366 — Add integration markers to HTTP coverage test classes | 2026-01-17T18:58:56.580787+00:00 | `66685f25` | Cannot resolve: object absent |
| #4740 — [IMPL] Add MediaAttachment import and media field to Memory dataclass | 2026-01-25T01:43:54.000000+00:00 | `fa335590` | Cannot resolve: object absent |
| #4922 — Document get_current tool in gobby-sessions SKILL.md | 2026-01-19T02:24:53.633233+00:00 | `3dc0404b` | Cannot resolve: object absent |
| #5849 — Block no_commit_needed when tracked files have uncommitted changes | 2026-01-21T17:19:53.284246+00:00 | `2737f219` | Cannot resolve: object absent |
| #5892 — Implement gobby skills install and remove commands | 2026-01-22T00:04:25.528896+00:00 | `fc676e86` | Cannot resolve: object absent |
| #5904 — Extend handoff payload with active_skills field | 2026-01-22T00:44:34.589732+00:00 | `78440d6b` | Cannot resolve: object absent |
| #5933 — Add Clone CLI commands | 2026-01-22T19:17:25.685279+00:00 | `83f2f4fb` | Cannot resolve: object absent |
| #5942 — Add Conductor CLI commands | 2026-01-22T19:35:04.396236+00:00 | `d0e3a8ca` | Cannot resolve: object absent |
| #6030 — Implement configurable task lifecycle policies via workflow variables | 2026-01-24T23:34:36.671792+00:00 | `a7242938` | Cannot resolve: object absent |
| #6047 — Implement Edit History Tracking for Task Enforcement | 2026-01-26T00:00:42.226797+00:00 | `46b1ab6f7` | Cannot resolve: object absent |
| #6070 — Fix multiple codebase issues | 2026-01-25T18:35:50.889738+00:00 | `09b54a93` | Cannot resolve: object absent |
| #6090 — Update plan: Unified /gobby skill router (revised) | 2026-01-26T15:30:32.857135+00:00 | `b93237b1` | Cannot resolve: object absent |
| #6133 — Refactor spawn_agent_in_worktree to use unified spawn_agent | 2026-01-26T17:54:23.984070+00:00 | `79964ca2` | Cannot resolve: object absent |
| #6245 — Add unit tests for sandbox resolvers | 2026-01-27T01:06:20.997993+00:00 | `bdf8ae40` | Cannot resolve: object absent |
| #6252 — Update strangler-fig plan for expansion | 2026-01-27T01:25:14.825562+00:00 | `4d25b7a4` | Cannot resolve: object absent |
| #6375 — Skill hub registry abstraction | 2026-01-28T19:51:06.907276+00:00 | `b754c8fd` | Cannot resolve: object absent |
| #6424 — Check weather forecast in Little Rock, Arkansas | 2026-01-29T02:42:43.171173+00:00 | `74d4fb8c` | Cannot resolve: object absent |
| #6446 — Check weather in Little Rock, AR and report via send_to_parent | 2026-01-29T15:19:02.245433+00:00 | `d21dbce8` | Cannot resolve: object absent |
| #6571 — Fix code issues across claude.py, workflows, and tests | 2026-01-30T05:39:50.905815+00:00 | `5d802cba` | Cannot resolve: object absent |
| #6638 — Fix memory deduplication bypass - align ID generation with global dedup | 2026-02-01T01:48:46.271276+00:00 | `6963eda9` | Cannot resolve: object absent |
| #6906 — Migrate skills CLI sync operations to use LocalSkillManager directly | 2026-02-05T18:24:50.627285+00:00 | `078093e9`, `9f6584cb` | Cannot resolve: object absent |
| #6983 — Test kill_agent self-termination | 2026-02-04T22:37:35.150069+00:00 | `0ed34ea9` | Cannot resolve: object absent |
| #7020 — Add bash/run action to workflow system | 2026-02-05T14:03:23.000200+00:00 | `71bf0882` | Cannot resolve: object absent |
| #7053 — Wipe closed metadata when task is reopened | 2026-03-18T23:20:21.881709+00:00 | `897d7e1d` | Cannot resolve: object absent |
| #7327 — Pre-existing: SQLite migration error 'table passkey_credentials already exists' | 2026-02-09T06:57:38.896954+00:00 | `f69f038e` | Cannot resolve: object absent |
| #7329 — Improve web chat UI markdown rendering | 2026-03-18T23:20:21.795083+00:00 | `b9a1f22e` | Cannot resolve: object absent |
| #7330 — Revert installer inlining and fix bad revert of #7329 | 2026-02-09T07:48:39.180088+00:00 | `4fa82272` | Cannot resolve: object absent |
| #8023 — Fix graph animation toggle placement and behavior on memory page | 2026-03-18T23:20:21.708614+00:00 | `4475b01b`, `78a72c97`, `cf24e4e6` | Cannot resolve: object absent |
| #8159 — Extract embedding service to services/embeddings.py | 2026-03-11T19:58:27.767377+00:00 | `fd8791ec` | Cannot resolve: object absent |
| #8236 — Fix no-any-return mypy errors in loader_sync.py | 2026-03-11T19:58:27.416672+00:00 | `1982becf`, `76e866b1` | Cannot resolve: object absent |
| #8355 — Update CHANGELOG.md with 0.2.17 branch changes | 2026-03-18T23:20:21.523490+00:00 | `547d03f5` | Cannot resolve: object absent |
| #8729 — Chat UI sidebar shows stale agents that have long expired | 2026-02-20T17:10:00.361985+00:00 | `89545f73` | Cannot resolve: object absent |
| #8811 — Create tool-hygiene.yaml rules and remove from session-lifecycle | 2026-03-11T19:58:22.056946+00:00 | `7b386fed` | Cannot resolve: object absent |
| #8896 — Fix MobileTerminalDrawer missing session title fallbacks | 2026-03-18T23:20:21.016560+00:00 | `d99d834e` | Cannot resolve: object absent |
| #8899 — Fix slow daemon startup, broken playwright MCP, and RuntimeWarning spam | 2026-02-22T00:57:33.247585+00:00 | `b45c8ebf` | Cannot resolve: object absent |
| #8900 — Decompose spawn_agent.py into package | 2026-03-18T23:20:20.995797+00:00 | `f0357eaf` | Cannot resolve: object absent |
| #8901 — Decompose runner.py - extract broadcasting and maintenance | 2026-03-18T23:20:20.974289+00:00 | `c37ae1bc` | Cannot resolve: object absent |
| #8902 — Decompose chat.py - extract SessionControlMixin | 2026-03-18T23:20:20.952663+00:00 | `487c0058` | Cannot resolve: object absent |
| #8903 — Decompose install.py - data-driven loops and extract setup | 2026-03-18T23:20:20.933299+00:00 | `ae1d9366` | Cannot resolve: object absent |
| #8915 — Add branch/worktree indicator and switcher to chat UI | 2026-03-18T23:20:20.914115+00:00 | `4f293151` | Cannot resolve: object absent |
| #8916 — Move branch indicator to ChatInput toolbar and show branches in dropdown | 2026-03-18T23:20:20.895571+00:00 | `5d9153c7` | Cannot resolve: object absent |
| #9101 — Fix uninstall tests touching Path.home() instead of fake home | 2026-03-18T23:20:20.738607+00:00 | `77ab87a5` | Cannot resolve: object absent |
| #9112 — Consolidate handoff tools and add agent-authored handoff support | 2026-03-18T23:20:20.677031+00:00 | `91b58384` | Cannot resolve: object absent |
| #9114 — Implement slash command modal system | 2026-03-18T23:20:20.698988+00:00 | `463e903b` | Cannot resolve: object absent |
| #9236 — Harden admin.py restart: SIGTERM before SIGKILL, file handle safety, auth check | 2026-02-27T07:15:22.125600+00:00 | `234547a3` | Cannot resolve: object absent |
| #9316 — Unify Chat and Sessions into a single tab | 2026-02-27T08:09:31.704550+00:00 | `6c607c39` | Cannot resolve: object absent |
| #9333 — Default session filter to web chat | 2026-02-27T08:12:22.176485+00:00 | `dbceb4c6` | Cannot resolve: object absent |
| #9334 — Session list: status filter and hide delete on expired | 2026-02-27T08:20:17.888265+00:00 | `620f366e` | Cannot resolve: object absent |
| #9336 — Fix sidebar, filter, and gallery panel layout bugs | 2026-02-27T08:55:18.549919+00:00 | `0d698076` | Cannot resolve: object absent |
| #9337 — Close artifact panel on window resize | 2026-02-27T08:58:51.055446+00:00 | `7320a30f` | Cannot resolve: object absent |
| #9395 — Create a recursive fibonacci function | 2026-03-01T04:38:53.969577+00:00 | `0da1ed4a`, `32b05fc9`, `5035b4b0`, `ae7952ce`, `e562e07f` | Cannot resolve: object absent |
| #9397 — Fix type checking errors in _handoff.py | 2026-03-01T04:07:44.548758+00:00 | `e562e07f` | Cannot resolve: object absent |
| #9398 — Fix mypy type errors in sessions/_handoff.py | 2026-03-01T04:29:10.850295+00:00 | `32b05fc9` | Cannot resolve: object absent |
| #9399 — Fix pre-existing ruff lint and format issues | 2026-03-01T04:28:54.482946+00:00 | `32b05fc9` | Cannot resolve: object absent |
| #9400 — Merge task #9395 into 0.2.23 release branch | 2026-03-01T04:38:00.695349+00:00 | `4332f3b5` | Cannot resolve: object absent |
| #9409 — Write recursive fibonacci function to n=12 | 2026-03-11T19:58:18.172113+00:00 | `6e3a6974` | Cannot resolve: object absent |
| #9414 — SafeExpressionEvaluator missing IfExp (ternary) support | 2026-03-01T16:06:38.504687+00:00 | `bafc3d51` | Cannot resolve: object absent |
| #9415 — Empty _active_rule_names filters out all rules including agent rule_definitions | 2026-03-01T16:06:47.620861+00:00 | `bafc3d51` | Cannot resolve: object absent |
| #9445 — Web chat resume from terminal/autonomous sessions | 2026-03-18T23:20:20.169436+00:00 | `bba065cb` | Cannot resolve: object absent |
| #9448 — Auto-compaction for long-running autonomous agents | 2026-03-18T23:20:20.092174+00:00 | `9e333c65` | Cannot resolve: object absent |
| #9467 — Fix 3 failing AgentRunnerHookIntegration tests | 2026-03-18T23:20:20.055002+00:00 | `892b700a` | Cannot resolve: object absent |
| #9471 — Manual test: task enforcement verification | 2026-03-18T23:20:20.035582+00:00 | `0844fdc0` | Cannot resolve: object absent |
| #9473 — Manual test: stop gates verification | 2026-03-18T23:20:20.016788+00:00 | `21c34370`, `8bc2116a` | Cannot resolve: object absent |
| #9474 — Manual test: error triage stop gate | 2026-03-02T14:13:43.347641+00:00 | `21c34370` | Cannot resolve: object absent |
| #9690 — chore: Code quality fixes — duplicates, dead code, deprecations | 2026-03-05T13:27:12.544904+00:00 | `85e51b1e` | Cannot resolve: object absent |
| #9832 — Add staleness detection to code index query tools | 2026-03-07T02:41:31.252425+00:00 | `6a4e7451` | Cannot resolve: object absent |
| #9838 — Set code_index_available variable when project has an index | 2026-03-07T02:31:03.373978+00:00 | `62b3c43f` | Cannot resolve: object absent |
| #10145 — Create require-commit-before-status rule to enforce commits on all terminal transitions | 2026-03-10T23:12:38.563894+00:00 | `00c9d3719` | Cannot resolve: object absent |
| #10352 — Phase 1: Models + Config | 2026-03-28T02:47:36.656311+00:00 | `3b97a918` | Cannot resolve: object absent |
| #10353 — Phase 1: DB Migration + Storage CRUD | 2026-03-28T02:47:37.582687+00:00 | `4e9c2e6c0` | Cannot resolve: object absent |
| #10354 — Phase 1: Adapter ABC + Registry | 2026-03-28T02:47:38.441561+00:00 | `f10860aff` | Cannot resolve: object absent |
| #10356 — Phase 1: CommunicationsManager + Wiring | 2026-03-28T02:47:40.201346+00:00 | `e904f3918` | Cannot resolve: object absent |
| #10358 — Phase 2: Telegram Adapter | 2026-03-28T02:47:41.999924+00:00 | `644a0bf2f` | Cannot resolve: object absent |
| #10360 — Phase 2: Polling System | 2026-03-28T02:47:43.801892+00:00 | `f83cf3bcd` | Cannot resolve: object absent |
| #10361 — Phase 2: Webhook Routes + HTTP Registration | 2026-03-28T02:47:44.673767+00:00 | `cfa5b2a00` | Cannot resolve: object absent |
| #10362 — Phase 2: MCP Tools + Broadcasting | 2026-03-28T02:47:46.691168+00:00 | `cfa5b2a00` | Cannot resolve: object absent |
| #10369 — Phase 4: Session Bridging + Identity Linking | 2026-03-28T02:47:51.880013+00:00 | `5312751a3`, `9dd30147c` | Cannot resolve: object absent |
| #10373 — Phase 4: Message Cleanup + CLI Commands | 2026-03-28T02:47:55.334944+00:00 | `cfa5b2a00` | Cannot resolve: object absent |
| #10529 — Add stats columns to sessions table via migration | 2026-03-20T20:06:23.877024+00:00 | `0377d519`, `bc210be5` | Cannot resolve: object absent |
| #10796 — Plan: Consolidate Branches/Worktrees/Clones into unified Source Control tab | 2026-03-26T16:36:32.250185+00:00 | `e9a5413fa` | Cannot resolve: object absent |
| #10808 — Auto-heal hook should suggest gobby-workflows when gobby-pipelines is requested | 2026-03-26T18:02:40.135337+00:00 | `fd8410391` | Cannot resolve: object absent |
| #10860 — Add .gobby/ stash to merge_worktree before merge step | 2026-03-27T20:20:12.431747+00:00 | `0149296c7` | Cannot resolve: object absent |
| #10862 — Fix 3 mypy errors in src/gobby/sync/github.py | 2026-03-27T20:24:46.003543+00:00 | `9a1fc699a` | Cannot resolve: object absent |
| #10863 — Fix 10 mypy errors in task_github.py | 2026-03-27T20:27:26.067909+00:00 | `f0489170d` | Cannot resolve: object absent |
| #10864 — merge_worktree should return success:true with has_conflicts flag instead of success:false | 2026-03-27T20:32:06.339266+00:00 | `98ffab54b` | Cannot resolve: object absent |
| #10867 — Fix gobby-merge pipeline outputs referencing stripped success key | 2026-03-27T20:47:51.972739+00:00 | `d2f51c481` | Cannot resolve: object absent |
| #10870 — Fix 4 mypy errors from epic-10351 merge | 2026-03-27T21:11:46.604306+00:00 | `8ee8855a6` | Cannot resolve: object absent |
| #10962 — Add router rule caching and LRU thread map eviction | 2026-03-29T21:10:40.085226+00:00 | `d8b897b0e` | Cannot resolve: object absent |
| #10974 — Fix mypy errors: _build_gobby_mcp_entry return type | 2026-03-29T05:08:55.842330+00:00 | `02bfd26b4` | Cannot resolve: object absent |
| #11042 — Fix 4 mypy type errors blocking push | 2026-03-30T08:07:01.937234+00:00 | `8cecfd754` | Cannot resolve: object absent |
| #11109 — Fix 7 pre-existing mypy type errors blocking push | 2026-03-31T05:13:08.252443+00:00 | `4f024a4f5` | Cannot resolve: object absent |
| #11111 — Normalize Docker volume naming and fix anonymous log volume | 2026-03-31T05:24:06.131485+00:00 | `3992eaddc` | Cannot resolve: object absent |
| #11112 — Fix pack/unpack CLI help text formatting and accuracy | 2026-03-31T05:32:43.585701+00:00 | `01a17eeaa` | Cannot resolve: object absent |
| #11113 — Clean up stale Qdrant bind-mount data and pack services/ directory | 2026-03-31T05:38:58.991444+00:00 | `735473024` | Cannot resolve: object absent |
| #11116 — Fix 64 pytest failures, 9 errors, and 4 warnings from full suite run | 2026-03-31T15:49:44.508915+00:00 | `1afe9fdad` | Cannot resolve: object absent |
| #11119 — [coderabbit] Frontend, test, docs, and skills nits (B17-B29) | 2026-03-31T15:41:28.020377+00:00 | `f232f623c` | Cannot resolve: object absent |
| #11120 — [coderabbit] Fix mypy error in _resolve_provider helper | 2026-03-31T15:46:33.869912+00:00 | `874dd72cb` | Cannot resolve: object absent |
| #11167 — Update test files to use session_context_for_test for session_id ContextVar migration | 2026-04-02T20:04:45.117938+00:00 | `e5b40fab6` | Cannot resolve: object absent |
| #11772 — Fix memory recall scoring semantics and threshold behavior | 2026-04-14T21:00:39.689899+00:00 | `209ed800b` | Cannot resolve: object absent |
| #11774 — Implement hook normalization v2 | 2026-04-14T21:14:06.533094+00:00 | `798cfb456` | Cannot resolve: object absent |
| #12079 — Document plan-adversary tool-choice and introduce mark_task_review_rejected verb | 2026-04-22T06:38:29.064309+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12130 — Gobby-side block observability + upstream ghook reason forwarding | 2026-04-22T06:01:31.178493+00:00 | `39d4a6718`, `77113cff0`, `c327f36ca`, `cc9f2d12d` | Cannot resolve: object absent |
| #12139 — Gobby-side audit + reason propagation | 2026-04-22T05:52:33.516711+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12140 — Adapter-side sentinel for ghook-origin empty reasons | 2026-04-22T05:59:33.917127+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12142 — [TEST] Phase 1: Write failing tests | 2026-04-22T05:46:15.433393+00:00 | `77113cff0` | Cannot resolve: object absent |
| #12143 — Populate reason from rule name in core.py block emission | 2026-04-22T05:48:50.586670+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12144 — Verify step-enforcement reason propagation in enforcement.py | 2026-04-22T05:49:31.258555+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12145 — Populate webhook identifier in webhook.py block emission | 2026-04-22T05:51:41.913603+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12146 — Populate lifecycle state in chat_session_permissions.py block emission | 2026-04-22T05:52:04.462923+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12147 — Populate webhook state in _lifecycle.py block emissions | 2026-04-22T05:52:27.671248+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12148 — Create integration tests for block observability | 2026-04-22T05:50:32.497854+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12149 — [REF] Phase 1: Refactor with green tests | 2026-04-22T05:50:45.569973+00:00 | `77113cff0`, `c327f36ca` | Cannot resolve: object absent |
| #12150 — [TEST] Phase 2: Write failing tests | 2026-04-22T05:58:18.472542+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12151 — Add empty-reason sentinel substitution to ClaudeCodeAdapter | 2026-04-22T05:58:45.645586+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12152 — Add empty-reason sentinel substitution to GeminiAdapter | 2026-04-22T05:58:58.899922+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12153 — Add empty-reason sentinel substitution to CodexAdapter | 2026-04-22T05:59:16.901098+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12154 — Extract shared sentinel constant and helper to adapters/base.py | 2026-04-22T05:59:28.640096+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12155 — [REF] Phase 2: Refactor with green tests | 2026-04-22T05:55:09.324011+00:00 | `39d4a6718`, `cc9f2d12d` | Cannot resolve: object absent |
| #12159 — Persistence + state semantics | 2026-04-22T05:58:52.774457+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12160 — MCP and HTTP surfaces | 2026-04-22T05:58:53.084139+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12161 — Agent + orchestrator | 2026-04-22T05:58:53.384774+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12162 — Docs | 2026-04-22T05:58:53.684611+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12163 — [TEST] Phase 1: Write failing tests | 2026-04-22T05:47:19.168584+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12165 — Add mark_task_review_rejected transition handler | 2026-04-22T05:47:46.602309+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12166 — [REF] Phase 1: Refactor with green tests | 2026-04-22T05:48:03.445911+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12167 — [TEST] Phase 2: Write failing tests | 2026-04-22T05:48:45.449010+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12168 — Register mark_task_review_rejected MCP verb | 2026-04-22T05:48:57.529099+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12169 — Add HTTP route and request schema for review-rejected | 2026-04-22T05:49:16.804515+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12170 — [REF] Phase 2: Refactor with green tests | 2026-04-22T05:49:30.021687+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12171 — [TEST] Phase 3: Write failing tests | 2026-04-22T05:54:37.152638+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12172 — Update plan-adversary.yaml review step with mark_task_review_rejected | 2026-04-22T05:51:55.056649+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12173 — Update plan-adversary test assertions for mark_task_review_rejected | 2026-04-22T05:52:09.951369+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12174 — Delete PLANNING_CHANGES_REQUESTED_PREFIX parsing from front-half tick | 2026-04-22T05:52:24.902023+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12175 — Surface adversary findings in planner re-dispatch prompt | 2026-04-22T05:52:38.914897+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12176 — Update front-half-orchestrator pipeline if needed | 2026-04-22T05:52:48.327018+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12177 — Add front-half tests for open-with-findings re-dispatch branch | 2026-04-22T05:53:13.242915+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12178 — [REF] Phase 3: Refactor with green tests | 2026-04-22T05:53:31.238024+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12179 — Rewrite plan-review skill with verdict matrix for mark_task_review_rejected | 2026-04-22T05:55:14.612858+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12180 — Update plan skill Step 7.6 to branch on status + findings content | 2026-04-22T05:55:42.180815+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12181 — Update test expectations for mark_task_review_rejected verdict matrix | 2026-04-22T05:50:45.807107+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12182 — Wire task_manager into RuleEngine condition evaluation | 2026-04-22T05:58:53.955446+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12183 — Delegated-autonomy branch in /gobby plan + autonomous planner edit-scope (unified rule) | 2026-04-22T05:59:42.867913+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12184 — Reconcile or remove stale tests/servers/test_chat_session_permissions_plan.py | 2026-04-26T02:46:18.006272+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12185 — [TEST] Phase 1: Write failing tests | 2026-04-22T05:24:05.121743+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12186 — Add task_manager parameter to RuleEngine.__init__ | 2026-04-22T05:38:00.716895+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12187 — Forward task_manager in TemplatingMixin._build_allowed_funcs | 2026-04-22T05:41:42.857814+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12188 — Wire storage.task into RuleEngine in HookManagerFactory | 2026-04-22T05:39:18.467537+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12189 — Fix block-front-half-on-interactive-lock condition for unwrapped input shape | 2026-04-22T05:25:03.124649+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12190 — [REF] Phase 1: Refactor with green tests | 2026-04-22T05:39:29.162401+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12191 — Helpers | 2026-04-22T05:58:54.306079+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12192 — Unified edit-scope rule | 2026-04-22T05:58:54.594340+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12193 — Skill changes | 2026-04-22T05:59:42.487684+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12194 — Planner spawn-time plumbing | 2026-04-22T05:58:54.906795+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12195 — [TEST] Phase 1: Write failing tests | 2026-04-22T05:39:53.802162+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12196 — Add is_current_plan_artifact exact-path helper | 2026-04-22T05:40:10.147385+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12197 — Register is_current_plan_artifact with project_path closure | 2026-04-22T05:42:06.374508+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12198 — Add task_status_in condition helper | 2026-04-22T05:42:22.862916+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12199 — [REF] Phase 1: Refactor with green tests | 2026-04-22T05:42:34.558579+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12200 — [TEST] Phase 2: Write failing tests | 2026-04-22T05:42:59.082418+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12201 — Create block-writes-outside-plan-artifact rule YAML | 2026-04-22T05:55:55.416968+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12202 — Register is_current_plan_artifact condition helper | 2026-04-22T05:43:55.033649+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12203 — [REF] Phase 2: Refactor with green tests | 2026-04-22T05:44:04.774815+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12204 — Replace Step 1a A/P menu with I/D/P menu | 2026-04-22T05:59:20.654626+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12205 — Add Step 7.6 delegated branch logic | 2026-04-22T05:56:37.647876+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12206 — Add Step 7 entry precondition for artifact_path | 2026-04-22T05:56:46.519252+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12207 — [TEST] Phase 4: Write failing tests | 2026-04-22T05:46:20.317743+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12208 — Add initial_variables.artifact_path to dispatch_planner step | 2026-04-22T05:46:38.728939+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12209 — [REF] Phase 4: Refactor with green tests | 2026-04-22T05:46:51.357040+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12210 — Split tests/workflows/test_pipeline_executor.py monolith after #12138 coverage append | 2026-04-22T00:15:22.918316+00:00 | `83e171087` | Cannot resolve: object absent |
| #12217 — Block mark_task_review_rejected in non-reviewer agent blocklists | 2026-04-22T05:53:48.821952+00:00 | `edab4bd5d` | Cannot resolve: object absent |
| #12218 — Add end_agent_run shim tool + wire plan-adversary terminate step to it | 2026-04-22T06:33:05.377659+00:00 | `3f054d30f` | Cannot resolve: object absent |
| #12219 — Engine auto-completes agent run when step workflow terminates (remove self-kill dependency) | 2026-04-22T06:38:03.482749+00:00 | `b7b08d610` | Cannot resolve: object absent |
| #12220 — Split storage/sessions.py into mixin package | 2026-04-22T05:07:56.282152+00:00 | `dd7ba3ea6` | Cannot resolve: object absent |
| #12221 — Unify service and storage into one SessionManager class | 2026-04-22T05:30:42.054615+00:00 | `915cb5a70` | Cannot resolve: object absent |
| #12224 — [TEST] Phase 1: Write failing tests | 2026-04-22T04:51:26.560559+00:00 | `4f9a3fb2c` | Cannot resolve: object absent |
| #12225 — Create sessions package with constants, bootstrap, and CRUD mixin | 2026-04-22T04:41:15.501751+00:00 | `fe6375c11` | Cannot resolve: object absent |
| #12226 — Extract Discovery and Field-update mixins | 2026-04-22T04:56:06.941070+00:00 | `638bffb5d` | Cannot resolve: object absent |
| #12227 — Extract Bulk-update and Query mixins | 2026-04-22T05:01:08.949648+00:00 | `a15a799b7` | Cannot resolve: object absent |
| #12228 — Extract Lifecycle-delegate, Transcript, Usage, Terminal mixins | 2026-04-22T05:06:11.981958+00:00 | `dd7ba3ea6` | Cannot resolve: object absent |
| #12229 — Add line-count invariant meta-check test | 2026-04-22T05:07:10.552406+00:00 | `4f9a3fb2c` | Cannot resolve: object absent |
| #12230 — [REF] Phase 1: Refactor with green tests | 2026-04-22T05:07:45.408374+00:00 | `dd7ba3ea6` | Cannot resolve: object absent |
| #12231 — [TEST] Phase 2: Write failing tests | 2026-04-22T05:12:32.090654+00:00 | `a7b0b8311` | Cannot resolve: object absent |
| #12232 — Create _RegistrationCacheMixin with service-layer methods | 2026-04-22T05:16:44.184008+00:00 | `3b1e59b13` | Cannot resolve: object absent |
| #12233 — Integrate mixin into SessionManager and add first-class methods | 2026-04-22T05:18:52.484065+00:00 | `d651b1588` | Cannot resolve: object absent |
| #12234 — Update storage package exports with SessionManager and compat alias | 2026-04-22T05:20:18.880398+00:00 | `d934b1122` | Cannot resolve: object absent |
| #12235 — Create regression tests for hook-friendly fallback behavior | 2026-04-22T05:23:15.360853+00:00 | `933ef8071` | Cannot resolve: object absent |
| #12236 — Replace sessions/manager.py with deprecation shim | 2026-04-22T05:25:41.788935+00:00 | `f0eecce68` | Cannot resolve: object absent |
| #12237 — [REF] Phase 2: Refactor with green tests | 2026-04-22T05:30:35.066080+00:00 | `915cb5a70` | Cannot resolve: object absent |
| #12238 — Wave 1: Migrate agents/ subsystem to SessionManager | 2026-04-22T05:36:38.984297+00:00 | `5646ef854` | Cannot resolve: object absent |
| #12239 — Wave 2: Migrate cli/ subsystem to SessionManager | 2026-04-22T05:41:44.928651+00:00 | `c2bcd3460` | Cannot resolve: object absent |
| #12240 — Wave 3: Migrate mcp_proxy/ tools and registries to SessionManager | 2026-04-22T05:45:00.164323+00:00 | `064919f48` | Cannot resolve: object absent |
| #12241 — Wave 4: Migrate hooks/ subsystem and unify HookManager attributes | 2026-04-22T05:54:57.324423+00:00 | `acdf8d1f8` | Cannot resolve: object absent |
| #12242 — [TEST] Phase 4: Write failing tests | 2026-04-22T06:02:14.560719+00:00 | `286187d62`, `ae5fe8cd2` | Cannot resolve: object absent |
| #12243 — Simplify messages.py session helpers to attribute-only access | 2026-04-22T06:04:36.818893+00:00 | `5e350521f` | Cannot resolve: object absent |
| #12244 — Migrate test_sessions_manager.py to attribute access and new imports | 2026-04-22T06:06:31.253272+00:00 | `4e33f17c0` | Cannot resolve: object absent |
| #12245 — Update conftest.py session_manager fixture to new import path | 2026-04-22T06:09:38.986818+00:00 | `d237563f7` | Cannot resolve: object absent |
| #12246 — Sweep remaining test stragglers for LocalSessionManager references | 2026-04-22T06:16:58.265473+00:00 | `79f6065ba` | Cannot resolve: object absent |
| #12247 — Remove LocalSessionManager alias, get_session shim, and delete old manager module | 2026-04-22T06:29:48.093956+00:00 | `6c472e462` | Cannot resolve: object absent |
| #12248 — Add meta tests enforcing single-SessionManager invariant | 2026-04-22T06:33:15.093761+00:00 | `e1392ef7f` | Cannot resolve: object absent |
| #12249 — [REF] Phase 4: Refactor with green tests | 2026-04-22T06:36:25.333183+00:00 | `7fb2ce5e8` | Cannot resolve: object absent |
| #12262 — claim_worktree rejects #N session IDs with FOREIGN KEY failure | 2026-04-22T06:30:39.250009+00:00 | `23b600f67` | Cannot resolve: object absent |
| #12332 — Verify canonical model and document decision | 2026-04-26T02:46:12.878092+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12333 — Remove stale test file | 2026-04-26T02:46:12.017567+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12334 — Verify ExitPlanMode-blocking is canonical in chat_session_permissions.py | 2026-04-26T02:45:15.523594+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12335 — Run existing ExitPlanMode blocking tests to confirm green baseline | 2026-04-26T02:45:36.684601+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12336 — Document stale file rationale in commit message draft | 2026-04-26T02:45:52.587599+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12337 — Delete tests/servers/test_chat_session_permissions_plan.py | 2026-04-26T02:45:57.327070+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12338 — Run full test suite to verify no regressions | 2026-04-26T02:46:04.569885+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #12339 — Run type check and lint to confirm clean removal | 2026-04-26T02:46:05.009715+00:00 | `6a54a2ee8` | Cannot resolve: object absent |
| #13984 — Plan-adversary review — round 3 | 2026-05-03T15:42:44.579199+00:00 | `a19ba32cc` | Cannot resolve: object absent |
| #14120 — Update docs, comments, and user-facing text | 2026-05-21T17:05:24.801188+00:00 | `560bc1079`, `a9b16643d`, `e1670dfb6` | Cannot resolve: object absent |
| #14372 — docs: audit and refresh docs/guides/frontend-style-guide.md | 2026-05-07T21:36:18.171185+00:00 | `5e3f17d91` | Cannot resolve: object absent |
| #14373 — docs: audit and refresh docs/guides/github-issue-triage.md | 2026-05-07T21:36:18.171185+00:00 | `49d1b24d4` | Cannot resolve: object absent |
| #14652 — Verify wizard end-to-end | 2026-05-23T13:13:59.542923+00:00 | `0afc3511d` | Cannot resolve: object absent |
| #14960 — Keep task stage rows consistent when closing repaired merge tasks | 2026-05-21T17:38:59.485071+00:00 | `f4b4475e8` | Cannot resolve: object absent |
| #14962 — Make code index usable inside isolated build worktrees | 2026-05-21T17:49:30.542316+00:00 | `cefcff888` | Cannot resolve: object absent |
| #14963 — Add watchdog handling for long idle agent reasoning turns during build reviews | 2026-05-21T17:54:46.205494+00:00 | `fcd2c5ca2` | Cannot resolve: object absent |
| #14964 — Pre-seed isolated worktree Python environments for offline validation | 2026-05-21T18:03:16.249164+00:00 | `0e0c9a694`, `78ce7cbea` | Cannot resolve: object absent |
| #14965 — Return valid success and error envelopes from build resume API | 2026-05-21T18:00:27.695878+00:00 | `5148380ef` | Cannot resolve: object absent |
| #14969 — Harden PostgreSQL keyring compatibility across Linux and Windows | 2026-05-21T18:30:01.948407+00:00 | `42e66cc65` | Cannot resolve: object absent |
| #15105 — Build task-12898-memory-recall-helper.md | 2026-05-25T22:30:19.200214+00:00 | `01294682f`, `02c4e8ab1`, `0a09635a6`, `0d9e35292`, `26a0cd86a`, `3c4de417c`, `6c69d0d94`, `a0b0e421c`, `fb1984c44` | Cannot resolve: object absent |
| #15202 — Reorder `_check_agent_tool_enforcement` so `blocked_tools` overrides the infrastructure-tool exempt | 2026-05-26T02:50:09.074993+00:00 | `404734394` | Cannot resolve: object absent |
| #15204 — Helper-aware delivery + same-turn dedup on the inline `inject_result` path | 2026-05-26T02:50:09.074993+00:00 | `37aac1402` | Cannot resolve: object absent |
| #15205 — Add `cancel_stale_helpers` MCP tool sharing `stop_agent`'s lifecycle path | 2026-05-26T02:50:09.074993+00:00 | `6bd2b1937` | Cannot resolve: object absent |
| #15513 — Document gwiki JSON command contracts | 2026-06-02T18:02:47.882552+00:00 | `75f06cbcd` | Cannot resolve: object absent |
| #15514 — Add GwikiGateway wrapper | 2026-06-02T18:02:47.882552+00:00 | `7587cb529` | Cannot resolve: object absent |
| #15515 — Add wiki update coordinator | 2026-06-02T18:02:47.882552+00:00 | `d7066ef4b` | Cannot resolve: object absent |
| #15516 — Add wiki HTTP routes | 2026-06-02T18:02:47.882552+00:00 | `b0dcf657a` | Cannot resolve: object absent |
| #15517 — Add gobby-wiki MCP tools | 2026-06-02T18:02:47.882552+00:00 | `27d582ac9` | Cannot resolve: object absent |
| #15518 — Add Wiki Activity panel | 2026-06-02T18:02:47.882552+00:00 | `67fe69f58` | Cannot resolve: object absent |
| #15519 — Add chat actions for wiki operations | 2026-06-02T18:02:47.882552+00:00 | `bff04aa78` | Cannot resolve: object absent |
| #15520 — Add debounced daemon watcher for wiki file changes | 2026-06-02T18:02:47.882552+00:00 | `6e8ed61cd` | Cannot resolve: object absent |
| #15521 — Add user-visible scheduled wiki jobs | 2026-06-02T18:02:47.882552+00:00 | `94ff84160` | Cannot resolve: object absent |
| #15522 — Keep lightweight maintenance out of cron history | 2026-06-02T18:02:47.882552+00:00 | `27f60c1d9` | Cannot resolve: object absent |
| #15551 — Fix invalid integration workspace records surviving recovery | 2026-06-02T16:41:44.105387+00:00 | `2bb253241` | Cannot resolve: object absent |
| #15552 — Fix resumed build leaving invalid integration workspace artifacts unhealthy | 2026-06-02T17:09:17.995339+00:00 | `b8e029863` | Cannot resolve: object absent |
| #15696 — Align gobby-cli gitignore allowlist | 2026-06-07T22:40:23.954252+00:00 | `257ca9e` | Cannot resolve: object absent |
| #15819 — Drain subprocess stderr in Codex app-server and ACP clients | 2026-06-11T17:30:09.352924+00:00 | `886fa4166`, `afcecc6b4` | Cannot resolve: object absent |
| #15820 — Enforce ACP pre-tool block decisions in permission grants and terminal/create | 2026-06-11T17:50:49.094091+00:00 | `270e075a8`, `e3bdf5a53` | Cannot resolve: object absent |
| #15821 — Key Codex JSON-RPC dispatch on method presence, not id-map membership | 2026-06-11T18:02:28.734679+00:00 | `73ad9011d` | Cannot resolve: object absent |
| #15822 — Make Codex BEFORE_AGENT gate enforce blocks in the app-server path | 2026-06-11T18:14:59.028091+00:00 | `127c17f87` | Cannot resolve: object absent |
| #15831 — Isolate bad audio bindings: dedupe in registry build, validate VoiceConfig provider ids | 2026-06-11T17:18:40.890351+00:00 | `b675ae9cb` | Cannot resolve: object absent |
| #15832 — Raise on ACP/Codex adapter errors and treat empty output as candidate failure | 2026-06-11T17:37:13.386299+00:00 | `6aabd1577` | Cannot resolve: object absent |
| #15833 — Wire the autonomous stuck/progress-detection layer into hooks and heartbeat | 2026-06-11T17:39:02.657234+00:00 | `bd2f50d17` | Cannot resolve: object absent |
| #15834 — Align explain_dispatch holistic-gate behavior with the real dispatcher | 2026-06-11T17:18:04.508757+00:00 | `89ba58c91` | Cannot resolve: object absent |
| #15835 — Scope build control task resolution to the caller's project for UUID refs | 2026-06-11T17:35:37.440381+00:00 | `c311004b5`, `d5108c220` | Cannot resolve: object absent |
| #15836 — Serialize agent-cap admission across concurrent dispatch heartbeats | 2026-06-11T17:58:39.478493+00:00 | `b64d379cd` | Cannot resolve: object absent |
| #15837 — Add containment checks to gobby unpack to fix zip-slip / arbitrary file write | 2026-06-11T17:20:55.601795+00:00 | `57b56ae59` | Cannot resolve: object absent |
| #15838 — Distinguish daemon-ran-and-failed from daemon-unreachable so pipelines don't silently re-run locally | 2026-06-11T17:40:18.286446+00:00 | `0164dc362`, `1ea30230f`, `8712ec4a7` | Cannot resolve: object absent |
| #15839 — Make merge resolve/apply perform a real merge or fail loudly instead of reporting fake success | 2026-06-11T18:12:25.824845+00:00 | `57e28a34f` | Cannot resolve: object absent |
| #15840 — Scope merge apply/abort to the current project's resolution instead of a global one | 2026-06-11T18:42:25.123573+00:00 | `8e86d935d` | Cannot resolve: object absent |
| #15841 — Scope workflows reinstall delete to Gobby-owned rows so it stops destroying user-authored definitions | 2026-06-11T18:58:23.416490+00:00 | `14da23e8b` | Cannot resolve: object absent |
| #15842 — Write the secret-bearing pack tarball with restricted permissions | 2026-06-11T19:11:07.868705+00:00 | `3a53d3dd8` | Cannot resolve: object absent |
| #15843 — Add a confirm gate and project scoping to memory dedupe to stop unconfirmed cross-project deletion | 2026-06-11T18:20:44.210416+00:00 | `7b8c57457`, `f67ca03b7` | Cannot resolve: object absent |
| #15844 — Guard task-tree walkers and reject parent cycles to stop infinite recursion in task listing | 2026-06-11T18:34:34.862384+00:00 | `224bb4b78` | Cannot resolve: object absent |
| #15845 — Make rules import scope to the named file instead of re-syncing the whole parent directory | 2026-06-11T18:48:46.209840+00:00 | `7e090dbaf` | Cannot resolve: object absent |
| #15846 — Move the daemon already-running guard before kill_all_gobby_daemons in gobby start | 2026-06-11T19:05:42.862818+00:00 | `42d0ed8e4`, `fdf4ec670` | Cannot resolve: object absent |
| #15870 — Route rules enable/disable through the daemon so the running engine picks up the toggle | 2026-06-11T19:22:57.131384+00:00 | `4eab8ad53` | Cannot resolve: object absent |
| #15871 — Fix feature-candidate allowlist rejecting explicit provider+model with misleading error | 2026-06-11T17:49:47.661973+00:00 | `251153f15` | Cannot resolve: object absent |
| #15874 — Route worktrees claim/release through the daemon instead of direct DB writes | 2026-06-11T19:51:17.604646+00:00 | `6aeb3da76`, `85fed7879` | Cannot resolve: object absent |
| #15877 — Use zombie-aware _is_process_alive in gobby status | 2026-06-11T20:09:59.300415+00:00 | `61c03f503` | Cannot resolve: object absent |
| #15880 — Let registry select() gate voice transcribe instead of whisper config early-returns | 2026-06-11T18:04:41.583958+00:00 | `1300c7e82` | Cannot resolve: object absent |
| #15882 — Wire tasks validate --max-iterations and return nonzero on every error path | 2026-06-11T20:24:04.366954+00:00 | `a6c4817ce` | Cannot resolve: object absent |
| #15892 — Re-probe provider installed() at selection time; stop freezing availability at startup | 2026-06-11T18:16:56.346783+00:00 | `1951e582e` | Cannot resolve: object absent |
| #15898 — Re-raise LLMProviderCancellation in candidate fallback loops for graceful shutdown | 2026-06-11T18:30:07.778011+00:00 | `a73c76ad7` | Cannot resolve: object absent |
| #15912 — Re-resolve CLI binding availability at selection time instead of freezing at build | 2026-06-11T18:42:42.370084+00:00 | `a90be5e74` | Cannot resolve: object absent |
| #15917 — Export GOBBY_QDRANT_HTTP_PORT so qdrant install --port works | 2026-07-15T12:46:44.087310+00:00 | `271a42d28`, `bfc2ea786` | Cannot resolve: object absent |
| #15923 — Use partition('/') in _parse_candidate to match registry/config candidate parsing | 2026-06-11T18:53:35.954723+00:00 | `13848a804` | Cannot resolve: object absent |
| #15927 — Implement POST /api/comms/send backing gobby comms send | 2026-07-15T12:56:52.111266+00:00 | `4e5c87e09` | Cannot resolve: object absent |
| #15930 — Validate audio provider ids at config parse time to prevent registry construction crash | 2026-06-11T19:14:39.614219+00:00 | `5a74301e3`, `aed9039cb` | Cannot resolve: object absent |
| #15936 — Reject sse transport at validation or implement an SSE transport | 2026-07-15T13:11:51.481294+00:00 | `b7de2132f` | Cannot resolve: object absent |
| #15942 — Resolve cron CLI job arguments by name with UUID fallback | 2026-07-15T13:21:21.174805+00:00 | `587079a74` | Cannot resolve: object absent |
| #15944 — Fix autonomous stagnation latch and tool-call classification heuristics | 2026-06-11T17:56:12.540264+00:00 | `44d6ac175` | Cannot resolve: object absent |
| #15968 — Bring MCP/CLI build surfaces to parity on controls, options, profile, delivery | 2026-06-11T18:20:14.376607+00:00 | `f5944567e` | Cannot resolve: object absent |
| #15981 — Check active workspace runs before resume reuses worktrees/clones | 2026-06-11T18:34:05.732512+00:00 | `b105d279c` | Cannot resolve: object absent |
| #15992 — Classify dirty worktrees before build clean --force deletes artifacts | 2026-06-11T18:57:55.444549+00:00 | `387d352b0`, `a759fc293` | Cannot resolve: object absent |
| #16000 — Restrict plan-file exemptions to resolved in-repo plan paths | 2026-07-14T02:41:56.232640+00:00 | `b1e7951cc` | Cannot resolve: object absent |
| #16005 — Coordinate recovery claim releases with the task dispatch mutex | 2026-06-11T19:24:02.361324+00:00 | `ddadebd39` | Cannot resolve: object absent |
| #16009 — Let cancellation propagate in Droid streams and registry drain cleanup | 2026-07-14T02:57:26.026879+00:00 | `cf49ed20e` | Cannot resolve: object absent |
| #16016 — Normalize build HTTP control envelopes and add missing load-bearing tests | 2026-06-11T19:51:13.997962+00:00 | `c06b6369a` | Cannot resolve: object absent |
| #16019 — Persist partial assistant messages and reset status on abnormal chat stream exit | 2026-07-14T03:10:30.005278+00:00 | `9a8346d01` | Cannot resolve: object absent |
| #16025 — Clean attached-session TTS on detach/disconnect and raise generated ID entropy | 2026-07-14T03:27:08.292715+00:00 | `e9e93d1b4` | Cannot resolve: object absent |
| #16028 — Offload blocking git and DB work in build spawn, control, and dispatch paths | 2026-06-11T20:29:45.453917+00:00 | `d57a6b411`, `f39c6abcf` | Cannot resolve: object absent |
| #16040 — Refuse branch cleanup when project repo_path is null instead of using cwd | 2026-06-11T20:44:03.123529+00:00 | `de1254693` | Cannot resolve: object absent |
| #16052 — Replace stage manifests under the dispatch mutex in one transaction | 2026-06-12T01:37:34.046615+00:00 | `822363616` | Cannot resolve: object absent |
| #16061 — Report failed/cancelled build root states instead of unconditional completed | 2026-06-12T01:49:52.332505+00:00 | `f65f322a2` | Cannot resolve: object absent |
| #16076 — Use git_subprocess_env in workspace_git and target_branch subprocesses | 2026-06-12T02:00:59.282684+00:00 | `1c172c8c0` | Cannot resolve: object absent |
| #16082 — Dispatch Codex incoming requests off the read loop | 2026-06-11T18:26:34.803973+00:00 | `07ad7797c` | Cannot resolve: object absent |
| #16086 — Verify cleanliness and active runs before persisting workspace promotion | 2026-06-12T02:18:45.652706+00:00 | `e2723a9a7` | Cannot resolve: object absent |
| #16092 — Fail pending Codex requests immediately when the app-server process dies | 2026-06-11T18:41:11.954403+00:00 | `dc4fa2b2c` | Cannot resolve: object absent |
| #16093 — Remove gobby postgres uninstall command; full-container teardown is the only uninstall path | 2026-06-11T19:48:06.592073+00:00 | `513f4d379`, `c1ba9788f` | Cannot resolve: object absent |
| #16097 — Add an overall timeout to Codex text generation | 2026-06-11T19:28:26.858357+00:00 | `0b364a8b0` | Cannot resolve: object absent |
| #16099 — Fix ACP setup-phase request routing, CancelledError swallow, and start() leak | 2026-06-11T18:59:09.645636+00:00 | `d743d943c` | Cannot resolve: object absent |
| #16102 — Add deadline and turn-failure handling to the Codex text-generate path | 2026-06-11T20:22:41.831750+00:00 | `077c0ee87`, `abbfbf868`, `d9c834c0d` | Cannot resolve: object absent |
| #16106 — Force permissionDecision allow when Claude updatedInput is emitted | 2026-06-11T19:12:12.101779+00:00 | `aa7c4c9a1` | Cannot resolve: object absent |
| #16107 — Add tests for register_plan_command happy path and malformed-plan error path | 2026-06-11T20:22:56.689876+00:00 | `8b793ef5b` | Cannot resolve: object absent |
| #16110 — Aggregate all-unavailable candidate errors into CapabilityUnavailableError (400 not 500) | 2026-06-12T01:28:55.483184+00:00 | `64c6da7de` | Cannot resolve: object absent |
| #16112 — Guard static-source plan-approval keystrokes with live pane-state checks | 2026-06-11T19:37:11.094092+00:00 | `d34eefe1d` | Cannot resolve: object absent |
| #16115 — Contain export-import import dest path so name/--from can't write outside the target dir | 2026-06-11T20:39:53.558878+00:00 | `6ca2f47db` | Cannot resolve: object absent |
| #16123 — Map Codex block to decline for command/file/mcp approvals | 2026-06-11T19:48:30.870766+00:00 | `2bf071dd8` | Cannot resolve: object absent |
| #16126 — Handle write errors in skills doc --output instead of leaking a traceback | 2026-06-12T01:30:16.955838+00:00 | `319c4e1ff` | Cannot resolve: object absent |
| #16128 — Normalize Gemini/Qwen tool_call session updates so lifecycle hooks fire | 2026-06-11T20:11:48.044970+00:00 | `256375fae` | Cannot resolve: object absent |
| #16134 — Reset or id-validate ACP stdout correlation after request timeouts | 2026-06-11T20:39:43.529968+00:00 | `129005b29` | Cannot resolve: object absent |
| #16136 — Make workflows reinstall --type delete from the type's real table | 2026-06-12T01:43:35.085314+00:00 | `a2279a431` | Cannot resolve: object absent |
| #16144 — Make workflows reload, linear sync/create, and cron add/edit return nonzero on failure | 2026-06-12T02:00:19.382087+00:00 | `bdf9996f6` | Cannot resolve: object absent |
| #16152 — Prefer the daemon route for pipeline approve/reject so resumes keep MCP/LLM access | 2026-06-12T02:20:40.784176+00:00 | `58667eb60` | Cannot resolve: object absent |
| #16164 — Escape LIKE wildcards in resolve_agent_run_id / find_tasks_by_prefix and reject empty refs | 2026-06-11T20:49:21.753500+00:00 | `731d7f9b1`, `cfa256509` | Cannot resolve: object absent |
| #16165 — Fail create_task when dependency attach errors instead of swallowing them into a partial task | 2026-06-12T01:35:50.822378+00:00 | `71bf535f2` | Cannot resolve: object absent |
| #16167 — Guard memory rebuild-graph daemon calls and cap each request by the remaining --timeout deadline | 2026-06-12T01:53:12.159423+00:00 | `ac79d5028` | Cannot resolve: object absent |
| #16170 — Make record_verification_evidence an atomic append | 2026-07-15T13:29:20.113474+00:00 | `3499ca6c4` | Cannot resolve: object absent |
| #16171 — Mark expansion runs failed on CLI error instead of leaving them stuck running with raw tracebacks | 2026-06-12T02:19:02.311033+00:00 | `15397fd0e` | Cannot resolve: object absent |
| #16176 — Restrict send_keys targets to owned or same-project sessions | 2026-07-15T13:45:38.856850+00:00 | `766420a91` | Cannot resolve: object absent |
| #16235 — Add CORSMiddleware outermost so auth-rejected responses carry CORS headers | 2026-07-14T03:36:12.382479+00:00 | `06858ed6d` | Cannot resolve: object absent |
| #16241 — Delimit untrusted text interpolated into privileged LLM prompt templates | 2026-07-15T12:03:25.869532+00:00 | `adc1f441c` | Cannot resolve: object absent |
| #16244 — Add liveness check and turn deadline to the Codex send_message wait loop | 2026-07-14T03:47:42.736979+00:00 | `8d75adae9` | Cannot resolve: object absent |
| #16257 — Authorize WebSocket attach/observe before subscribing to session streams | 2026-07-14T04:26:39.384609+00:00 | `0f182bb41`, `1880cd2cd` | Cannot resolve: object absent |
| #16270 — Cap single-file diff reads in session_changes by file size | 2026-07-14T04:37:24.245063+00:00 | `c632d79e6` | Cannot resolve: object absent |
| #16282 — Fix Linear/GitHub integrations: pagination, mutation-safe retries, availability | 2026-07-12T03:58:26.831800+00:00 | `ea0959bba` | Cannot resolve: object absent |
| #16283 — Defer inter-session mark_delivered until context injection is confirmed | 2026-07-14T04:52:43.449547+00:00 | `6787173e9` | Cannot resolve: object absent |
| #16293 — Deliver timeout to pending-interaction waiters only when expire actually won | 2026-07-14T05:02:58.849406+00:00 | `3bb354f6a` | Cannot resolve: object absent |
| #16310 — Enforce a read-only allowlist for Bash and MCP write tools in plan mode | 2026-07-14T05:22:07.959468+00:00 | `05aae8652` | Cannot resolve: object absent |
| #16322 — Fix Droid context-window catalog drift and resolver precedence | 2026-07-14T05:36:22.294566+00:00 | `099aa8307` | Cannot resolve: object absent |
| #16325 — Keep send_message success tied to mailbox result, not optional WS broadcast | 2026-07-15T12:48:43.317660+00:00 | `ff4e6d53b` | Cannot resolve: object absent |
| #16337 — Guard WebSocket _handle_message against non-dict JSON payloads | 2026-07-14T05:51:27.623309+00:00 | `3b4dba505` | Cannot resolve: object absent |
| #16341 — Mark messages delivered only after payloads are built in deliver_pending_messages | 2026-07-15T13:01:43.903965+00:00 | `429f69ef0` | Cannot resolve: object absent |
| #16351 — Key pending approvals/questions/plans by tool_use_id for concurrent tool calls | 2026-07-14T06:41:10.264021+00:00 | `56e49f2d9` | Cannot resolve: object absent |
| #16364 — Make idle chat-session reaper teardown race-safe and registry-consistent | 2026-07-14T06:57:47.830132+00:00 | `26945cbc2` | Cannot resolve: object absent |
| #16366 — Restore isolation context prompt for reused worktree/clone spawns | 2026-07-15T13:20:00.629231+00:00 | `5b7a908a3` | Cannot resolve: object absent |
| #16377 — Release pending approval/plan/question waiters in ChatSession.stop() | 2026-07-14T07:18:51.900506+00:00 | `e8ab1d538` | Cannot resolve: object absent |
| #16379 — Exclude node_modules/target/dist/build from test-quality directory walks | 2026-07-13T16:17:53.610603+00:00 | `112ee46ea` | Cannot resolve: object absent |
| #16391 — Reset the global artifact broadcaster on app shutdown | 2026-07-14T07:38:46.281456+00:00 | `1ca37c448` | Cannot resolve: object absent |
| #16406 — Route tmux set-option/refresh-client through TmuxSessionManager._base_args | 2026-07-14T08:04:23.561070+00:00 | `0c067b4e6` | Cannot resolve: object absent |
| #16420 — Flag bare @pytest.mark.xfail under XFAIL_WITHOUT_STRICT_OR_REASON | 2026-07-13T16:44:21.864502+00:00 | `6b2905db7` | Cannot resolve: object absent |
| #16434 — Gate voice idle-unload on actual load state, not warmup status, so lazily-loaded models get reclaimed | 2026-07-13T19:41:56.132510+00:00 | `f729d89b8` | Cannot resolve: object absent |
| #16438 — Bound db_executor shutdown so a hung DB call cannot block daemon shutdown forever | 2026-07-15T09:45:09.645332+00:00 | `31d1daccf` | Cannot resolve: object absent |
| #16443 — Guard _write_verification against corrupt project.json instead of crashing on --fix | 2026-07-13T19:57:30.421039+00:00 | `7d448887f` | Cannot resolve: object absent |
| #16454 — Guard collect_evidence against valid-but-non-object project.json / package.json | 2026-07-13T20:08:36.251642+00:00 | `677870c20` | Cannot resolve: object absent |
| #16463 — Honor explicit guardrail_target=tool-config in the confirmed promotion ladder | 2026-07-13T20:19:20.018799+00:00 | `b99e1badd` | Cannot resolve: object absent |
| #16470 — Make finding fingerprints unambiguous so distinct findings stop colliding and dropping lessons | 2026-07-13T20:40:33.925750+00:00 | `597895871` | Cannot resolve: object absent |
| #16478 — Make resolve_refresh_root walk up to the project root instead of suggesting the split-project gobby init | 2026-07-13T20:45:36.147831+00:00 | `00007a758` | Cannot resolve: object absent |
| #16482 — Anchor /clear session-boundary detection to real command messages | 2026-07-14T13:40:13.706194+00:00 | `c12368355` | Cannot resolve: object absent |
| #16485 — Make test-quality audit tolerate one unparseable/non-UTF-8 .py file instead of aborting the run | 2026-07-14T01:59:22.798163+00:00 | `f666ad164` | Cannot resolve: object absent |
| #16490 — Offload sync internal tool functions off the event loop at the registry dispatch | 2026-07-15T13:22:26.371197+00:00 | `f3374c412` | Cannot resolve: object absent |
| #16493 — Make the JS/TS delimiter scanner comment-aware so tests with apostrophes in comments stop vanishing | 2026-07-14T02:10:05.902603+00:00 | `beb1d9f95` | Cannot resolve: object absent |
| #16497 — Project-scope delete_memory and update_memory | 2026-07-15T12:55:47.291203+00:00 | `7d8254b3f` | Cannot resolve: object absent |
| #16502 — Make unload() lock-safe and cancel active pipelines so it stops mutating/racing the shared model | 2026-07-14T02:26:16.461260+00:00 | `26688d4df` | Cannot resolve: object absent |
| #16512 — Make zero-file / ignored-explicit-file test-quality audits non-silent (warn or nonzero exit) | 2026-07-14T02:37:46.550120+00:00 | `15e40e54f` | Cannot resolve: object absent |
| #16516 — Check tree cleanliness before direct merge; mark resolved when merge landed | 2026-07-15T13:14:41.819492+00:00 | `6d6753be6` | Cannot resolve: object absent |
| #16528 — Move tensor->PCM conversion into the worker thread so .cpu()/clip don't block the event loop | 2026-07-14T02:50:46.457581+00:00 | `87678d9c1` | Cannot resolve: object absent |
| #16549 — Kill the whole process group on verify_in_worktree timeout | 2026-07-15T13:25:51.292232+00:00 | `cfce2bbff` | Cannot resolve: object absent |
| #16563 — Offload _check_imports heavy dependency imports off the event loop | 2026-07-14T02:58:16.644653+00:00 | `ea3e5d769` | Cannot resolve: object absent |
| #16566 — Restore detached HEAD correctly after direct merge | 2026-07-15T13:36:23.195070+00:00 | `62fa5c60a` | Cannot resolve: object absent |
| #16580 — Offload sync DB I/O in _resolve_scope off the event loop | 2026-07-14T03:08:11.870122+00:00 | `343ee9d97` | Cannot resolve: object absent |
| #16584 — Serialize merge_start/merge_apply with the per-resolution lock | 2026-07-15T14:04:27.222187+00:00 | `80be34d17` | Cannot resolve: object absent |
| #16593 — Protect review-lesson pattern memories from memory-dream consolidation/deletion | 2026-07-14T03:20:04.254466+00:00 | `be433f4c9` | Cannot resolve: object absent |
| #16612 — Re-drive promotion idempotently so a record→promote partial failure doesn't strand the threshold crossing | 2026-07-14T03:30:13.334653+00:00 | `36ee5b28d` | Cannot resolve: object absent |
| #16629 — Scope guardrail-task find-or-create by decision so a no-fix-policy promotion can't demote a confirmed task | 2026-07-14T03:40:08.984043+00:00 | `463f17951` | Cannot resolve: object absent |
| #16649 — Stop record() silently writing lessons to the wrong project when session resolution fails | 2026-07-14T03:52:27.294436+00:00 | `2ca2d804a` | Cannot resolve: object absent |
| #16663 — Stop refresh from silently overwriting deliberate short user verification commands | 2026-07-14T04:03:37.232926+00:00 | `230b85ba0` | Cannot resolve: object absent |
| #16679 — Stop regex.test(...) creating phantom JS tests with false high-severity NO_ASSERTION | 2026-07-14T04:14:02.114495+00:00 | `2b91541f4` | Cannot resolve: object absent |
| #16696 — Stop suppression/TODO spans leaking across sibling tests in decorated classes (gate bypass) | 2026-07-14T04:25:09.137379+00:00 | `45df11fde` | Cannot resolve: object absent |
| #16704 — Default normalizeStageRow review_policy to the restrictive value | 2026-07-15T02:51:12.400749+00:00 | `c215be711` | Cannot resolve: object absent |
| #16707 — Stop extractImageSrc classifying bare HTTPS URLs and rooted paths as images | 2026-07-15T02:59:32.676757+00:00 | `c3cb5d4c4` | Cannot resolve: object absent |
| #16710 — Wrap the Makefile-branch _read_text so collect_evidence doesn't crash on a directory/unreadable Makefile | 2026-07-14T04:38:21.078984+00:00 | `8209e0c3f` | Cannot resolve: object absent |
| #16714 — Derive command-palette activity navigation from the activity tab registry | 2026-07-15T04:12:11.909979+00:00 | `2a4b5cffa` | Cannot resolve: object absent |
| #16718 — Stop hook feedback overwriting the status of successful tool calls | 2026-07-15T04:19:14.196547+00:00 | `2d0d61405` | Cannot resolve: object absent |
| #16721 — Eliminate silent message-drop paths in chatMessageMapping | 2026-07-15T04:31:56.794854+00:00 | `7348789b4`, `ebcf8453b` | Cannot resolve: object absent |
| #16724 — Residual orchestration-infra nit sweep after worktree/cron changes | 2026-07-15T13:06:03.978262+00:00 | `38fc40c1f`, `64ed7d96f` | Cannot resolve: object absent |
| #16728 — Stop staging the root-executed firewall script in world-writable /tmp | 2026-07-15T04:39:09.208015+00:00 | `cbfa8d005` | Cannot resolve: object absent |
| #16733 — Fix Claude model auto-selection: honor is_default, scale family rank, include fable | 2026-07-15T04:47:48.973328+00:00 | `f99ddec3a` | Cannot resolve: object absent |
| #16738 — Stop the setup wizard advancing past failures; check and surface every step result | 2026-07-15T05:09:53.194839+00:00 | `94f15acc8`, `f1c5d6f11` | Cannot resolve: object absent |
| #16746 — Fix setup port commit: reject duplicates, make defaults override stale bootstrap ports | 2026-07-15T05:19:48.713381+00:00 | `400392579` | Cannot resolve: object absent |
| #16748 — Unify the three divergent current-stage resolvers with backend state semantics | 2026-07-15T05:37:09.383378+00:00 | `ff5141617` | Cannot resolve: object absent |
| #16751 — Fix lossy AgentsTab payload builders: updates can't clear, duplicate drops fields | 2026-07-15T03:12:02.055595+00:00 | `757256091` | Cannot resolve: object absent |
| #16753 — Replace string-match exception classifier in embedding provider probe | 2026-07-14T18:36:01.520995+00:00 | `6b6912a4d`, `8fc9c062d` | Cannot resolve: object absent |
| #16756 — Guard relativeTime against invalid timestamps fabricated by taskNormalization | 2026-07-15T05:52:40.899980+00:00 | `0849cb210` | Cannot resolve: object absent |
| #16757 — Use mapStoredChatMessage for /api/chat message rows so restored timestamps survive | 2026-07-15T06:02:57.432031+00:00 | `bbbc8d737` | Cannot resolve: object absent |
| #16759 — Gate config Save on ValidationDetectionEditor JSON validity | 2026-07-15T04:14:18.533779+00:00 | `c81a75687` | Cannot resolve: object absent |
| #16763 — Keep the LRU size bound enforced under clock skew in pruneTimeBoundLru | 2026-07-15T06:12:53.911521+00:00 | `abcca5b92` | Cannot resolve: object absent |
| #16768 — Validate /api/providers/models payloads and keep the stale cache on fetch failure | 2026-07-15T06:24:59.011220+00:00 | `dc97a9143` | Cannot resolve: object absent |
| #16772 — Either remove page-hash routing or make back/forward update the remaining page surface | 2026-07-15T06:35:38.517754+00:00 | `4baeeed55` | Cannot resolve: object absent |
| #16779 — Make mapApiMessages fallback ids collision-free | 2026-07-15T06:43:28.338571+00:00 | `59005b476` | Cannot resolve: object absent |
| #16786 — Make the setup resume prompt reachable and handle completed-setup re-runs | 2026-07-15T06:59:21.352862+00:00 | `acb1689b6` | Cannot resolve: object absent |
| #16805 — KnowledgeGraph: distinguish fetch failure from 'No entities found' | 2026-07-15T04:22:33.143587+00:00 | `76c4ceb2d` | Cannot resolve: object absent |
| #16812 — Add repo containment check to expansion file-context path resolution | 2026-07-14T11:44:37.327736+00:00 | `1ca0c56b4` | Cannot resolve: object absent |
| #16818 — Memory search: use server searchMemories instead of client filter over 100-row cap | 2026-07-15T04:45:41.940470+00:00 | `9ec46ec5b` | Cannot resolve: object absent |
| #16819 — Broaden TASK_ID_PATTERNS to match hyphenated/spaced project names | 2026-07-14T12:09:37.412360+00:00 | `c23505f39` | Cannot resolve: object absent |
| #16826 — Cap get_task_diff combined diff size; prefer live stages in current_stage | 2026-07-14T12:22:46.424204+00:00 | `09f048c76` | Cannot resolve: object absent |
| #16828 — Fix offline queue flush: duplicate bubbles and lost injectContext | 2026-07-15T02:53:42.673505+00:00 | `23c470820` | Cannot resolve: object absent |
| #16830 — Move off-ladder typography onto the token ladder across workflows/tasks/dashboard | 2026-07-15T05:02:36.291612+00:00 | `13a8d3704` | Cannot resolve: object absent |
| #16833 — Catch DependencyCycleError and cap recursion depth in tree_builder | 2026-07-14T12:45:44.028698+00:00 | `5daf3c531` | Cannot resolve: object absent |
| #16838 — Guard reconnect backfill by conversation id — old conversation's messages leak in | 2026-07-15T03:07:14.645948+00:00 | `68a421bff` | Cannot resolve: object absent |
| #16839 — Make lifecycle-repair manifest reseed atomic (DELETE + initialize_manifest in one transaction) | 2026-07-14T13:11:17.705261+00:00 | `6d0946407` | Cannot resolve: object absent |
| #16840 — ProfilesTab: confirm Delete and handle save/action failures | 2026-07-15T05:15:59.856023+00:00 | `46e83bf76` | Cannot resolve: object absent |
| #16844 — Move expansion apply idempotency check inside a parent-scoped lock | 2026-07-14T13:39:01.841878+00:00 | `c0bc95e95` | Cannot resolve: object absent |
| #16845 — Guard the initial transcript load against clobbering live WS messages | 2026-07-15T04:12:59.294421+00:00 | `c8dd13edf` | Cannot resolve: object absent |
| #16847 — Promote sub-44px touch targets (incl. ui/Switch) for coarse pointers | 2026-07-15T05:32:08.793867+00:00 | `e446e415b` | Cannot resolve: object absent |
| #16851 — Parse both old and new diff paths in is_doc_only_diff | 2026-07-14T14:02:38.318078+00:00 | `2fdddfa99` | Cannot resolve: object absent |
| #16852 — Abort partially-applied rebase/merge on any sync_from_main failure, not just CONFLICT | 2026-07-15T09:04:40.808093+00:00 | `de55ba1b8` | Cannot resolve: object absent |
| #16853 — Guard useCronJobs/useWorkflows selection fetches against stale responses | 2026-07-15T04:25:22.749395+00:00 | `9974f92f9` | Cannot resolve: object absent |
| #16854 — Remove outline-none focus suppression; adopt inputFocusCls | 2026-07-15T05:46:27.699567+00:00 | `cd384a878` | Cannot resolve: object absent |
| #16856 — Refuse stage-manifest reseed of closed/escalated tasks even under --force | 2026-07-14T14:21:36.344096+00:00 | `e0d4a60a9` | Cannot resolve: object absent |
| #16857 — Invalidate the persisted viewing-session id on 404 | 2026-07-15T04:34:31.741193+00:00 | `b212ba772` | Cannot resolve: object absent |
| #16858 — Delete Gobby-created branches in worktree cleanup paths that pass delete_branch=False | 2026-07-15T09:15:43.296362+00:00 | `d2eac6631` | Cannot resolve: object absent |
| #16861 — Stop smart-context validation feeding unrelated commits to the LLM judge | 2026-07-14T14:32:34.384876+00:00 | `f933ba065` | Cannot resolve: object absent |
| #16862 — Invalidate useFiles git status after saves; clear stale tab error on success | 2026-07-15T04:45:53.282197+00:00 | `5942db153` | Cannot resolve: object absent |
| #16864 — Fix has_unpushed_commits count when no remote-tracking branch exists | 2026-07-15T09:22:46.021809+00:00 | `45ede3cf7` | Cannot resolve: object absent |
| #16867 — Thread session project_id/project_name into session-end auto_link_commits | 2026-07-14T14:58:26.336089+00:00 | `3ba15f3da` | Cannot resolve: object absent |
| #16868 — Restore the 30s dead-stream failsafe (reconnect timer cancels it after 2s) | 2026-07-15T04:53:39.112958+00:00 | `c9d34bb81` | Cannot resolve: object absent |
| #16869 — Reset FilesPage diff state when switching file tabs | 2026-07-15T06:02:10.010998+00:00 | `5f2fd592d` | Cannot resolve: object absent |
| #16871 — Nit sweep: tasks | 2026-07-14T17:09:37.406481+00:00 | `0b3ed7b5e` | Cannot resolve: object absent |
| #16873 — Treat missing validation_criteria on leaf code tasks as a hard validation gate | 2026-07-14T15:33:38.281984+00:00 | `08eb38539` | Cannot resolve: object absent |
| #16874 — Stop chat_error reusing the assistant message_id — duplicate message ids | 2026-07-15T05:01:00.000193+00:00 | `ebc39e867` | Cannot resolve: object absent |
| #16877 — Stop writing cumulative session totals into context-usage occupancy | 2026-07-15T05:11:59.427055+00:00 | `8ee763eff` | Cannot resolve: object absent |
| #16878 — Unify divergent validation implementations and record history on the live path | 2026-07-14T16:10:22.530122+00:00 | `2fa7eee0d` | Cannot resolve: object absent |
| #16880 — Rule enable/disable toggle: surface failures and add keyboard semantics | 2026-07-15T06:25:50.705450+00:00 | `587e1e7ec` | Cannot resolve: object absent |
| #16883 — Validate task_type and implementation_domain in validate_compiled_spec | 2026-07-14T16:28:35.050615+00:00 | `b9e271df9` | Cannot resolve: object absent |
| #16884 — Surface failed messages fetch during session load instead of 'empty transcript' | 2026-07-15T05:25:28.818957+00:00 | `2143bcd81` | Cannot resolve: object absent |
| #16887 — Settings dialog: add Escape close, focus trap, and initial focus | 2026-07-15T06:37:31.389332+00:00 | `f34e3c5db` | Cannot resolve: object absent |
| #16888 — Wire run_expansion_qa into compile_run or delete the dead _qa module | 2026-07-14T16:46:12.611521+00:00 | `4c78b743c` | Cannot resolve: object absent |
| #16890 — Token events: key dedupe and the since-cursor on stable identity, not timestamps | 2026-07-15T05:41:06.179671+00:00 | `1c3613681` | Cannot resolve: object absent |
| #16892 — SidebarPanel: untabbable when closed; dialog role/trap/restore when open | 2026-07-15T06:55:42.840086+00:00 | `ff50ceba9` | Cannot resolve: object absent |
| #16893 — Nit sweep: web components B | 2026-07-15T07:16:16.607444+00:00 | `e21e29de8` | Cannot resolve: object absent |
| #16896 — Nit sweep: cli-build-ops (pipelines / workflows / clones / merge / cron / pack / services / skills) | 2026-06-12T04:54:11.444361+00:00 | `1e79b5777` | Cannot resolve: object absent |
| #16899 — Residual web-core nit sweep after chat-primary/source-control cleanup | 2026-07-15T08:16:59.564005+00:00 | `33bd42350` | Cannot resolve: object absent |
| #16915 — Require confirmation (or undo) before palette Backspace deletes a session | 2026-07-15T02:49:57.284915+00:00 | `57b560847` | Cannot resolve: object absent |
| #16916 — Restore the focus indicator on LoginPage inputs | 2026-07-15T02:58:44.029731+00:00 | `f83cf3eab` | Cannot resolve: object absent |
| #16917 — Restore the focus-visible ring on activity-panel search inputs | 2026-07-15T03:04:30.654974+00:00 | `27f9616b0` | Cannot resolve: object absent |
| #16918 — Show the newly selected task's ref while its detail loads | 2026-07-15T04:13:13.779133+00:00 | `cdb4cf00d` | Cannot resolve: object absent |
| #16919 — Snap off-ladder typography to --text-* tokens and extend the ladder test to TSX | 2026-07-15T04:25:27.409527+00:00 | `cfdddf6df` | Cannot resolve: object absent |
| #16920 — Stop live refresh and search transients reassigning the watched session | 2026-07-15T04:35:40.454250+00:00 | `c8e167523` | Cannot resolve: object absent |
| #16997 — Apply memory recall fix to review worktrees | 2026-06-11T21:52:22.035159+00:00 | `0105b84e6`, `601305ab2`, `8ad74b95b`, `bf5dcccec`, `cfab9935f` | Cannot resolve: object absent |
| #17052 — Cherry-pick shutdown fix into review worktrees | 2026-06-11T23:05:06.830900+00:00 | `478f55286`, `5ca11f016`, `918b6d78a`, `b1cdaf6ce`, `d803fe1f6` | Cannot resolve: object absent |
| #17054 — Cherry-pick Gemini Flash feature changes into review worktrees | 2026-06-12T00:51:45.661782+00:00 | `1b29f4014`, `94dad5bdb`, `b3d3f9539`, `bb4e657c9`, `da2f5e039` | Cannot resolve: object absent |
| #18039 — Surface and complete failed merge_worktree abort cleanup | 2026-07-12T18:14:09.091851+00:00 | `ef33f1396` | Cannot resolve: object absent |
| #18083 — Replace timing sleeps and mock-only executor assertions | 2026-07-12T21:25:24.589661+00:00 | `631ef9159`, `a63375657` | Cannot resolve: object absent |
| #18102 — Clear gobby-core project-root Clippy question-mark warning | 2026-07-15T01:20:10.353552+00:00 | `05cb0fc0` | Cannot resolve: object absent |
| #18172 — Correct review-learning guidance and evidence parsing edge cases | 2026-07-14T05:25:53.738649+00:00 | `14739fce3` | Cannot resolve: object absent |
| #18173 — Skip the legacy review-lesson scan when tagged matches satisfy the recall limit | 2026-07-14T05:40:26.163096+00:00 | `967a4102f` | Cannot resolve: object absent |
| #18174 — Canonicalize empty path tags and fallback lesson identities | 2026-07-14T06:02:34.811885+00:00 | `abe45ad68` | Cannot resolve: object absent |
| #18175 — Prune stale promotion metadata and bound guardrail task labels | 2026-07-14T06:19:55.115924+00:00 | `fe5f93ae8` | Cannot resolve: object absent |
| #18176 — Document the installed review-learning rule group | 2026-07-14T06:33:02.465719+00:00 | `56c352a8e` | Cannot resolve: object absent |
| #18177 — Harden Whisper temporary audio handling and remove dead state | 2026-07-14T06:47:05.952191+00:00 | `72aafd04d` | Cannot resolve: object absent |
| #18178 — Surface TTS provider factory failures and remove the unused provider listing API | 2026-07-14T07:06:20.063952+00:00 | `3d4933685` | Cannot resolve: object absent |
| #18179 — Suppress zero-length Chatterbox audio frames | 2026-07-14T07:28:25.068927+00:00 | `3967238d6` | Cannot resolve: object absent |
| #18180 — Harden project-verification mutating-command and frontend classification | 2026-07-14T07:47:21.573555+00:00 | `3154037d8` | Cannot resolve: object absent |
| #18181 — Preserve evidence-derived confidence for existing verification commands | 2026-07-14T08:06:02.964844+00:00 | `91451c5a8` | Cannot resolve: object absent |
| #18195 — Refactor src/gobby/sync/tasks.py below 1000 lines | 2026-07-15T02:59:17.750882+00:00 | `6439f87b4` | Cannot resolve: object absent |
| #18270 — Rebase comp-b lane onto 0.5.0, re-resolving cron-storage test collision | 2026-07-15T02:48:56.707203+00:00 | `4910abced` | Cannot resolve: object absent |
| #18275 — Rebase comp-b lane onto 0.5.0 post-comp-a merge, resolving overlap conflicts | 2026-07-15T05:59:06.084762+00:00 | `9810b5e31` | Cannot resolve: object absent |
| #18276 — Rebase web-core lane onto 0.5.0 post-comp-a merge, resolving overlap conflicts | 2026-07-15T06:07:49.531690+00:00 | `78fc166fe` | Cannot resolve: object absent |
| #18277 — Fold #16869 diff-state fix from retry fork into comp-b lane, integrating with #16681 dirty-tab logic | 2026-07-15T06:13:49.401485+00:00 | `dafdcb44c` | Cannot resolve: object absent |
| #18294 — Reconcile agent-YAML model-spec test drift (gpt-5.5 vs gpt-5.6-sol) | 2026-07-15T14:09:15.822644+00:00 | `b06b1a9c3` | Cannot resolve: object absent |
| #18295 — Fix transport session-resolution tests for timeout=30.0 dispatch kwarg drift | 2026-07-15T14:17:59.900739+00:00 | `8cbcef4a1` | Cannot resolve: object absent |
| #18650 — Observe bounded evidence validation on oversized scratch diff | 2026-07-20T23:17:12.154242+00:00 | `7fdd6888c` | Cannot resolve: object absent |
| #19008 — Fix CodeRabbit packet 10: Runner, dispatch, storage, and core contracts | 2026-07-28T08:37:55.263019+00:00 | `479f36f37` | Cannot resolve: object absent |
| #19386 — Repair hijacked migration slot 346 + drop orphan tmux tables | 2026-07-31T10:42:45.294656+00:00 | `01fa44ffd`, `04e82b39c`, `78f2f6859`, `88aa2d61d`, `8d3372bf7` | Cannot resolve: object absent |
| #19389 — Destructive-migration gate in the runner | 2026-07-31T08:07:20.596158+00:00 | `46cc8d2f9` | Cannot resolve: object absent |
| #19391 — Shared maintenance epoch: DB-enforced fence, orchestrator, ledgers | 2026-07-31T07:21:24.980323+00:00 | `e0d8fd51c` | Cannot resolve: object absent |
| #19392 — Decompose runner_maintenance.py into a package | 2026-07-31T07:02:40.488652+00:00 | `7363205a5` | Cannot resolve: object absent |
| #19393 — Rewrite claims reader off workflow_states | 2026-07-31T07:55:00.489619+00:00 | `ca5ab5d87` | Cannot resolve: object absent |
| #19396 — gobby_test_* schema hygiene: leaked-schema drops + leased startup sweep | 2026-07-31T10:28:26.964930+00:00 | `f5676a552` | Cannot resolve: object absent |
| #19397 — Delete dead Python files | 2026-07-31T08:07:47.899190+00:00 | `e6e7e8d31` | Cannot resolve: object absent |
| #19399 — Remove no-op CLI/API surfaces | 2026-07-31T10:22:59.604263+00:00 | `c8503e5ba` | Cannot resolve: object absent |
| #19400 — Remove dead config fields incl. CodeIndexConfig disposition | 2026-07-31T08:21:55.915342+00:00 | `6c585a4a2` | Cannot resolve: object absent |
| #19402 — Consolidate duplicate utilities, _sanitize_url first | 2026-07-31T09:25:12.298574+00:00 | `d82390a55` | Cannot resolve: object absent |
| #19403 — Consolidate webhook stacks onto httpx; drop aiohttp | 2026-07-31T10:40:20.280368+00:00 | `d44bdc5f2` | Cannot resolve: object absent |
| #19405 — TTS: remove voice=[] extra; pin no-torch-at-import | 2026-07-31T07:00:38.465620+00:00 | `5a3cf3710` | Cannot resolve: object absent |
| #19406 — Remove dead Rust mediawiki/wayback + dead deps | 2026-07-31T08:47:30.509625+00:00 | `4061d1048` | Cannot resolve: object absent |
| #19408 — Retire template-registry chaff, DB registry verified | 2026-07-31T09:47:38.597460+00:00 | `96a4afbca` | Cannot resolve: object absent |
| #19409 — Decompose the 3 oversized gcode files | 2026-07-31T08:52:04.953579+00:00 | `0db21e729` | Cannot resolve: object absent |
| #19413 — Remove legacy secrets/auth migration paths | 2026-07-31T08:10:55.289287+00:00 | `4ba93d1e1`, `cb5fd2ab1` | Cannot resolve: object absent |
| #19417 — Delete scratch files + doc fixes | 2026-07-31T07:04:05.782774+00:00 | `c51f4e4db` | Cannot resolve: object absent |
| #19436 — Patch admitted_database_url in create_base_patches to fix 15 runner lifecycle tests | 2026-07-31T07:26:03.159278+00:00 | `4cf36d4b4` | Cannot resolve: object absent |
| #20609 — Fix gcore migration and gwiki contract findings | 2026-08-20T00:05:21.188419+00:00 | `c2405da688` | Cannot resolve: object absent |
| #20610 — Fix Python runtime and security CodeRabbit findings | 2026-08-20T01:09:55.098595+00:00 | `e3ff32f3de` | Cannot resolve: object absent |
| #20612 — Fix memory providers sessions storage telemetry and workflow tests | 2026-08-20T01:31:54.236646+00:00 | `9b337dfb06` | Cannot resolve: object absent |
| #22034 — Install standalone GobbyAI Crane with streaming Qwen voice cloning | 2026-09-10T02:33:13.097459+00:00 | `b0cc5fe`, `cf55799`, `f56647f` | Cannot resolve: object absent |

## Invalid linked object

| Task | Closed (UTC) | Linked reference | Evidence and verdict |
| --- | --- | --- | --- |
| #14665 — Update Rust repo documentation | 2026-05-23T13:13:59.542923+00:00 | `8e06661` | Resolves to blob `8e066616ddc2d9dd15ed05612e6bef4fda6eaad2`, not a commit. No commit ancestry/holding branch verdict is possible. |

## Read-only audit boundary

The audited task records, branches, worktrees and source files were not modified. The live fleet advanced integration independently; all membership and patch comparisons use the immutable sweep pin above. The only new Git branch created by this audit is this seat's report branch, and the only commit proposed is this report file, under the Orchestrator's explicit report-only exception relayed by Lane Manager gobby#16007. Research inputs/cache remain local and uncommitted. No audited branch or worktree was retired, and no audited task was reopened, relinked or edited. The Orchestrator independently filed the three recovery tasks documented above.

## Semantic recovery review: batch 1 (15 of 89)

Task #24099, **Semantic recovery review of the 89 unproven unlanded tasks**,
continues the original audit. This batch checks the 15 most recently closed
tasks in the remaining set, ordered by the MCP snapshot's `closed_at` descending
and then task number descending. The original sweep tables remain historical
inventory; the classifications below replace their unproven semantic verdicts
for these tasks.

All current-source claims in this follow-up use one immutable integration pin:

- Branch at observation: `0.5.0`.
- Commit: `4ccb3e966ed18fe13cd9f268d55143128d034048`.
- Tree: `428e1e0a2babda4fe2edf5c6bf56ae4950dbaf43`.
- Gcode checkout project: `19b73420-3946-5c6a-8a06-4c7522f77543`.
- Checkout: `/Users/josh/.gobby/worktrees/gobby/lane-4-close-out-1`.
- Source reads: bound `gcode evidence` ranges, with all three binding fields.
  Historical changes and holding refs: read-only Git.
- Task inventory: the same MCP-exported snapshot and original ignored analysis
  cache used by the sweep. Filtering `needs_inspection` tasks, excluding the six
  already checked and #23269 **Node bootstrap refusal**, reproduces exactly 89.
  The cache is unchanged and is not a deliverable.

“Delivered” describes the revision's behavior or document correction in the
pinned source, not live activation or a fresh test pass. “Intentionally
superseded” requires a named replacement or explicit withdrawal. “Missing”
means the concrete revision content remains absent; small probe artifacts are
identified as artifacts rather than product defects.

### Batch 1 classifications

| Task | Unproven revision(s), unique prefixes from the original full-SHA inventory | Classification | Current evidence and disposition |
| --- | --- | --- | --- |
| #23225 — Decompose the near-ceiling gclient files | `3a2f06df00` | delivered | Same task's replacement `5eebb797287e1a66747154ccb534e477970f45be` is an ancestor of the pin. Gcode outlines locate the complete adoption group in `live_loop/local_adoption.rs:14–320`, lifecycle group in `live_loop/lifecycle.rs:26–283`, and disconnect methods in `app/disconnect.rs:8–65`. The module declarations are in E1–E2. Git blob line counts for the four moved-from files are 795, 189, 653 and 826, all below the task's 850 ceiling. The old pure-move draft needs no recovery. |
| #22856 — Remove dead tmux spawn and launch code | `6b15e2c086` | intentionally superseded | This revision only deletes the placed-test fixture's obsolete registration-timeout patch. #23364 **Retire the dead registration timeout knob**, landed `f9a32c47a7d835fb868ec199cf72191abdaab4b3`, instead replaces that key with the still-supported `tmux.init_timeout_seconds` so the deliberately unregistered stub remains held. E3 shows the replacement; `src/gobby/config/tmux.py:67` describes the initialization timeout as provider-failure handling. Replaying the deletion would remove the fixture's valid initialization guard. |
| #23076 — gclient reconnects straight to the host | `ecf27cff5a` | intentionally superseded | The sole unproven revision changes only Cargo.lock, Cargo.toml and the installer floor from 0.1.16 to 0.1.17. E4–E5 show current crate 0.1.35 and floor 0.1.34; neither should be reverted to 0.1.17. Later #24077 **Model alias sidebar fix**, `db6fdf3a5d`, owns the current crate bump. This verdict concerns the version-only revision, not a new assertion about every reconnect criterion. |
| #22895 — Plan runbooks as existing pipelines with placed agent launch | `0902d3c0b2`, `523f487870` | intentionally superseded | These are rounds 1–2 of the old deploy_runbook service, ledger and document design. E12 explicitly withdraws that design and uses existing pipeline execution/step records; E20 records “A runbook is a pipeline.” Landed same-task rewrite `3aa814ece6` and later same-task manifest/completion commits replace the drafts. No old deployment table/service should be recovered. |
| #23303 — Rebase BEFORE_AGENT cancellation and strip daemon logging | `e2f9674a13` | delivered | Landed same-task candidate `bb308056a0364bbb17d6b73f2a5575557ea78f09` is an ancestor. E8–E11 retain child/evaluation cancellation separation, effect-safe failure gates, compact hydration release, and shielded shared embedding futures. E13 retains database-backed BEFORE_AGENT confirmation and bounded resubmission. `compact_continuation.py:608` calls it; `src/gobby/workflows/hooks.py:304–305` enriches the existing cancellation warning with session/source. The exact receipt-safety test blob from this revision is also present at the pin. |
| #23192 — Reconcile front-door plan with contract-corpus slice plan | `3f8d7baf3b` | delivered | This merge carries the #22951 **Front-door plan repairs** into the slice-based plan. E14 records those repairs; E15 retains P2/P3 pointers and dependency reconciliation, and E16 records renewed slice consensus. Landed same-task `9bd9576342` and later index/manifest repairs carry the approved plan. The merge draft itself is unnecessary. |
| #23246 — Pool exhaustion diagnosis | `2c88be74b6` | missing | The sanitized psycopg warning routing is delivered (E7), but the revision's rate-limited, nonblocking `pg_stat_activity` census is absent. E6 shows the final backoff failure logging only pool stats and re-raising; indexed searches find no `ACTIVITY_CENSUS` or census helper in the hub source, and no ancestor introduces that constant. See the concrete recovery record below. |
| #22706 — BEFORE_AGENT evaluation cancellation and continuation confirmation | `0a17973545`, `0ebabda962`, `240d4d698f`, `312efa5f8f`, `39774017b2`, `437f829bfb`, `4609f492b4`, `66c9fc4672`, `9093dfb2d6`, `ba2d1c3e54`, `bdcf9cf761` | delivered | #23303 **Cancellation recovery under the logging ban** is the explicit recovery vehicle in its MCP task contract and landed candidate `bb308056a0`. E8–E13 show the corrected mechanism, preserved effect outputs/failure blocking, hydration gate, and confirmed retry. Diagnostic-only draft logger calls are intentionally excluded by that task's explicit logging ruling; per-retry logging was superseded, not lost. The original 26 incidents remain UNKNOWN, as the contract requires. |
| #23058 — Refresh front-door plan for Ask retirement and migration renumber | `3792fa484a`, `efe2a739fd` | delivered | E16 records the Ask-only Targets and acceptance items as moot after #23055 **Ask retirement**. The signed-golden correction is now owned by the executable API-keys slice: E17 points to it, E18 requires the five signed goldens and new schema identity, and E19 preserves the malformed-field rejection test rather than treating that negative vector as parseable. Old migration numbers and M1 bytes are superseded by the active slice. |
| #22951 — Refresh reviewed front-door plan for current-code expansion | `04c3b8cc6d`, `4573796764`, `753fb33102`, `9774514ae2`, `c7274d2be2` | delivered | E14 retains the complete repair/consensus record: runner ownership until supervisor cutover, per-case backend state, credential-reader sweep, per-hello key refresh/drain, shutdown ordering and SIGKILL topology bounds. E15–E17 show later slice execution authority. The same-task landed repairs and #23192 **Front-door slice reconciliation** carry these decisions; the stale draft manifests should not be restored. |
| #22764 — Frontend load races and timed-out test cascades | `b5e5fe5837` | missing | The reconnect effect still issues another `viewSession` while an initial REST restore can be in flight (E21); it lacks the revision's settled-request barrier. E22 also returns an imported hook after `vi.resetModules()` without checking which test context owns the globals. Both fixes and their new regressions are absent; this is a source comparison, not a fresh loaded-suite reproduction. See recovery record below. |
| #22940 — Reconcile runbooks migration references with occupied schema head | `769e42d910` | intentionally superseded | #22942 **Archive retired runbooks plan**, `c0c23f6e99c686d79f70aa55b5ef9b6ac0b31b42`, explicitly archives the earlier plan at `completed/runbooks.md`. E12 and E20 name the active pipeline-based runbook contract. The historical archive lacks this revision's migration-455 and task-edit-policy annotations; they are lost historical edits to a retired plan, not an executable migration obligation. Restoring or re-expanding the retired plan is unnecessary. |
| #22761 — gclient User guide | `4cc1aad245` | delivered | E23 describes the current menu bar, pinned/overlay sidebar and concrete menus; E24 describes four startup stages and daemon retry. These are the guide's substantive Chrome additions, updated by later #22944 **Chrome polish** and #23626 **Client chrome refresh** changes. The old “menus during splash” wording is intentionally replaced by the fullscreen splash followed by chrome on failed health. |
| #22434 — Droid probe: blocker round trip | `31f53f3166` | missing | The revision adds exactly the one-line `docs/droid-probe-blocker.md` artifact containing `marzipan` (blob `4255d2b5132aba6c68ce3487a68962ce16fb8522`). The path is absent from the pinned Git tree. This is lost probe documentation, with no asserted product impact. |
| #22426 — Chocolate chip cookie recipe reference | `d95a76a505` | missing | The revision adds the 32-line `docs/chocolate-chip-cookies.md` reference (blob `f67181c60a27fe0ba8f44fb3d9d0f081264015f6`). The path is absent from the pinned Git tree, and the commit has no holding ref. This is a missing requested document, with no asserted runtime impact. |

Batch totals: **7 delivered, 4 intentionally superseded, 4 missing**.
**74 tasks remain** for subsequent batches. No audited task, source file, holding
branch or worktree was changed. The seat's authorized report branch is the only
new branch, and this addendum is its only edited path.

### Batch 1 recovery referrals

These records preserve the exact absent change and holding location for the
Orchestrator; they do not authorize replay of the whole historical branch.

1. **#23246 — Pool exhaustion diagnosis**:
   `2c88be74b6f6ad12f531bf93480000f61ca06725`,
   branch `task-23246-pool-census`. Missing the at-most-one-per-minute census
   thread after final acquisition failure, bounded direct connection/query
   parameters, typed failure reporting and original-PoolTimeout preservation.
   Sanitized pool warning routing is already present and needs no duplicate fix.
   Recovery must reconcile the current operation-deadline path and current
   logging policy; the old code is a reference rather than a replay instruction.
2. **#22764 — Frontend load races**:
   `b5e5fe5837ce8438b8df5ae41b64d6bbdcb1549e`,
   branches `task-22764-usechat-sessions-races` and
   `review-r3-c22764-778bd67`. Missing the settled initial-restore barrier
   before reconnect retry and ownership check preventing a timed-out test from
   importing/rendering against another test's globals, plus the revision's
   focused regression cases.
3. **#22434 — Droid blocker-round-trip probe**:
   `31f53f3166cc4e00bee8d5469b58d1a7cd84013e`.
   No holding branch; retained at `refs/gobby-evidence/task-22434`.
   Missing `docs/droid-probe-blocker.md` and its parent-supplied probe word.
   Artifact-only recovery may be disposed by the Orchestrator.
4. **#22426 — Cookie recipe reference**:
   `d95a76a5054c36ed53edc7e30da467be99d47733`.
   No holding branch or other ref; the commit object remains available.
   Missing `docs/chocolate-chip-cookies.md`.
   Artifact-only recovery may be disposed by the Orchestrator.

### Batch 1 pinned evidence

Each SHA-256 hashes the raw excerpt bytes returned by the bound gcode read.
Line ranges refer only to the immutable follow-up pin above. Outlines locate the
moved functions; Git ancestry, blob-presence and holding-ref checks supplement
the source evidence without claiming fresh runtime validation.

| ID | Pinned source range | Excerpt SHA-256 |
| --- | --- | --- |
| E1 | `crates/gclient/src/app/live_loop.rs:40–55` | `ebabf045f5b467a8725a70fa5a03c8162f5ddcc365e3d6b64182c3b7510fa4a5` |
| E2 | `crates/gclient/src/app/mod.rs:1–12` | `7c83de8c1d310838088c81cfeb9e650db4930c9486dd6edf772c5e9cd7b616c7` |
| E3 | `tests/workflows/test_placed_pipeline_fixture.py:178–212` | `2ffa1214006ed29c852e0f6929a059e6bb1093d5d589d93c4ead46a1bc90ace2` |
| E4 | `crates/gclient/Cargo.toml:1–8` | `22c8c84fed03bd2afc6631868c3c1a81e4727893fc91fec1bf78be4e7fe55bed` |
| E5 | `src/gobby/install/version_pins.py:4–18` | `191e9929aea96bc0bdf87b8701fc6926c2dd69de0d170d8b3708c90a93921c49` |
| E6 | `src/gobby/storage/hub/postgres_pool.py:190–222` | `c31513f4f75f2b7c8a7b17f03bbb73f5ba1965d30e4eb12b313d14c1ff6fea97` |
| E7 | `src/gobby/telemetry/logging.py:550–566` | `4ed2e51f01e24711ffb35b0e8de8b04ef46c585c03bfbe65c5ac79b426c8c97b` |
| E8 | `src/gobby/workflows/evaluation_runtime.py:126–147` | `f3292a25c99cc156b540b632c91957c47ead9138b824e6f3cca3f3e88931292e` |
| E9 | `src/gobby/workflows/engine/evaluation.py:740–767` | `c68e48175db9525fa87eeb5b1b8bc92cdd5003f1f31e31c00f7d8576e2376fb8` |
| E10 | `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml:147–168` | `362a61b5ebe3ece6f7f79eb8e9e5d3fc4ecbe8db01704778f31560d1307ff8c1` |
| E11 | `src/gobby/ai/embedding_cache.py:148–169` | `f4615f08f9f614d5f90e7bbcad3b58de66b989e0850331f4ad84c1f74654dd5a` |
| E12 | `.gobby/plans/deploy-runbook.md:7–36` | `3b529f82fd3117611d3e5bce6d0868502f2efa7857196965b86d486193be4298` |
| E13 | `src/gobby/sessions/continuation_retry.py:201–240` | `9486cc5966b5bf9612b3e37a85bea91d0e1a5889f6ce98a0fdaf08b9f46c4145` |
| E14 | `.gobby/plans/gdaemon-front-door.md:1896–1964` | `b98bede99da0136cb70a100f0302ede10d7c48091c28b3f618b3dace50ff29a6` |
| E15 | `.gobby/plans/gdaemon-front-door.md:436–461` | `32b0f95394c16d6226a2a610b373a146032a3bdfca88abea73f967c9799641c2` |
| E16 | `.gobby/plans/gdaemon-front-door.md:1976–2003` | `6af3fd2288923e922fb2e25649e411f73c9dbb82a8a7a07e689e904144b4e3de` |
| E17 | `.gobby/plans/gdaemon-front-door.md:665–668` | `640d0bcaa3270cd654c64373c5b6999edddec31a7b5cd0a4d28edf4f5b35e825` |
| E18 | `.gobby/plans/gdaemon-api-keys-nodes.md:726–737` | `7d783a6835b7ef69d95fbed7ce9e92c7e8186c0d7379ec3c058e5094746af68b` |
| E19 | `tests/runtime_grants/test_golden_vectors.py:178–197` | `91237eaf5a2594284e43f63788acc6195287b525b020a3d85788738f4d50a40e` |
| E20 | `.gobby/plans/deploy-runbook.md:37–54` | `090e3261fa15d3f594cca07f8dcc6d39391816ae3a81935008c2b50063ecab18` |
| E21 | `web/src/hooks/useChat/sessionViewing.ts:380–414` | `db9dedd40e263812ae330d3d3e9cf7c183cac6546e5f90e7e279a46d3eb44aab` |
| E22 | `web/src/hooks/__tests__/useChat.setup.ts:70–91` | `c1c4dca42b59d062369c55891023484531c7ef1325b7ef8b6ef7d38497b25f7a` |
| E23 | `docs/guides/gclient-user-guide.md:65–90` | `63e745e91f02cd9ec1ea8beaf26dbb54aab6963df8913f52f0084cd4e263e72c` |
| E24 | `docs/guides/gclient-user-guide.md:955–981` | `48df16e177e4c7ea9ce57e4b6beb746a737b296a34fa6974208eab1a259df837` |

## Semantic recovery review: batch 2 (30 of 89 cumulative)

This addendum reviews positions 16–30 in the same newest-first snapshot order.
Current evidence remains bound to integration commit
`4ccb3e966ed18fe13cd9f268d55143128d034048`, tree
`428e1e0a2babda4fe2edf5c6bf56ae4950dbaf43`, and gcode checkout project
`19b73420-3946-5c6a-8a06-4c7522f77543`. The Lane Manager directed a clean
fast-forward of the existing report branch to landed batch 1 tip
`9eb45d3d93b35568e2279050afc1200d8cd0876d`; the evidence pin stays fixed.

Batch 1 dispositions: #23246 **Pool exhaustion diagnosis** is routed to
#24103 **Recover pool census**; #22764 **Frontend restore races** is routed to
#24104 **Recover frontend restore races**, both in Lane 7. The Orchestrator
chose no recovery for #22434 **Droid probe artifact** or #22426 **Recipe reference**.
Their historical missing classifications remain accurate. The two citation
nits from batch 1 are corrected in place.

### Batch 2 classifications

| Task | Unproven revision(s), unique prefixes | Classification | Pinned evidence and disposition |
| --- | --- | --- | --- |
| #22389 — Found-work owner message counting | `486fb22d37` | delivered | The sole unproven revision actually removes retired GitHub/Linear INSERT columns and rejects non-null legacy integration arguments. It does not change message counting. B1 retains the column removal and goes further by removing the legacy arguments altogether. This verdict addresses the actual revision. |
| #22326 — B-group test drift repair | `624e3623dd` | delivered | B2 retains the richer deferral shape, including title, depends_on and acceptance prose. B3 retains the 260k deadline guidance tier. B4–B5 seed indexed projects for both review fixtures. All four test corrections survive; no fresh test pass is claimed. |
| #22319 — Isolated daemon health wait | `ae65650a5e` | delivered | B6–B7 retain monotonic polling, ten minimum attempts, bounded probes, categorized failures and log-tail diagnostics. The budget is now 75 seconds, beyond the draft's 60 seconds. Git comparison of `tests/e2e/test_daemon_health_wait.py` from this revision to the pin adds 97 lines and removes none, preserving its focused regressions. |
| #22071 — Feedback synthesis report 15052df6 | `c06f8df8e4` | missing | The 53-line historical report is absent at the pin. Exact path, SHA and blob appear below. Report loss does not establish that underlying database synthesis actions were undone. |
| #22069 — Dream synthesis report c19e1126 | `8370c53dea` | missing | The 76-line historical report is absent at the pin. Exact path, SHA and blob appear below. No runtime or database loss is inferred. |
| #22040 — Feedback synthesis report e3b255c6 | `e9cdbca0c3` | missing | The 67-line historical report is absent at the pin. Exact path, SHA and blob appear below. No runtime or database loss is inferred. |
| #22051 — Feedback synthesis report ef48b0b1 | `26aa30ddb8`, `dbe7162df6` | missing | Both historical report postimages, 237 and 236 lines, are absent at the pin. Exact paths, SHAs and blobs appear below. No runtime or database loss is inferred. |
| #22039 — Dream synthesis report 49640230 | `dea81c11c8`, `f7cac386b2` | missing | Both historical report postimages, 46 and 48 lines, are absent at the pin. Exact paths, SHAs and blobs appear below. No runtime or database loss is inferred. |
| #21820 — Fair archive-backed summary retries | `eb53e743b4` | delivered | Same-task landed replacement `c3e0e7d33695b9c19a01d5dd4bd996dfbb722f18` is an ancestor of the pin. B8–B10 retain exclusive (created_at,id) pagination, wraparound, cursor advance in finally even on failure, and archive-aware TranscriptReader construction. The failure ceiling also limits deterministic poison-session retries. |
| #21794 — Handoff rollout and replay qualification | `12abc42f16`, `20308164c0`, `fa12a9de43` | missing | Partial loss, corrected after batch 3 review: `12abc42f165a18d8f6f0ef004a7d12b985cecb03` independently documents `get_handoff_record` and `search_handoffs`, including project-default search and non-consuming historical reads. Those docs follow the missing #21792 **Read-only handoff queries** into #24121 **Handoff retrieval recovery**. Generated Source Records, provenance/coverage fields, full-source replay qualification and the two replay-only revisions remain intentionally retired under #21693 **Structured handoff delivery history** and #21887 **Incidental handoff history policy** (B11/B12). Adapt the query documentation to the surviving authored-record contract; do not restore the retired rollout wholesale. |
| #21875 — Optional review candidates in recovery | `0acc7cef77` | missing | The generated recovery/combination mechanism is intentionally superseded by B11–B12, but this revision also adds six fidelity lines to the live archival-summary prompt and its golden fixture. Those lines are absent in B15. The missing half prevents a whole-task superseded verdict. See the exact recovery referral below. |
| #21881 — Operation qualifiers in recovered facts | `5fa4992e9e` | missing | The generated recovery/combination mechanism is intentionally superseded, but six operation, actor, condition, reference-kind and source-inspection fidelity lines added to the live archival-summary prompt and golden fixture are absent in B15. B11 explicitly leaves archival-summary behavior unchanged. See the exact recovery referral below. |
| #21850 — Reference fidelity and inherited attribution | `9b925f9c27`, `d10d2f7f6f` | missing | The generated reducer, validation and combination changes are intentionally superseded. The d10d2f7f6f revision also adds two exact-identifier and inherited-attribution lines to the live archival-summary prompt and golden fixture; B15 lacks them. No explicit withdrawal covers those live edits. See the exact recovery referral below. |
| #21860 — Generated recovery output headroom | `410c950601` | intentionally superseded | The revision targets 6,000 characters beneath a 12,000-character generated-recovery limit. B11 replaces that path with authored handoffs. B13 documents the active 10,000 JSON-escaped hard cap and approximately 5,000-character authoring target. These are different operations and budgets; the old reducer's numeric instructions are not claimed as delivered. |
| #21867 — Session and example exclusion from task evidence | `103a07c698` | intentionally superseded | The revision filters session identities and displayed examples from generated transcript Source Records evidence. B11 explicitly places that ledger and source-position semantics outside the replacement contract. B12 loads bounded DB-owned task evidence for authored content. The absent transcript-selection/evidence modules should not be replayed into that design. |

Batch totals: **4 delivered, 2 intentionally superseded, 9 missing**.
Cumulative totals: **11 delivered, 6 intentionally superseded, 13 missing**;
**59 tasks remain**. B11 supersedes the generated-recovery halves of the handoff revisions. B14 shows
archival summaries still use a separate Markdown generator; no claim is made
about installed prompt registry rows or live semantic behavior. No audited task,
source path, holding ref or foreign worktree was mutated.

### Batch 2 recovery referrals

These five closely aligned losses are historical report artifacts. All seven
revisions are readable Git objects but have **no holding branch or other ref**
at inspection. Neither rerunning nightly jobs nor replaying their database
mutations is implied. The Orchestrator owns the recovery disposition.

| Task and title | Unlanded full SHA | Missing path | Historical blob and line count | Holding branch |
| --- | --- | --- | --- | --- |
| #22071 — Feedback synthesis report 15052df6 | `c06f8df8e42f2a1362a8f8717fd8a9de1e134ecb` | `docs/reports/feedback/15052df6-3b06-450d-9f4e-aa75b4e03917.md` | `0a48b3dad4a27fdb616bfd2f063fd928195be486`, 53 lines | none |
| #22069 — Dream synthesis report c19e1126 | `8370c53dea76479567e359677c0126e909587764` | `docs/reports/dream/c19e1126-cbf4-436e-b956-ada5631bd49c.md` | `675f395b5367bb28b7c206646e75f51267dab896`, 76 lines | none |
| #22040 — Feedback synthesis report e3b255c6 | `e9cdbca0c37dc5bc406732963a4e1c56e35b114a` | `docs/reports/feedback/e3b255c6-044e-48fc-a49b-297d821d016a.md` | `336cefddb8eee790c8e74b06606329ce2ed22e73`, 67 lines | none |
| #22051 — Feedback synthesis report ef48b0b1 | `26aa30ddb88fca28e26f15c6d2adb57e601dcc34` | `docs/reports/feedback/ef48b0b1-dd58-403a-ac84-52a28675d289.md` | `15f815b4c336488fbb64aae2f074517c5c923e4d`, 237 lines | none |
| #22051 — Feedback synthesis report ef48b0b1 | `dbe7162df67a9f00b5df939005d849cc5b19ac70` | `docs/reports/feedback/ef48b0b1-dd58-403a-ac84-52a28675d289.md` | `17812e14ad9850a2643a3823188f8d838e215ee8`, 236 lines | none |
| #22039 — Dream synthesis report 49640230 | `dea81c11c8c746999952a480bbed6321e0580c58` | `docs/reports/dream/49640230-c32a-4547-957c-f23d36e39109.md` | `d797b014b8e63fb8002672aaee8554044422984e`, 46 lines | none |
| #22039 — Dream synthesis report 49640230 | `f7cac386b2dc5b11fe0af346ab01259555f51735` | `docs/reports/dream/49640230-c32a-4547-957c-f23d36e39109.md` | `e1588c18cb905e2cd5d3ea9e35ae93a904149e76`, 48 lines | none |

### Batch 2 bound gcode excerpts

Each SHA-256 hashes the raw UTF-8 excerpt, including its final newline.

| Evidence | Path | Lines | Excerpt SHA-256 |
| --- | --- | --- | --- |
| B1 | `src/gobby/storage/tasks/_creation.py` | 136–225 | `03152df82839876aebfbced6e8bfa198fc80d1ecf228c755f4738d2aa4651c78` |
| B2 | `tests/tasks/test_expansion_service_compile_minimal.py` | 232–280 | `e91878e4d1796ffc148c63f7f6783c7c693f2af98e96d74a7bb8baea33ff756b` |
| B3 | `tests/workflows/test_database_deadline_retry.py` | 220–250 | `373ec7b4efb97b11b945d8f5f2e0ec3b7125940a4f7cde85fdf96b8738406348` |
| B4 | `tests/storage/stage_review_helpers.py` | 40–65 | `b3a3e124483830edd3a59b6b423a0032f783dd8c2d7d2f30b2dc17699fe13c73` |
| B5 | `tests/review_learning/test_round_diff.py` | 410–445 | `9dc29fdb5b8f0ff8432ac62c1ab24498d89175713649ed52d3bef17b0a2e77af` |
| B6 | `tests/e2e/conftest.py` | 64–121 | `90e5d8241d9a78d4419eb6fdc6c8badaa1bbe4b284b434863e69c2eed8457993` |
| B7 | `tests/e2e/conftest.py` | 646–735 | `4127ee4bc78bc48440475b0a6fa0876e7aa5900ccadf3c9ac792e5374631b4d8` |
| B8 | `src/gobby/sessions/transcript_processing.py` | 120–157 | `fcb5260b2408442765131d0e227e9affb09d9b9044eb34f4686f917c8ac70a45` |
| B9 | `src/gobby/storage/sessions/_transcript.py` | 25–60 | `1462babf68c716c58b242cddfc696ef8684cca0f1fecf2e504537bf917312c00` |
| B10 | `src/gobby/sessions/transcript_processing.py` | 158–235 | `9e991e2ae332c64ce004490037fae40d89f0cc6001feb54fe13de63cede858ed` |
| B11 | `docs/contracts/session-boundary.md` | 70–140 | `fc531958f64a087a69f0b4bac1f395a29c8e3b551b6f7f524b4636159eca0b31` |
| B12 | `src/gobby/sessions/handoff_summary.py` | 46–105 | `c320c4136c65bd38cbe214399abf15a1f2f7ac09bc4dbb2a4d0bf66e8af4e4ff` |
| B13 | `docs/contracts/session-boundary.md` | 1–75 | `8016b7b0aee098b5eb2bfe0c0491f24b97d960db8f032f7b79319749e42cd8b5` |
| B14 | `src/gobby/sessions/summary_generation.py` | 104–170 | `4e2bba4db751c3c83d3c0dccc397067ad6c926ba47775996351d7e4db6f3471b` |
| B15 | `src/gobby/install/shared/prompts/handoff/session_end.md` | 1–104 | `8b747ddb1b4ea714584d2b1d2844aeee269bbb310daf60588379f0ca9368b72b` |

Validation covers the exact 89-task derivation and batch order, revision prefixes,
15 bound excerpt hashes, three replacement ancestors, seven historical report
postimages and absent paths/refs, classification counts, Markdown fences, and
freshness footer with a focused `uv run python` audit. `git diff --check` checks
the report edit. No full suite or heavy command ran.

### Batch 2 archival-prompt recovery referrals

The Orchestrator disposed the five historical report losses as **no recovery**:
those report directories are absent on integration, and synthesis actions live in
the database. That disposition does not cover these three live prompt losses.
The generated-recovery halves are superseded; the archival-prompt halves are
**missing**. B14 loads this prompt, and B11:78–81 expressly leaves archival-summary
behavior unchanged. B15 proves the full current prompt lacks all 14 added lines.
No fresh model or live registry execution is claimed.

| Task and title | Unlanded full SHA | Live paths with missing content | Holding branch |
| --- | --- | --- | --- |
| #21875 — Optional review candidates | `0acc7cef777035f57136a68d575653a468370b54` | `src/gobby/install/shared/prompts/handoff/session_end.md`; `tests/prompts/fixtures/handoff_session_end_golden.md` | none |
| #21881 — Operation qualifiers | `5fa4992e9e81d735f548126bb3b9848a65feb2f8` | `src/gobby/install/shared/prompts/handoff/session_end.md`; `tests/prompts/fixtures/handoff_session_end_golden.md` | none |
| #21850 — Reference fidelity and inherited attribution | `d10d2f7f6fe842675fe937cfed847be3839d895d` | `src/gobby/install/shared/prompts/handoff/session_end.md`; `tests/prompts/fixtures/handoff_session_end_golden.md` | none |

Exact absent lines for #21875 **Optional review candidates**, from `0acc7cef777035f57136a68d575653a468370b54`:

```text
Search hits, review candidates and optional recommendations do not establish unfinished
work. A successful review does not require changing every candidate. Aggregate queue
flags such as pending_reviews_complete=false do not identify required candidate actions
or their cause. Preserve ambiguous flags as observations when relevant; do not invent a
corrective step. Retain explicitly pending acceptance and other adopted unfinished work.
Handoffs from this same session describe earlier epochs, not different sessions.
```

Exact absent lines for #21881 **Operation qualifiers**, from `5fa4992e9e81d735f548126bb3b9848a65feb2f8`:

```text
Preserve the operation, actor, conditions and exceptions that limit a claim.
A restriction in one handler or response path is not a rule for the whole subsystem.
Keep supported alternatives together and do not broaden the scope of "only".
Reconcile earlier shorthand with later evidence that supplies a narrower scope.
Preserve each reference's kind (for example session, handoff, task or commit).
Source inspection establishes what the inspected code says, not what a running service uses.
```

Exact absent lines for #21850 **Reference fidelity and inherited attribution**, from `d10d2f7f6fe842675fe937cfed847be3839d895d`:

```text
Copy identifiers and commit hashes exactly. Attribute work quoted from earlier sessions
to those earlier sessions, and distinguish it from actions performed in this session.
```

### Batch 2 touched-path audit

Existence is checked for every changed path of every unproven revision, including
paths in deliberately retired mechanisms. A surviving path triggers inspection
of its actual delta; contract retirement alone cannot dispose a live-path edit.
The prompt and golden fixture survive in all three corrected rows.

| Task and phrase | Revision | Paths present at pin | Paths absent at pin |
| --- | --- | --- | --- |
| #22389 — found-work owner handoff must count every successful send_message, not only parent-targeted sends | `486fb22d37` | 1 | 0 |
| #22326 — Repair B-group test drift: deadline guidance tier, review-evidence code-index fixtures, compile-minimal deferral shape | `624e3623dd` | 4 | 0 |
| #22319 — Isolated test daemon health wait is too tight and reports nothing | `ae65650a5e` | 9 | 0 |
| #22071 — Synthesize feedback run 15052df6-3b06-450d-9f4e-aa75b4e03917 | `c06f8df8e4` | 0 | 1 |
| #22069 — Synthesize dream run c19e1126-cbf4-436e-b956-ada5631bd49c | `8370c53dea` | 0 | 1 |
| #22040 — Synthesize feedback run e3b255c6-044e-48fc-a49b-297d821d016a | `e9cdbca0c3` | 0 | 1 |
| #22051 — Synthesize feedback run ef48b0b1-dd58-403a-ac84-52a28675d289 | `26aa30ddb8` | 0 | 1 |
| #22051 — Synthesize feedback run ef48b0b1-dd58-403a-ac84-52a28675d289 | `dbe7162df6` | 0 | 1 |
| #22039 — Synthesize dream run 49640230-c32a-4547-957c-f23d36e39109 | `dea81c11c8` | 0 | 1 |
| #22039 — Synthesize dream run 49640230-c32a-4547-957c-f23d36e39109 | `f7cac386b2` | 0 | 1 |
| #21820 — Schedule archive-backed summary retries without starving later sessions | `eb53e743b4` | 5 | 1 |
| #21794 — Validate session handoff rollout and document review handoff | `12abc42f16` | 3 | 1 |
| #21794 — Validate session handoff rollout and document review handoff | `20308164c0` | 0 | 1 |
| #21794 — Validate session handoff rollout and document review handoff | `fa12a9de43` | 0 | 1 |
| #21875 — Keep optional review candidates from inventing unfinished session work | `0acc7cef77` | 2 | 5 |
| #21881 — Preserve operation-specific qualifiers in recovered session facts | `5fa4992e9e` | 2 | 5 |
| #21850 — Preserve recovery reference fidelity and inherited work attribution | `9b925f9c27` | 0 | 5 |
| #21850 — Preserve recovery reference fidelity and inherited work attribution | `d10d2f7f6f` | 2 | 5 |
| #21860 — Give bounded recovery output a concise target below its hard limit | `410c950601` | 0 | 2 |
| #21867 — Exclude session objects and displayed examples from handoff task evidence | `103a07c698` | 0 | 4 |

Correction validation additionally reproduces all touched-path counts, verifies
the 14 exact added lines are absent from both live paths, and checks the three
new referral SHAs and lack of holding refs.

## Semantic recovery review: batch 3 (45 of 89 cumulative)

This addendum reviews positions 31–45 in the unchanged newest-first snapshot.
Evidence remains bound to commit `4ccb3e966ed18fe13cd9f268d55143128d034048`,
tree `428e1e0a2babda4fe2edf5c6bf56ae4950dbaf43`, gcode checkout project
`19b73420-3946-5c6a-8a06-4c7522f77543`. The report branch was cleanly
fast-forwarded to batch 2 landing `6b2c42eb3e9a7f0e220d50eb77b088978d157257`
on the Lane Manager’s direction; the evidence pin remains fixed.

### Batch 3 classifications

| Task and title | Unproven revision | Classification | Pinned evidence and disposition |
| --- | --- | --- | --- |
| #21886 — Preserve receipt wording and reference kinds in recovered work | `19ad378d4e` | missing | B15 lacks six additive receipt/counter/reference-kind instructions in the surviving archival prompt and golden fixture. Generated recovery halves are retired; these live prompt edits have no named withdrawal. |
| #21832 — Reuse the existing summary generator and parsed transcript recovery | `3e1c1e9584` | missing | B14 retains configured single-call Markdown generation and validation. Ordered Source Records recovery and reduction are superseded by B11–B12. Its archival system/prompt guidance distinguishing expiration from completion, reconciling later accomplishments, preventing invented follow-up and preserving attribution is absent from B14–B15. This partial loss needs adaptation to the current transcript input, not replay of the old generator. |
| #21847 — Keep explicit session references out of handoff task evidence | `39d5fc517b` | intentionally superseded | Both changed paths are retired generated-evidence/selection modules. B11 explicitly excludes the event-ledger/source-position mechanism; B12 supplies authored-content evidence. No surviving independent path is changed. |
| #21842 — Update prompt sync tests for retired session prompts | `e416a26215` | missing | C2 retains the removal of retired prompt assertions and generic orphan deletion coverage. Five surviving test methods still have untyped db parameters; the revision’s HubDatabase annotations and corresponding baseline reduction are missing. Its transcript_recovery expectations are obsolete and should not be restored. |
| #21826 — Accept fenced structured model output during handoff recovery | `2a76e1d76e` | intentionally superseded | Only the retired generated handoff_synthesis module and its test change. Fenced structured JSON normalization is superseded by the authored Markdown delivery contract in B11–B12. |
| #21793 — Select session UI summaries by lifecycle status | `4056b4b32f` | missing | C1 still prefers any summary_markdown before handoff_markdown without considering lifecycle status. The revision’s expired-only archival selector, active/paused continuation preference and route/UI regressions are missing across three surviving paths. |
| #21822 — Wrap overlong session lifecycle test comments and SQL | `eb53e743b4` | missing | This shares the revision with delivered #21820 Fair archive retries, but its 15 E501 test wraps are independently absent. The focused Ruff command below reproduces exactly those 15 failures. Recovery is already filed as #24113 Test-line wraps. |
| #21792 — Expose attributable project-scoped handoff retrieval | `46843a7290` | missing | C3 lacks the query-tool registration; both query modules and their test are absent. Bound searches find no search_handoffs or get_handoff_record. Project-defaulted read-only retrieval, text search, filtering and stable pagination remain missing. Retired provenance/epoch fields require adaptation to authored records; no explicit withdrawal covers the query affordance itself. |
| #21791 — Retire summary revisions with a recoverable mechanical migration | `c37094fba7` | delivered | Named replacement #21889 Remove session summary revision history explicitly preserves generation, handoffs and current summary fields. C4 removes the pointer/table with transactional RESTRICT; C5 stores current fields atomically. The draft’s destructive backup/metadata-transfer variant is intentionally superseded by this ordinary migration. Its provenance-dependent tests and version/checksum artifacts are obsolete. |
| #21781 — Recover structured session work and synthesize expired summaries | `88e6e32668` | missing | The generated structured recovery, gap fingerprints, forced regeneration, epoch-only CLI and replacement dispatcher flow are superseded by B11–B12 and #21889 Revision retirement. All 29 surviving paths were checked. The rewritten live archival prompt/fixture also contains independent expiration/completion, later-accomplishment reconciliation and attribution/no-invented-follow-up guidance absent from B15. Coalesce that partial prompt-policy loss with #21832 Generator reuse; preserve current architecture. |
| #21770 — Add handoff provenance and transcript source-range selection | `7e8bffd406` | intentionally superseded | B11 explicitly retires full-source epoch recovery, provenance ledger and source-position verification. Surviving transcript index/sidecar/reader/parser changes exclusively support that mechanism; current handoff records retain authored narrative and delivery receipts without those columns. Its empty-next_steps change conflicts with the current nonblank continuation contract. Schema pins and tests belong to that retired design. |
| #21414 — Fix project-scoped secret upsert conflict | `39211c1fe7` | delivered | C6 retains project_id in INSERT/RETURNING and ON CONFLICT(name,project_id); current scope resolution additionally supports explicit project writes. No database execution or live activation is claimed. |
| #21251 — Resolve schema 412 number collision between checkout identity and MCP templates | `0ebdca49db` | delivered | C7–C12 embed checkout identity, MCP templates, project-scoped secrets and feedback review fields in baseline@420. Named landed replacement #21511 Canonical baseline, 95e809ee251d64e742b68032065c241cce34b179, and #22368 Baseline lineage repair, b705b0254841bbcadd3bb0b55e2c8dc5f5ec08ba, replace the old 412/413/414 files and identity/golden pins. The old collision is eliminated rather than replayed. |
| #20303 — Add project_checkouts via 412_project_checkouts.sql | `74feea869d` | delivered | C7–C10 retain machine/project/root checkout columns, RLS read scope and lock-only UPDATE with WITH CHECK(false). C13–C14 retain the privilege manifest and its regression in the current column_operations shape. The old migration and numeric identity pins are replaced by baseline@420; the permission comparison preserves lock-only semantics, not row mutation. |
| #19133 — Reduce routine daemon INFO noise | `2771fbfced` | delivered | Same-task landed replacement cc82f7a5a395a0fa033c5bdfa8cf1376b4ced824 is an ancestor of the pin. Surviving logging demotions, mode transitions and caplog regressions remain. The aggregate sync behavior moved to C15. The draft’s unrelated dream semaphore change is replaced by landed #19253 Batched related-evidence retrieval, 06112ec3cc40a44b66cb6dde1ceb63590525fc73: C16 starts keyword/vector channels independently with bounded deadlines. Retired codewiki/summary-action and close-log paths need no replay. |

Batch totals: **5 delivered, 3 intentionally superseded, 7 missing**.
Cumulative totals: **16 delivered, 9 intentionally superseded, 20 missing**;
**44 tasks remain**. These are source classifications, not fresh test passes,
installed registry assertions or live activation receipts. B11’s explicit
preservation of archival-summary behavior does not withdraw independent prompt
policy. The mixed missing rows distinguish those losses from retired mechanisms.

### Batch 3 recovery referrals

All seven revisions remain readable Git objects with **no holding branch or other
ref**. Recovery must target the named missing behavior using the current design.
The Inbox Manager receives Orchestrator-bound findings; the Lane Manager receives
copies. Prompt losses should coalesce with #24108 Archival prompt fidelity.

Referral receipts: Inbox Manager `5800892a-e00f-469b-a5f6-0fb90ea42f22`;
Lane Manager copy `6c9503a2-6b81-4622-af24-dbb75f134038`. Both durable messages
carry all seven full SHAs, titles, missing behaviors, paths and absent holding refs.

| Task and title | Full unlanded SHA | Missing behavior and paths | Holding branch |
| --- | --- | --- | --- |
| #21886 — Preserve receipt wording and reference kinds in recovered work | `19ad378d4e65347ec6406b04d83597c57ed2e342` | Six receipt-fidelity lines in src/gobby/install/shared/prompts/handoff/session_end.md and tests/prompts/fixtures/handoff_session_end_golden.md. | none |
| #21832 — Reuse the existing summary generator and parsed transcript recovery | `3e1c1e95841fea08e029533bea2893f6c1a9221a` | Archival summary system/prompt safeguards in src/gobby/sessions/summary_generation.py, src/gobby/config/sessions.py and the session_end prompt/fixture. Adapt input terminology; preserve single-call Markdown generation. | none |
| #21842 — Update prompt sync tests for retired session prompts | `e416a262151a9edef6609d1ae4578f32acaf8c2b` | Five HubDatabase db-parameter annotations in tests/prompts/test_prompt_sync.py; reconcile .gobby/test-types-baseline.json after the scoped type audit. Generic orphan-deletion behavior already exists. | none |
| #21793 — Select session UI summaries by lifecycle status | `4056b4b32f3c54c37b08ea751089097bfd18dc95` | Lifecycle-aware selector and regressions in web/src/components/activity/SessionsTab.tsx, web/src/components/activity/__tests__/SessionsTab.test.tsx and tests/servers/test_servers_sessions_routes.py. | none |
| #21822 — Wrap overlong session lifecycle test comments and SQL | `eb53e743b4e268e42e2fd46b85aabd413f4acb6d` | 15 comment/SQL wraps in tests/storage/sessions/test_pruning.py and tests/sessions/test_sessions_lifecycle.py; already filed as #24113 Test-line wraps. | none |
| #21792 — Expose attributable project-scoped handoff retrieval | `46843a72909cf5cd1d2b13e3d88732631556f755` | Read-only handoff retrieval/search/filtering/pagination in src/gobby/mcp_proxy/tools/sessions/_factory.py, _handoff_queries.py, src/gobby/sessions/handoff_search.py and tests/sessions/test_handoff_search.py. Adapt retired provenance filters rather than replay them. | none |
| #21781 — Recover structured session work and synthesize expired summaries | `88e6e32668d01bf284bf7ceb85374245478fe397` | Independent archival prompt safeguards in src/gobby/install/shared/prompts/handoff/session_end.md and tests/prompts/fixtures/handoff_session_end_golden.md. Coalesce with #21832 Generator reuse; do not replace current generation, hooks or CLI. | none |

The six exact additive lines missing from both live paths in #21886 Receipt wording:

```text
Hexadecimal appearance does not establish a Git commit; retain the source's label
for a review, run or other identifier, and leave an unspecified kind unspecified.
Keep receipt wording and counters tied to the observation that establishes them.
Successful sending is not confirmed delivery; a null delivery timestamp confirms neither outcome.
When later receipts update a counter, attribute the new value to the later receipt.
Omit incidental activity counters unless they explain task progress or a decision.
```

The independent prompt safeguards from #21781 Structured synthesis and #21832
Generator reuse distinguish expiration from completion, reconcile earlier next
steps with later accomplishments, retain decisions and unsuccessful approaches,
prohibit invented commits/validation/review/closure follow-up, distinguish reported
observations from verified outcomes, and attribute inherited context. Their old
source_records/previous_context interfaces and structured JSON output are retired;
the referral requests those safeguards in the current archival prompt contract.
B14 uses only the generic comprehensive/actionable system instruction. B15 still
asks for remaining commit/link/validation/closure actions when implementation is
complete without those reconciliation safeguards.

The focused command below failed with exactly **15 E501 diagnostics**:

```bash
uv run ruff check --select E501 tests/storage/sessions/test_pruning.py tests/sessions/test_sessions_lifecycle.py
```

Pruning lines: 57, 337, 367, 396, 429, 457, 492, 518, 552, 588, 793, 941.
Lifecycle lines: 802, 961, 2060. The historical removed overlong lines match the
pin. This finding is confined to audited source and referred through #24113
Test-line wraps; no source fixes are authorized in this report-only task.

### Batch 3 bound gcode excerpts

Each SHA-256 hashes raw UTF-8, including the final newline. B11–B15 are reused
from batch 2. Bound literal searches over src/gobby for search_handoffs and
get_handoff_record returned no items; C3 separately proves absent registration.

| Evidence | Path | Lines | Excerpt SHA-256 |
| --- | --- | --- | --- |
| C1 | `web/src/components/activity/SessionsTab.tsx` | 49–67 | `2cd92106ded4d7d6ae307bf6eff959c817d6a8c4aa90f890fc1bf67a7a0458e0` |
| C2 | `tests/prompts/test_prompt_sync.py` | 19–110 | `74e34793b48a7438a0c307fa583352d85cf70ec8019337ab2877de1b929b5317` |
| C3 | `src/gobby/mcp_proxy/tools/sessions/_factory.py` | 1–90 | `aeb9ae1fad89fa01f0fb305a6b96b0f87bf193b592ed9b720581657ccdc84f13` |
| C4 | `crates/gcore/assets/schema/migrations/427_remove_session_summary_revisions.sql` | 1–24 | `a766908d0dbfac143765e98530a8dd16b25b4060a2b9039d31c309269fb9b140` |
| C5 | `src/gobby/storage/sessions/_summary_update.py` | 16–68 | `38d7dad5590187917f86b6b70d9c0e1d2c4ef9d78f694625711a4bd78770a6d2` |
| C6 | `src/gobby/storage/secrets.py` | 562–600 | `1ab682813da2318e0fde04f082e738eebfe7114ca95bfb4d47db13446f8b4cd8` |
| C7 | `crates/gcore/assets/schema/baseline.sql` | 2995–3003 | `29f2755733d028dc1a4d722ae69f4e739f2b90a94f7d917ab3167481bf6900c3` |
| C8 | `crates/gcore/assets/schema/baseline.sql` | 4479–4483 | `afd5ae23b5cfb12a95b1d86cfc18d1c5e085c9486c11be0df56c86baef0b978b` |
| C9 | `crates/gcore/assets/schema/baseline.sql` | 5926–5960 | `8d8051c23d6cb87a98b98524652b367d2b5eb6ebf4666b216f04dd3f3376a931` |
| C10 | `crates/gcore/assets/schema/baseline.sql` | 6347–6358 | `c2c1290ec71bee9534159c359b02acfc7653a4df5e940e34a571a3657d95f1ab` |
| C11 | `crates/gcore/assets/schema/baseline.sql` | 2677–2690 | `86224a61d5b2da46539886022a7a57f2aba9cfa00076475cc92fedd35f813fce` |
| C12 | `crates/gcore/assets/schema/baseline.sql` | 3338–3380 | `3cc0fd68be318008822b084279cd6a1b1b8581eadeb0bcb55136906af69b925d` |
| C13 | `crates/gcode/security/managed_postgres_privileges.json` | 1–35 | `7d1d2cf99b56af27d75f28b40c1948c9bb1aba106fb87e439eead21901ef5709` |
| C14 | `tests/code_index/test_gcode_privilege_manifest.py` | 72–92 | `b780c98a1f53cba21cbf356f77768b8992b3e3ab4d0bdf4e7730115758f82fb8` |
| C15 | `src/gobby/sync_registry.py` | 90–129 | `d3e418680ffa1e44f2ac13f644a24dc22d9f792097b54b1e8c5019ee7913006d` |
| C16 | `src/gobby/memory/dream/related.py` | 328–400 | `69320b4ed44cfac6c44807a3bc36ff557bdee119af6d3a7113eb4e4a77f87f39` |

### Batch 3 touched-path audit

Every revision’s complete changed-path list was checked against the pin. Surviving
production/test deltas were compared individually; retired paths were mapped to
B11–B12 or the named replacement. Numeric schema identities, catalog manifests
and grant golden vectors were inspected as generated artifacts of their schema
versions, not as independent runtime behavior. Prompt/fixture policy losses are
retained as partial missing findings even when their generation interfaces retire.

| Task and title | Revision | Paths present at pin | Paths absent at pin |
| --- | --- | --- | --- |
| #21886 — Preserve receipt wording and reference kinds in recovered work | `19ad378d4e` | 2 | 5 |
| #21832 — Reuse the existing summary generator and parsed transcript recovery | `3e1c1e9584` | 5 | 10 |
| #21847 — Keep explicit session references out of handoff task evidence | `39d5fc517b` | 0 | 2 |
| #21842 — Update prompt sync tests for retired session prompts | `e416a26215` | 2 | 0 |
| #21826 — Accept fenced structured model output during handoff recovery | `2a76e1d76e` | 0 | 2 |
| #21793 — Select session UI summaries by lifecycle status | `4056b4b32f` | 3 | 0 |
| #21822 — Wrap overlong session lifecycle test comments and SQL | `eb53e743b4` | 5 | 1 |
| #21792 — Expose attributable project-scoped handoff retrieval | `46843a7290` | 1 | 3 |
| #21791 — Retire summary revisions with a recoverable mechanical migration | `c37094fba7` | 13 | 3 |
| #21781 — Recover structured session work and synthesize expired summaries | `88e6e32668` | 29 | 11 |
| #21770 — Add handoff provenance and transcript source-range selection | `7e8bffd406` | 13 | 7 |
| #21414 — Fix project-scoped secret upsert conflict | `39211c1fe7` | 1 | 0 |
| #21251 — Resolve schema 412 number collision between checkout identity and MCP templates | `0ebdca49db` | 14 | 5 |
| #20303 — Add project_checkouts via 412_project_checkouts.sql | `74feea869d` | 16 | 1 |
| #19133 — Reduce routine daemon INFO noise | `2771fbfced` | 20 | 6 |

### Batch 3 reproducible report validation

Run this focused source/report check from any checkout containing this report
and its historical Git objects. It derives the fixed inventory from the committed
original table and per-commit proof sections; no ignored cache is required. It runs
no runtime test suite.

```bash
uv run python - <<'PY'
# batch-3-audit
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

PIN = "4ccb3e966ed18fe13cd9f268d55143128d034048"
REPORT = Path(".gobby/plans/research/closed-unlanded-audit-2026-10.md").read_text()
EXCLUDE = {23201, 23252, 23304, 23345, 23519, 23626, 23269}

def git(*args):
    return subprocess.check_output(["/usr/bin/git", *args])

original = REPORT.split("## Every closed task with an unlanded commit\n", 1)[1].split("\n## Per-commit", 1)[0]
proof = REPORT.split("## Per-commit supersession and recovery evidence", 1)[1].split("## Every closed task", 1)[0]
needs_inspection = {
    sha for sha, body in re.findall(r"### `([0-9a-f]{40})`\n(.*?)(?=\n### `|\Z)", proof, re.S)
    if "\nMissing linked revision, semantic supersession not proven." in body
}
inventory = []
for line in original.splitlines():
    if not line.startswith("| #") or not line.endswith("Missing linked revision, semantic supersession not proven |"):
        continue
    columns = line.strip("| ").split(" | ")
    seq_num = int(re.match(r"#(\d+)", columns[0])[1])
    oids = [oid for oid in re.findall(r"`([0-9a-f]{40})`", columns[2]) if oid in needs_inspection]
    if oids and seq_num not in EXCLUDE:
        inventory.append(({"seq_num": seq_num, "closed_at": columns[1]}, oids))
inventory.sort(key=lambda row: (row[0]["closed_at"] or "", row[0]["seq_num"]), reverse=True)
assert len(inventory) == 89
batch = REPORT.split("## Semantic recovery review: batch 3", 1)[1]
batch = re.split(r"\n## Semantic recovery review:", batch, maxsplit=1)[0]
classification = batch.split("### Batch 3 classifications", 1)[1].split("Batch totals:", 1)[0]
rows = re.findall(r"\| #(\d+) — .*? \| `([0-9a-f]+)` \| (missing|delivered|intentionally superseded) \|", classification)
assert [(int(n), sha) for n, sha, _ in rows] == [(t["seq_num"], oids[0][:10]) for t, oids in inventory[30:45]]
assert Counter(v for _, _, v in rows) == {"delivered": 5, "intentionally superseded": 3, "missing": 7}
assert "44 tasks remain" in batch
hashes = re.findall(r"\| C\d+ \| `([^`]+)` \| (\d+)–(\d+) \| `([0-9a-f]{64})`", batch)
assert len(hashes) == 16
for path, start, end, expected in hashes:
    excerpt = b"".join(git("show", f"{PIN}:{path}").splitlines(keepends=True)[int(start)-1:int(end)])
    assert hashlib.sha256(excerpt).hexdigest() == expected, path
path_rows = re.findall(r"\| #(\d+) — .*? \| `([0-9a-f]+)` \| (\d+) \| (\d+) \|", batch)
assert len(path_rows) == 15
for (n, prefix, present, absent), (task, oids) in zip(path_rows, inventory[30:45], strict=True):
    assert int(n) == task["seq_num"] and prefix == oids[0][:10]
    paths = git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", oids[0]).decode().splitlines()
    found = sum(subprocess.run(["/usr/bin/git", "cat-file", "-e", f"{PIN}:{path}"], capture_output=True).returncode == 0 for path in paths)
    assert (found, len(paths)-found) == (int(present), int(absent))
    assert not git("for-each-ref", f"--contains={oids[0]}", "refs/heads", "refs/remotes")
for task, oids in inventory[30:45]:
    if any(n == str(task["seq_num"]) and v == "missing" for n, _, v in rows):
        assert oids[0] in batch
for ancestor in ("cc82f7a5a395a0fa033c5bdfa8cf1376b4ced824", "06112ec3cc40a44b66cb6dde1ceb63590525fc73", "95e809ee251d64e742b68032065c241cce34b179", "b705b0254841bbcadd3bb0b55e2c8dc5f5ec08ba"):
    subprocess.run(["/usr/bin/git", "merge-base", "--is-ancestor", ancestor, PIN], check=True)
lines = batch.split("```text\n", 1)[1].split("\n```", 1)[0].splitlines()
assert len(lines) == 6
for path in ("src/gobby/install/shared/prompts/handoff/session_end.md", "tests/prompts/fixtures/handoff_session_end_golden.md"):
    current = git("show", f"{PIN}:{path}").decode()
    historical = git("show", f"19ad378d4e65347ec6406b04d83597c57ed2e342:{path}").decode()
    assert all(line in historical and line not in current for line in lines)
assert sum(line.startswith("```") for line in REPORT.splitlines()) % 2 == 0
assert REPORT.rstrip().endswith("_Last verified: 2026-10-12_")
print("PASS: 89 inventory; batch order/counts; 16 hashes; 15 path/ref checks; 7 referrals; 4 ancestors; 6 live prompt losses; Markdown/footer")
PY
```

Also check whitespace with:

```bash
git diff --check
```

Validation passed: exact 89-task inventory and batch order/counts, 16 bound
excerpt hashes, 15 touched-path/ref inventories, seven missing full-SHA referrals,
four replacement ancestors, six exact live prompt losses, and Markdown/footer.
The focused AST check also confirms the five missing db annotations. Whitespace
check passed. No full suite or heavy command ran.

### Batch 3 review follow-up

LOW-1 is corrected by the cache-independent validator above. The original committed
208-task table and 121 unproven per-commit records reconstruct the same 89-task
inventory, newest first; historical Git derives each changed-path inventory.
The ignored cache remains unmodified and uncommitted.

LOW-2 is corrected in batch 2's #21794 **Handoff rollout and replay qualification**
row and cumulative totals. Its historical query documentation is missing alongside
#21792 **Read-only handoff queries**; #24121 **Handoff retrieval recovery** owns both.
The old replay-only artifacts and generated-recovery guidance remain retired.
This correction changes the 45 classified tasks to **16 delivered, 9 intentionally
superseded, 20 missing**; **44 tasks remain**. Batch 4 research is in progress.

## Semantic recovery review: batch 4 (60 of 89 cumulative)

This addendum reviews positions 46–60 in the unchanged newest-first snapshot.
Evidence remains bound to commit `4ccb3e966ed18fe13cd9f268d55143128d034048`,
tree `428e1e0a2babda4fe2edf5c6bf56ae4950dbaf43`, gcode checkout project
`19b73420-3946-5c6a-8a06-4c7522f77543`. Before edits, the clean report branch
was fast-forwarded to the batch 3 follow-up landing
`af678a70afd222f8e7220cc58fe0f95357d39889` on the Lane Manager’s direction.
That changes neither the evidence pin nor the audit inventory.

### Batch 4 classifications

| Task and title | Unproven revision | Classification | Pinned evidence and disposition |
| --- | --- | --- | --- |
| #19142 — Arbitrate synthetic tmux input safely across providers | `85668288c0` | intentionally superseded | The authoritative task explicitly says BACKED OUT, names 7e2a555681b20299de27ed18d15cf098af7b32c2 **Compaction identity without arbiter**, and directs lost-work audits not to recover this revision. Its retired migration 346 and the entire arbiter rollout were withdrawn. The task separately identifies #19752 **Live-wake cancellation** as the residual fix. This disposition rests on explicit withdrawal, not the absence of the 13 retired paths. |
| #19007 — Fix CodeRabbit packet 09: Servers, sessions, WebSocket, and runtime boundaries | `e95f9be14b` | delivered | Named replacement #19173 **Server/session/runtime reconciliation**, c3c13e0ed4af73e6dc103322c559f16ffa5e5bd5, is a pin ancestor. Surviving tmux command construction, context/offload, deployment credentials, ASGI cleanup, Codex dedup/registration rollback, telemetry ownership and test/fixture changes remain or are adapted. D1 retains probe-before-atomic-config-write semantics; D3–D4 retain corrupt-log handling and bounded counts. D2 uses the explicitly redesigned #23117 **Bounded transcript search** journal/source-window contract; it does not claim the old full-prefix digest remains in use. |
| #19006 — Fix CodeRabbit packet 08: MCP proxy and Hub services | `6d121d19bf` | delivered | Named replacement #19172 **MCP/Hub reconciliation**, 9c34494c6ce5d1c6f8688fea7c042804907b3136, carries or adapts the independent proxy/Hub hardening. D5 proves the current full-snapshot tool returns structured errors for corrupt stored snapshots and retains PostgreSQL/I/O handling for bind operations. Old automatic-recall paths and their tests were explicitly removed by #21009 **Retire automatic memory recall injection**, 8358d7bba6973b5ba58ea9340b0437bbb492be46; agents use on-demand search. Other surviving deltas were checked individually against the reconciliation ledger. |
| #19005 — Fix CodeRabbit packet 07: Workflows, skills, and orchestration | `f8dbf6d929` | delivered | Named replacement #19171 **Workflow/skills reconciliation**, 1242097f2fb0abd571b70c4339b462b9e4131390, carries the surviving workflow, variable, skill, runner and regression changes. D6 uses an evidence handle plus get_plan_review_snapshot instead of inline bytes, so inline nonce protection is obsolete. D7 retains offloaded memory suppliers. The unresolved-error reservation inside the old digest-based summary action was removed by #21140 **Structured session handoffs**, 5694253eb6232fc0cae9dc6a27521b176dea98d5; D8 caps the replacement transcript input. Retired transport/digest tests need no replay. |
| #19004 — Fix CodeRabbit packet 06: Tasks, plans, and review learning | `7daf01cf55` | missing | Partial loss: six independent improvements remain absent in live coverage, finding and lesson-selection interfaces (D12–D17). Named replacement #19170 **Task/plan/review reconciliation**, da44e2fa9e42fc149a7414c1b1492dda48af6269, carried them, but #19368 **Restore pre-experiment plan-adversary flow** removed them. That task explicitly preserves unrelated improvements. Atomic manifest application, approval locking and direct run IDs survive (D9–D11). Both attestation substitutions to canonical_json_sha256 were reverted by the baseline restoration; the helper and _source_digest use survive, and inline attestation encoding produces identical digest bytes. This additional maintainability cleanup belongs to the existing #24128 **Packet 06 review-learning hardening** recovery task alongside the six named losses below. Retired receipt/convergence mechanisms stay retired. |
| #18970 — Harden idle, prompt, stall, and pane-monitor behavior | `79486d6f34` | delivered | Prompt, stall and lifecycle deltas survive. The old pane monitor is replaced by attention_tracker and interactive_attention_monitor: D18–D20 retain classification payloads, offloaded storage, provider-specific detection and capture-timeout handling; D21 retains complete machine-run and live-session pagination. The retired pane-monitor tests are mapped to the replacement attention surface rather than treated as missing solely by path name. |
| #18984 — Integrate hardened managed sandbox startup into 0.5.0 | `58e05a93f7` | delivered | All nine touched paths survive. D22 records startup failure against the run manager, returns a failed SpawnResult and offloads enforcement storage. D23 retains portable Node preflight, provider TMPDIR restoration, signal cleanup and unsubscribe. The runner/helper decomposition preserves the tagged launch-or-failure contract; current sandbox regression modules remain. |
| #18971 — Harden sandbox, spawn, and SRT runtime contracts | `1e17d29420` | delivered | The same startup/cleanup contracts are retained in D22–D23 and all nine paths survive. The task’s bounded package-root follow-up 5396812e8902bc278cd21405d08f5bc88e3fe83d is a pin ancestor. Provider-optional sandbox policy, spawn run-manager wiring and contextual failure logging remain; no runtime sandbox execution is claimed. |
| #18267 — Fold web-core retry fork into review-fixes/16966-web-core lane | `16a1cca3d2` | delivered | Its hydration fix is delivered by #16695 **Selection hydration**, e87998c1399a3fe149184da14e0749e03c97c07c. D24 preserves touched guards, both callbacks and persistence gates; D25–D26 preserve deferred-response regression semantics after #19968 **Browser configuration migration**, b6294eb62ec86e0b44ac069596459bc6adff0f37. D27 retains the bundle warning threshold and robust React vendor splitting. All four changed paths survive. |
| #16695 — Stop UI-settings hydration clobbering selections made during load | `404411584c` | delivered | Same-task landed replacement e87998c1399a3fe149184da14e0749e03c97c07c is a pin ancestor. D24 prevents late persisted settings from replacing user-touched project/provider selections and persists early edits. The renamed test in D25–D26 still edits both selections before resolving stale configuration, asserts both final selections and checks both revision-carrying PATCH bodies. The specialized PUT endpoint is intentionally replaced by universal configuration. |
| #18250 — Resolve web-components-b JIT-merge conflict in cron storage validation tests | `30539f3ad5` | delivered | The merge has no ordinary changed-path delta. Its remerge conflict resolution keeps invalid-cron coverage and uses an expired one-shot schedule for the separate invalid-enabled-schedule test. D28 retains both rejection cases and empty-storage assertions. This is semantic test-resolution evidence, not an ancestry claim for the unlanded merge. |
| #16615 — Restore the focus indicator on the font-size slider | `dcd4644bfc` | delivered | Same-task replacement 6019aa7a68221fe828bc89af72fad02569ee62f4 is a pin ancestor. D29–D30 retain the focus-visible accent outline, offset and actual slider-focus regression; the old settings.css rule is replaced by utilities on AppearanceSection. D27 retains the revision’s robust React chunk matching. |
| #16582 — Add keyboard parity to clickable divs, rows and SVG across 15+ surfaces | `31ae7bf0ae` | delivered | Same-task replacement 169bffaddb3ccf94404774826c265f045576e2c5 is a pin ancestor. D32–D34 retain Enter/Space activation, nested-target guards and preventDefault; D37–D40 map menu/dialog/filter presentation changes to current shared primitives and the primary-mousedown overlay contract. Surviving activity/file-change/trace/integration/memory/skills regressions remain or adapt. FilesPage and legacy wiki paths are explicitly retired by #20103 **Prettier adoption** and #21816 **Legacy wiki UI retirement**; their deleted tests are not independent live losses. |
| #16071 — FilesPage failed save must preserve the editor and unsaved edits | `afe1e1a63a` | delivered | Same-task replacement d1ef17cbab0f4e9b7c1ff2a358f70ab77a3b6a1b is a pin ancestor. D35 keeps failure separate in saveError, clears saving and preserves content/edit state by file identity; D36 retains failed-save/retry coverage. D31 closes the current shared editor only on successful onSave. The dead FilesPage and its stylesheet/component tests were explicitly removed by #20103 **Prettier adoption**, 927af24ab739527f160a10b2cf3b4bcd0a7d672e. That retirement does not erase the surviving hook fix. |
| #17462 — Write Codex goal prompt for fable fixes orchestration | `4ee2c63b30` | intentionally superseded | The only changed path, .gobby/goals/fable-fixes.md, is an old provider-native goal orchestration prompt. Josh explicitly retired native goal loops for developer lanes; current seat policy requires Gobby task/handoff/Lane Manager continuity. Memory 9215aba2-b193-5b35-b1f1-24162643ec3d records the decision and coordination failure it prevents. No product source or independent regression is changed. The object remains on 0.4.44; do not reactivate its obsolete orchestration instructions. |

Batch totals: **12 delivered, 2 intentionally superseded, 1 missing**.
Cumulative totals: **28 delivered, 11 intentionally superseded, 21 missing**;
**29 tasks remain**. These classify source and explicit decisions; they do not
assert fresh runtime test passes, installed registry state or activation.

### Batch 4 recovery referral

Inbox Manager receipt `7f719a24-e8a6-4e13-8bb9-ee73adf3cdce` and Lane Manager
copy `db116bc8-3028-4307-8307-f5b3f7cd340e` carry the full missing-partial
referral to the Orchestrator. Both messages name the task, full SHA, current
archive ref, absence of a holding branch, missing behaviors and affected paths.
The Lane Manager confirms the Orchestrator filed #24128 **Recover packet 06
review-learning hardening**; it remains queued behind this audit.

| Task and title | Full unlanded SHA | Missing behavior and paths | Holding branch |
| --- | --- | --- | --- |
| #19004 — Fix CodeRabbit packet 06: Tasks, plans, and review learning | `7daf01cf5552fc65d2e07ffaca06016ac7db6fa5` | Coverage source-resolution errors and stable field error codes, named threshold constants, Markdown-structure guards, positional candidate handling, and evidence-scoped invalid-payload diagnostics. Source: src/gobby/plans/review_coverage.py, src/gobby/plans/review_findings.py, src/gobby/review_learning/round_diff.py. Adapt regressions in tests/plans/test_review_coverage.py, tests/storage/test_stage_review_findings.py and tests/review_learning/test_round_diff.py. | none; preserved by refs/audit/24076/7daf01cf5552fc65d2e07ffaca06016ac7db6fa5 |

The six losses correspond to packet findings 531, 532, 533, 539, 543 and 544:

- D12 resolves the root outside structured handling and maps every citation
  OSError to retryable source_drift. The revision distinguishes disappearance
  from non-retryable source_io_error.
- D13 derives error codes from owner prose instead of explicit caller codes:
  invalid_lane_results, invalid_candidate, invalid_dispositions and
  invalid_source_citation. New coverage fields need current-schema adaptation.
- D14 repeats 8/24/12/4 in routing and metadata. The numeric behavior is retained;
  the missing improvement shares named constants.
- D15–D16 validate strings but omit the revision’s rejection of fenced blocks
  and heading-like description/fix/prevention lines. Rendering inserts those
  strings into durable Markdown.
- D17 uses object identity to exclude already-selected candidates, collapsing
  repeated references to the same object. The revision tracks ranked positions.
  Its current no-fix-policy lesson class must remain.
- D17 silently returns None for invalid previous payloads; the revision records
  evidence-scoped DEBUG diagnostics without changing classifications.

The landed reconciliation ledger marks these improvements carried/adapted.
They disappear in `a2b779f60ff0020d8e5704bb37ecc345f1f6e754` under #19368
**Restore pre-experiment plan-adversary flow**. Its authoritative description
says to “preserve approved planning controls and unrelated improvements.”
The current interfaces still consume citations, render finding strings and
select lesson candidates. No separate withdrawal covers these guards.
By contrast, D9–D11 prove manifest re-fetch/verification/render/intent/write/
completion remain under mutation transactions and approval reuses the pre-held
mutex. Recovery must preserve that architecture and the named retirements.

### Batch 4 touched-path and replacement audit

Every unproven revision’s full path inventory was checked against the fixed pin;
surviving production and test deltas were compared individually. The large packet
reconciliations were checked against their finding-by-finding ledgers, now retained
under .gobby/plans/completed/coderabbit-fixes/. Exact-line retention was a navigation
screen, not sufficient proof of semantic delivery. Changed interfaces were followed
to their current helpers and regressions.

| Task and title | Revision | Paths present at pin | Paths absent at pin |
| --- | --- | --- | --- |
| #19142 — Tmux input arbitration | `85668288c0` | 42 | 13 |
| #19007 — Server/session/runtime packet | `e95f9be14b` | 49 | 4 |
| #19006 — MCP/Hub packet | `6d121d19bf` | 49 | 7 |
| #19005 — Workflow/skills packet | `f8dbf6d929` | 34 | 8 |
| #19004 — Task/plan/review packet | `7daf01cf55` | 35 | 2 |
| #18970 — Idle/prompt/stall hardening | `79486d6f34` | 9 | 3 |
| #18984 — Managed sandbox integration | `58e05a93f7` | 9 | 0 |
| #18971 — Sandbox/spawn/SRT contracts | `1e17d29420` | 9 | 0 |
| #18267 — Web-core retry integration | `16a1cca3d2` | 4 | 0 |
| #16695 — Selection hydration | `404411584c` | 4 | 0 |
| #18250 — Cron merge resolution | `30539f3ad5` | 0 | 0 |
| #16615 — Slider focus | `dcd4644bfc` | 2 | 1 |
| #16582 — Keyboard parity | `31ae7bf0ae` | 12 | 4 |
| #16071 — Failed-save preservation | `afe1e1a63a` | 3 | 3 |
| #17462 — Native-goal prompt | `4ee2c63b30` | 0 | 1 |

Path-retirement mappings:

- The arbiter task explicitly withdraws its whole 55-path rollout, including
  independent-looking compaction edits; named compaction replacement is an
  ancestor. Migration 346 belongs to the retired lineage.
- Packet 09’s historical archived test paths moved to current provider,
  deployment and telemetry tests. Sandbox records use incremental byte counts
  and replacement-decoded recent JSON (D3–D4). The transcript journal redesign
  deliberately replaces repeated full-prefix verification with bounded source
  windows and identity checks (D2); claim no equivalent whole-prefix guarantee.
- Packet 08’s automatic memory recall modules/tests are explicitly retired.
  Cleanup, health, communications-root security and Hub/proxy deltas survive
  in the current split helpers. Snapshot transport was later restored to a
  full UTF-8 snapshot tool under #19368 **Baseline review flow**; D5 is the
  current corrupt-snapshot proof, not the old paged-service name.
- Packet 07’s removed runner_maintenance.py is split into lifecycle.py (D7).
  Old summary_actions.py split into session modules, then #21140 **Structured
  handoffs** replaced the digest/reserved-error input. Current transcript bounds
  remain (D8). Variable transactions, structured summary logging and surviving
  skill/runner regressions retain their purposes.
- Packet 06’s receipt admission/validation helpers and corresponding deleted
  tests belong to the explicitly retired checklist-close design. Manifest,
  transition, criteria normalization and tagged-commit parser changes survive.
  The shared digest helper and _source_digest use survive, but both attestation
  call sites reverted to equivalent inline hashing. Recover that maintainability
  cleanup with the live coverage/finding/selection losses under #24128
  **Packet 06 review-learning hardening**; no digest-byte regression is claimed.
- The three absent idle-hardening paths are the pane monitor and its old tests;
  attention components retain provider-specific classification, offloading,
  capture handling and complete pagination (D18–D21).
- Browser persistence migrates from UI-settings PUT to revision-carrying config
  PATCH under #19968 **Browser configuration migration**. The renamed regression
  still verifies the original race and both writes (D25–D26), not just a new name.
- FilesPage was explicitly dead and retired by #20103 **Prettier adoption**;
  CodePage was removed by #16899 **Web-core nits**, a718a4fd9797f4c6a7365c992f5b3b78360555a2;
  legacy wiki components were retired by #21816 **Legacy wiki UI retirement**,
  200d7aa0503dff8d4afab28694b47217bcb5a839. Surviving useFiles and shared editor
  contracts preserve failed saves (D31/D35–D36). Keyboard changes survive in
  current tree items/helper and shared menu/dialog/filter primitives (D32–D40).
  Old settings.css focus styling is replaced by current slider utilities.

The first 14 revisions now have archive refs under refs/audit/24076/; they have
no local or remote holding branch. #17462 **Native-goal prompt** remains on
refs/heads/0.4.44. Archive refs preserve objects; they establish neither landing
nor semantic delivery. No refs were created or changed by this developer.

Found work in the report validator is fixed here: batch 3’s all-refs-empty
assertion now checks holding branches and permits archive refs created after
its earlier observation. Its section selection stops at the next batch.
Historical “no refs” observations remain dated evidence, not current claims.

### Batch 4 bound gcode excerpts

Each range was served by gcode evidence with the exact fixed binding and
complete=true. Each SHA-256 was checked against raw UTF-8, including its final
newline. Ranges ending past EOF hash only the actual returned lines.

| Evidence | Path | Lines | Excerpt SHA-256 |
| --- | --- | --- | --- |
| D1 | `src/gobby/servers/routes/configuration_generation_endpoints.py` | 43–124 | `29bcaca8f0f90b808a16cd821df157587bb729bc241bbe6dceced5153ac7c9bf` |
| D2 | `src/gobby/sessions/transcript_index_sidecar.py` | 535–576 | `7787097502706a4a653fbe172a9792b70fbe140c065196370d262043e51c4cb7` |
| D3 | `src/gobby/storage/agents/_sandbox_records.py` | 155–195 | `2f4f172b0258ba0ff4aba902ef2574b18d7270c02f913c1a47457117fcc17227` |
| D4 | `src/gobby/storage/agents/_sandbox_records.py` | 230–275 | `bc9b6898824b407f344a5ac482103f0174971716e7972ca7ef3868db1555633b` |
| D5 | `src/gobby/mcp_proxy/tools/plans/review_evidence.py` | 125–160 | `be09ddfdccbcb6cf406720e1d1c75d4d33e5d88ac86ff008d39f4257f1d28118` |
| D6 | `src/gobby/dispatch/prompts.py` | 170–200 | `8bd929186ba2d7410653a1bb63c978ddf7c249972eaa9081d9559fa211dcf1fe` |
| D7 | `src/gobby/runner_maintenance/lifecycle.py` | 20–55 | `16d82201b1ddb0a15fbe494e5511f25cd64f781b016da7aff0d99ba784012805` |
| D8 | `src/gobby/sessions/summary_transcripts.py` | 260–278 | `f011ca7c065392d1ea0257f522b9e8be9a68acd2bac6adef89884f6b6652d41a` |
| D9 | `src/gobby/plans/review_manifest_service.py` | 188–300 | `a4281c1f3949ef28f6498989ff14f8e79ef8e903f9b40dbafde11d6025fef726` |
| D10 | `src/gobby/plans/review_manifest_service.py` | 295–336 | `44bfe9b51d7b9b8a2845e70e29dac26dd6ecf3dc03f36a7404b7e8186ee6626e` |
| D11 | `src/gobby/storage/tasks/_review_transitions.py` | 200–255 | `0ac3cd52ffa1ff3d88a56be1011deac72c77b909dfea97607bca04c4211382e0` |
| D12 | `src/gobby/plans/review_coverage.py` | 759–817 | `78cece999f7d3ec507efc1863325a5f1f13659310267a9633f91a294707e5e9c` |
| D13 | `src/gobby/plans/review_coverage.py` | 840–850 | `98c7dca5c0ad7c2a3c28ffa2f78eae2056ccb612f74bc9b29332a31d16b5a388` |
| D14 | `src/gobby/plans/review_coverage.py` | 204–238 | `c147b58f36464e337d684bbc9ec9bad3d4ca97b7bb11fa4047f71704115a8982` |
| D15 | `src/gobby/plans/review_findings.py` | 100–170 | `28e35668a2609ff24915e3a8fa1a7a41f3ce0689b4ab06fe59fbca4f399820cd` |
| D16 | `src/gobby/plans/review_findings.py` | 170–225 | `6206d01450f9a53f4c85ccf972dc2d89e70a08bc464d992e5aa3efb71c5efae9` |
| D17 | `src/gobby/review_learning/round_diff.py` | 65–145 | `fbb07990702e34f3a3db3228df3dec890607d37fa4b164c5701f480b634bae97` |
| D18 | `src/gobby/agents/attention_tracker.py` | 80–130 | `09cbff07e0c3050b42cda37c41604bd376ef70fde311fba6ed43442f017f3ae0` |
| D19 | `src/gobby/agents/interactive_attention_monitor.py` | 188–240 | `7ae0dd9cb2c8b64197315482696448f9ea589a2fb0bd137192f5b931bcc69218` |
| D20 | `src/gobby/agents/interactive_attention_monitor.py` | 244–315 | `cde8ac229f6a5c5f79b93d8d1321113561a16524186572631627f161cab9e563` |
| D21 | `src/gobby/agents/interactive_attention_monitor.py` | 383–433 | `d51a9d7a49e2fc23fa6b48757c15be8196436b09b95b530eaef7576d7b753051` |
| D22 | `src/gobby/agents/spawn_executor_providers.py` | 170–225 | `ef896dfa8f5ba51009d9acec464848d1b250b0903a6705811b9d8657cbc250e6` |
| D23 | `src/gobby/agents/srt_runner.mjs` | 80–149 | `30cc5ce50a00dc694b5aa2ccb422329e05d0e979086b21a086deb12b0622b2e2` |
| D24 | `web/src/components/app/useAppProjectSelection.ts` | 95–193 | `0d76b1b84e27374c89260f568af9cfe6cdfad1e0fdab7d80e3c08af635ae43f2` |
| D25 | `web/src/components/app/__tests__/useAppProjectSelection.test.tsx` | 1–130 | `4f9b110d62a36cd087eaa451bcb06af2df31293fc0679f39a36f6500f364434b` |
| D26 | `web/src/components/app/__tests__/useAppProjectSelection.test.tsx` | 130–175 | `35cb0031818ffc49edf343d447d947c5c4f4274bbf30aa3febf81c098da4d23a` |
| D27 | `web/vite.config.ts` | 80–129 | `192ccf76c3256bf103562fa34807096751b3de2ab689c25d8ef5c641275d4e3d` |
| D28 | `tests/scheduler/test_cron_storage.py` | 220–253 | `009e6bce416c7b5b5373b53f59f92f14e22a10db6fd396428bb8fb3804df41f4` |
| D29 | `web/src/components/settings/sections/AppearanceSection.tsx` | 180–206 | `8ab4605ef440a6b86fa9e09bd7bd3c6f9a82e2bf74d9751187786f390b2d31f8` |
| D30 | `web/src/__tests__/settingsSliderFocus.test.ts` | 1–86 | `b6b0c30417496b91f1b7dda9f6a7d12dbbe70e218faab4af8627a2895d74bd0d` |
| D31 | `web/src/components/shared/editableContent.ts` | 72–115 | `af6b122a1181af0fed6c53c5c861c5b1f113a42e98f5f43f1dcd234d53fc14b6` |
| D32 | `web/src/components/activity/FilesTabTree.tsx` | 190–215 | `80aa32ead4f9ee2adc2b9e6b5f777fc9eff27d1c98e1b331c540131aff9f061f` |
| D33 | `web/src/components/activity/FilesTabTree.tsx` | 276–300 | `4c459db2454ac71495c23ead754941f8c64b22bf32acfdfdf6832d4eb688677e` |
| D34 | `web/src/lib/keyboard.ts` | 1–20 | `7686f899c0301089c37e96eba100ac2b4711d22365116170a88862102fc43670` |
| D35 | `web/src/hooks/useFiles.ts` | 419–475 | `bf14e8a5fdbd68af965e0c6931ed690e0111a262ef8fa04be2c52f6fe27bc1ab` |
| D36 | `web/src/hooks/__tests__/useFiles.save.test.tsx` | 45–75 | `09cd5ffe127928e1efcba51079428259bc9331499ce777bc0a31b1da4167533f` |
| D37 | `web/src/components/activity/SessionsTabMenu.tsx` | 20–106 | `0bf5fddff7a084aef11d97564ffa21b613f47020e29eaee9d1212e08ae89e93d` |
| D38 | `web/src/components/activity/TaskCloseDialog.tsx` | 58–76 | `4af036a99533c0620a0b1271d023514ad8fc12fdd35000853fdcd3f8963a3bde` |
| D39 | `web/src/components/activity/TasksTabFilters.tsx` | 60–90 | `924de0cc5e99512b6769cadce28cc219a9da4e0ca62693b1b84d0fed90558920` |
| D40 | `web/src/components/activity/FilterPrimitives.tsx` | 126–178 | `3949c2ac427a02a3085a6a9558545b866e68c2df575aef3931294d88b318b208` |

### Batch 4 reproducible report validation

Run this focused report/source check from a checkout containing the report,
historical Git objects and preserved audit refs. It reconstructs the fixed
inventory from the committed report, checks all four batches’ totals, and
runs no runtime test suite.

```bash
uv run python - <<'PY'
# batch-4-audit
import hashlib
import re
import subprocess
from collections import Counter
from pathlib import Path

PIN = "4ccb3e966ed18fe13cd9f268d55143128d034048"
REPORT = Path(".gobby/plans/research/closed-unlanded-audit-2026-10.md").read_text()
EXCLUDE = {23201, 23252, 23304, 23345, 23519, 23626, 23269}

def git(*args):
    return subprocess.check_output(["/usr/bin/git", *args])

original = REPORT.split("## Every closed task with an unlanded commit\n", 1)[1].split("\n## Per-commit", 1)[0]
proof = REPORT.split("## Per-commit supersession and recovery evidence", 1)[1].split("## Every closed task", 1)[0]
needs_inspection = {
    sha for sha, body in re.findall(r"### `([0-9a-f]{40})`\n(.*?)(?=\n### `|\Z)", proof, re.S)
    if "\nMissing linked revision, semantic supersession not proven." in body
}
inventory = []
for line in original.splitlines():
    if not line.startswith("| #") or not line.endswith("Missing linked revision, semantic supersession not proven |"):
        continue
    columns = line.strip("| ").split(" | ")
    seq_num = int(re.match(r"#(\d+)", columns[0])[1])
    oids = [oid for oid in re.findall(r"`([0-9a-f]{40})`", columns[2]) if oid in needs_inspection]
    if oids and seq_num not in EXCLUDE:
        inventory.append(({"seq_num": seq_num, "closed_at": columns[1]}, oids))
inventory.sort(key=lambda row: (row[0]["closed_at"] or "", row[0]["seq_num"]), reverse=True)
assert len(inventory) == 89
batch = REPORT.split("## Semantic recovery review: batch 4", 1)[1]
batch = re.split(r"\n## Semantic recovery review:", batch, maxsplit=1)[0]
classification = batch.split("### Batch 4 classifications", 1)[1].split("Batch totals:", 1)[0]
rows = re.findall(r"\| #(\d+) — .*? \| `([0-9a-f]+)` \| (missing|delivered|intentionally superseded) \|", classification)
assert [(int(n), sha) for n, sha, _ in rows] == [(t["seq_num"], oids[0][:10]) for t, oids in inventory[45:60]]
assert Counter(v for _, _, v in rows) == {"delivered": 12, "intentionally superseded": 2, "missing": 1}
assert "29 tasks remain" in batch
totals = Counter()
for number in range(1, 5):
    section = REPORT.split(f"## Semantic recovery review: batch {number}", 1)[1]
    section = re.split(r"\n## Semantic recovery review:", section, maxsplit=1)[0]
    section = section.split("Batch totals:", 1)[0]
    totals.update(re.findall(r"\| (missing|delivered|intentionally superseded) \|", section))
assert totals == {"delivered": 28, "intentionally superseded": 11, "missing": 21}
hashes = re.findall(r"\| D\d+ \| `([^`]+)` \| (\d+)–(\d+) \| `([0-9a-f]{64})`", batch)
assert len(hashes) == 40
for path, start, end, expected in hashes:
    excerpt = b"".join(git("show", f"{PIN}:{path}").splitlines(keepends=True)[int(start)-1:int(end)])
    assert hashlib.sha256(excerpt).hexdigest() == expected, path
path_rows = re.findall(r"\| #(\d+) — .*? \| `([0-9a-f]+)` \| (\d+) \| (\d+) \|", batch)
assert len(path_rows) == 15
for (n, prefix, present, absent), (task, oids) in zip(path_rows, inventory[45:60], strict=True):
    assert int(n) == task["seq_num"] and prefix == oids[0][:10]
    paths = git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", oids[0]).decode().splitlines()
    found = sum(subprocess.run(["/usr/bin/git", "cat-file", "-e", f"{PIN}:{path}"], capture_output=True).returncode == 0 for path in paths)
    assert (found, len(paths)-found) == (int(present), int(absent))
    holding = git("for-each-ref", f"--contains={oids[0]}", "--format=%(refname)", "refs/heads", "refs/remotes").decode().splitlines()
    if task["seq_num"] == 17462:
        assert holding == ["refs/heads/0.4.44"]
    else:
        assert not holding
        assert git("rev-parse", f"refs/audit/24076/{oids[0]}").decode().strip() == oids[0]
assert "7daf01cf5552fc65d2e07ffaca06016ac7db6fa5" in batch
assert "7f719a24-e8a6-4e13-8bb9-ee73adf3cdce" in batch
assert "db116bc8-3028-4307-8307-f5b3f7cd340e" in batch
ancestors = (
    "da44e2fa9e42fc149a7414c1b1492dda48af6269",
    "1242097f2fb0abd571b70c4339b462b9e4131390",
    "9c34494c6ce5d1c6f8688fea7c042804907b3136",
    "c3c13e0ed4af73e6dc103322c559f16ffa5e5bd5",
    "e87998c1399a3fe149184da14e0749e03c97c07c",
    "6019aa7a68221fe828bc89af72fad02569ee62f4",
    "169bffaddb3ccf94404774826c265f045576e2c5",
    "d1ef17cbab0f4e9b7c1ff2a358f70ab77a3b6a1b",
    "5396812e8902bc278cd21405d08f5bc88e3fe83d",
    "8358d7bba6973b5ba58ea9340b0437bbb492be46",
    "a2b779f60ff0020d8e5704bb37ecc345f1f6e754",
    "5694253eb6232fc0cae9dc6a27521b176dea98d5",
    "292bcb1711a816ded4fc5bab46104d14a1abed08",
    "927af24ab739527f160a10b2cf3b4bcd0a7d672e",
    "a718a4fd9797f4c6a7365c992f5b3b78360555a2",
    "200d7aa0503dff8d4afab28694b47217bcb5a839",
    "b6294eb62ec86e0b44ac069596459bc6adff0f37",
    "7e2a555681b20299de27ed18d15cf098af7b32c2",
)
for ancestor in ancestors:
    subprocess.run(["/usr/bin/git", "merge-base", "--is-ancestor", ancestor, PIN], check=True)
for packet in ("06-tasks-plans-review-learning.md", "07-workflows-skills-orchestration.md", "08-mcp-proxy-hub-services.md", "09-servers-sessions-runtime.md"):
    git("show", f"{PIN}:.gobby/plans/completed/coderabbit-fixes/{packet}")
assert sum(line.startswith("```") for line in REPORT.splitlines()) % 2 == 0
assert REPORT.endswith("_Last verified: 2026-10-12_\n")
print("PASS: 89 inventory; batch 4 order/counts; 60 cumulative classifications; 40 hashes; 15 path/holding checks; archive refs; referral; 18 ancestors; packet ledgers; Markdown/footer")
PY
```

Validation records the focused audit above and `git diff --check`; no heavy
command or full suite is needed. The batch 3 validator is also rerun after
the ref/scoping repair.

## Semantic recovery review: batch 5 (75 of 89 cumulative)

This addendum reviews positions 61–75 in the unchanged newest-first snapshot:
15 tasks, 16 revisions. Evidence remains bound to commit
`4ccb3e966ed18fe13cd9f268d55143128d034048`, tree
`428e1e0a2babda4fe2edf5c6bf56ae4950dbaf43`, gcode checkout project
`19b73420-3946-5c6a-8a06-4c7522f77543`. The report branch starts at reviewed
batch 4 commit `b91cba7023f4b6bc126ddc1d4e6d5c43298c1747`; cutover changes
neither the evidence pin nor the original audit inventory.

### Batch 5 classifications

| Task and title | Unproven revision | Classification | Pinned evidence and disposition |
| --- | --- | --- | --- |
| #17461 — Fix tmux window title names rejected by tmux | `9a6edcc43d` | intentionally superseded | The colon-to-hyphen sanitizer conflicts with the explicit #22163 **Session labels** contract, which keeps project#ref:Provider/Task punctuation. E1–E3 preserve persisted/manual titles with tmux-format escaping; E2 asserts Manual:title survives. The old summary-actions title path belongs to #21140 **Structured session handoffs** retirement. |
| #17364 — Add wiki branch setup to init and install hooks | `a6a0a91b6e` | intentionally superseded | Equivalent wiki setup landed in d1ae71eb55c48aea6df87e4e6aeb6ccd6d76c954, then #21796 **Wiki retirement** withdrew provisioning. E6 checks the retired CLI surface. Independent general Git initialization, including existing-project setup, and pre-push ref capture survive (E4–E5). Wiki publication/bypass behavior is withdrawn. |
| #17036 — Build the Integrations activity tab | `f2d485d813` | delivered | Equivalent 9fc3e0fb06a6814a1fbf0b6f172038f057daa954 is a pin ancestor. E7 registers the tab. E8–E12 retain drafts, CRUD/toggle actions, dirty-navigation confirmation, messages, configuration/secret separation and finite-number validation. ChannelsList, ChannelDetailPanel and IntegrationsFilterPanel replace the three old names. E13–E14 retain registration, filtering, actions, save/secret and unavailable-manager regressions. |
| #15873 — Wire set_websocket_broadcast and fail gobby_chat sends when broadcast is unset | `fc89631eb4` | delivered | E15 rejects an unset callback, awaits broadcast and propagates failures before returning the platform ID. E16 wires the server callback; E17 reapplies pre-start wiring on activation. E18 retains failed-send and pre-start-wiring cases in the renamed manager test module. |
| #15711 — Bump version to 0.4.18 on branch 0.4.18 | `8fbaa32400` | intentionally superseded | Task scope explicitly names release branch 0.4.18. Its three changes only set release metadata. E19–E21 prove the distinct integration package/module/lock version 0.5.0. The release branch retains the object; no independent behavior or regression needs recovery. |
| #15562 — Bump version to 0.4.15 on release branch | `dc4f915849` | intentionally superseded | Task scope explicitly names release branch 0.4.15. The two changes only set package/module metadata; local and origin release refs retain them. E19–E20 prove the separate 0.5.0 contract. |
| #15329 — Bump version files for 0.4.12 branch | `89bf2aabcd` | intentionally superseded | Criteria require the three 0.4.12 versions on its release branch, retained by local/origin refs. E19–E21 prove the integration version. There are no independent runtime or test changes. |
| #15293 — Prepare and publish 0.4.11 release | `d4cf939a6d` | intentionally superseded | Criteria scope versions and the complete changelog entry to release branch 0.4.11, with integration restored to 0.5.0. The 81-line entry is absent at the pin and retained on release refs; that branch-scoped absence is intentional. Independent edits in all four guides were checked separately: E22–E26 retain native projection ownership, sync-worker settings, positional filters, graph/vector commands and HTTP shims. #22237 **Verified code-index guidance** replaces obsolete setup/sync-projections/require-cpp-semantics commands. #22197 **Generated manifest retirement** withdraws the tracked manifest while E27 preserves package-time generation. The SDK lock downgrade is obsolete. No release CI/publication receipt is asserted. |
| #15296 — Add dispatcher dependency base guard | `9d396e5531` | intentionally superseded | The guard and its regression never landed. Old caller paths still existed at the pin, so absence alone is insufficient. The Orchestrator explicitly ruled this withdrawn under the approved **Remove Gobby Build** plan. E28 names build/workspaces.py and dispatch/spawn.py for deletion; the plan removes both packages. Neither requested guard path survives to recover. No replay or recovery task follows. |
| #15132 — Make plan artifacts reachable in isolated review worktrees | `dc641be8f3` | delivered | E29–E32 trace the replacement read path: resolve/read the author plan, publish an evidence ID, retrieve immutable UTF-8 bytes independent of reviewer cwd. The TOCTOU regression keeps original bytes after source changes. #23846 **Dispatch-only spawn hook retirement** removes planner/plan-enhancer roles and their source-parent write grants; the adversary gate remains. No surviving writer is claimed to use retired symlink/copy staging. Independent agent-sync bool cleanup now uses typed json_equal (E33–E34); condition-helper changes were formatting only. |
| #15133 — Preserve ambient MCP session context for reviewer agents | `f377dda09f` | delivered | E35–E37 forward child identity, managed daemon/grant context and sandbox cache variables into Codex's scrubbed MCP process. Parent identity is deliberately withheld so leases belong to the child. E38 reconstructs and checks child identity, daemon and capability path, without parent ID or raw token. Literal environment overrides replace the old forwarding implementation. |
| #11235 — Remove session_id parameter from task/variable tool calls | `000334f1a5` | missing | Partial loss across both revisions, including b55a28e3a16effdf74f2dad8e0f5bf3784afcbdc: four optional-session tools lose ambient caller scope (E39–E47). Required-only injection does not recover them. #24130 **Caller-session scoping recovery** owns suggest_next_task, get_messages, list_identities and search_skills. Required-session and variable routes survive/adapt. #22308 **Show all skills** explicitly withdraws list_skills active allowlisting; keep it out of recovery. Other historical deltas are evaluated below. |
| #11350 — Unified CLI Launcher — SDK Removal + Interchangeable Web Chat Sessions (v3) | `c741003136` | delivered | The linked six-file revision contains two production EOF whitespace removals, three test-formatting changes and nine Codex mock-name replacements, with no independent launcher behavior. Formatting is carried or superseded by later formatting. The CodexAppServerAdapter mock belongs to the chat module explicitly retired by #11434 **Unified CLI chat sessions**; no live target remains to repair. This classification covers the linked revision, not the epic's eight-phase scope. |
| #11357 — Phase 7: Remove Claude Agent SDK and associated code | `3d4269f973` | intentionally superseded | #20654 **MCP 2 migration** explicitly retains/resolves the SDK; E48–E49 prove its dependency/imports remain. Independent helper moves were relocations: E51–E52 and retained sdk_utils/tests preserve plan permission, pending approval, fallback and compaction helpers. #20475 **Default-agent persona** replaces the old prompt loader (E50). #12812 **Conductor retirement** and #11434 **Unified CLI chat sessions** withdraw the other absent provider/chat paths. Old SDK removal would contradict supported chat behavior. |
| #11246 — Add session_id param to set_variable and get_variable in stdio MCP proxy | `4c3a205eaa` | delivered | E53–E54 retain explicit session targeting in stdio schemas/delegation. Required session_id replaces the optional/default surface. E55 checks the target and step scope. The original stdio.py implementation is decomposed. |

Batch totals: **6 delivered, 8 intentionally superseded, 1 missing**.
Cumulative totals: **34 delivered, 19 intentionally superseded, 22 missing**;
**14 tasks remain**. These classify source and explicit decisions; no fresh
runtime tests, registry activation or release publication are asserted.

### Batch 5 recovery referral

#11235 **Ambient session cleanup** has two unproven revisions:
`000334f1a563c54aa03ddcebb9247125eb40ca13` and
`b55a28e3a16effdf74f2dad8e0f5bf3784afcbdc`. Neither has a local branch or
remote-tracking holder. Both have exact `refs/audit/24076/<full-SHA>` archives,
with descendant audit archives also retaining history. Archives prove
recoverability, not delivery.

Inbox Manager receipt `e051b26e-4553-486e-966a-aa4a910f44ac` and Lane Manager
copy `6f060583-7314-447c-ae4f-662c29d9dda1` reported initial losses.
Follow-ups `0fca1877-81bd-4528-b67d-6f77c7e65ac0` and
`12c4bfa0-a058-4063-96b7-1eee7b677115` add identity listing and confirm skill
search. The Orchestrator filed/extended #24130 **Caller-session scoping
recovery**; the Inbox Manager confirmed all four cases are included.

- E39–E41: suggest_next_task(session_id=None) passes None to the ready resolver,
  bypassing automatic session_task selection.
- E42–E43: get_messages resolves omission to None and passes no caller filter.
- E42 and E44: list_identities resolves omission to None and skips filtering.
- E45: search_skills reads active_names only when session_id is supplied.
- E46–E47: proxy injection covers required schema fields; these four optional
  fields receive no caller value from that mechanism.

Recovery preserves explicit cross-session targets. #22308 **Show all skills**
replaces list_skills allowlisting with explicit exclusions; do not restore it.
Other deltas were inspected independently: memory_write recovers ambient
provenance; required session/transcript/worktree tools recover identity through
proxy injection; spawning retains parent context at the proxy boundary; variable
delegation survives in E53–E55. The old session-start heuristic is replaced by
#21140 **Structured session handoffs** and its marker protocol. Deprecated
pipeline/skill/provider docs and fixtures describe retired interfaces; surviving
tool/runtime regression edits are carried/adapted. The SDK 0.1.39/root 0.3.2
lock downgrade is obsolete under the current SDK and root 0.5.0 contract.

### Batch 4 LOW-1 correction included in this batch

Both overbroad digest-survival claims are corrected. E56–E57 prove the two
attestation sites use inline hashing after baseline restoration
`a2b779f60ff0020d8e5704bb37ecc345f1f6e754`. E58 preserves the helper and E59
its _source_digest use. Both encodings use key-sorted compact JSON and SHA-256,
producing identical bytes. Missing call-site substitutions are maintainability
cleanup for #24128 **Packet 06 review-learning hardening**, alongside six prior
losses. #19004 **Task/plan/review packet** stays missing; totals and retired
receipt/convergence dispositions are unchanged.

### Batch 5 touched-path and replacement audit

Every revision's full Git path inventory was checked at the pin. Historical
production, documentation, installation and test deltas were followed separately
to replacements. Presence counts are inventory checks, not delivery proof.

| Task and title | Revision | Paths present at pin | Paths absent at pin |
| --- | --- | --- | --- |
| #17461 — Tmux titles | `9a6edcc43d` | 2 | 3 |
| #17364 — Wiki setup | `a6a0a91b6e` | 6 | 1 |
| #17036 — Integrations tab | `f2d485d813` | 10 | 3 |
| #15873 — Chat broadcast | `fc89631eb4` | 4 | 1 |
| #15711 — Release 0.4.18 | `8fbaa32400` | 3 | 0 |
| #15562 — Release 0.4.15 | `dc4f915849` | 2 | 0 |
| #15329 — Release 0.4.12 | `89bf2aabcd` | 3 | 0 |
| #15293 — Release 0.4.11 | `d4cf939a6d` | 8 | 1 |
| #15296 — Dependency base guard | `9d396e5531` | 4 | 2 |
| #15132 — Plan reachability | `dc641be8f3` | 3 | 2 |
| #15133 — Reviewer MCP context | `f377dda09f` | 3 | 0 |
| #11235 — Ambient session cleanup | `000334f1a5` | 17 | 10 |
| #11235 — Ambient session cleanup (second revision) | `b55a28e3a1` | 17 | 2 |
| #11350 — Launcher linked cleanup | `c741003136` | 5 | 1 |
| #11357 — SDK removal | `3d4269f973` | 18 | 9 |
| #11246 — Stdio variable targeting | `4c3a205eaa` | 1 | 0 |

Holding refs and absent-path dispositions:

- #17461 **Tmux titles** remains on refs/heads/0.4.43. titles.py is replaced
  by E1–E3; summary_actions.py and tests belong to structured-handoff retirement.
- #15711 **Release 0.4.18** remains on local 0.4.18; #15562 **Release 0.4.15**,
  #15329 **Release 0.4.12** and #15293 **Release 0.4.11** remain on their local
  and origin release refs. #15296 **Dependency guard** remains on local 0.4.11.
- Other revisions have no branch/remote holder and exact audit archives.
- #17364 **Wiki setup** loses only wiki_branch_setup.py, explicitly retired.
- #17036 **Integrations tab** loses IntegrationsTabDetailPanel.tsx,
  IntegrationsTabList.tsx and IntegrationsTabToolbar.tsx; replacements are
  ChannelDetailPanel.tsx, ChannelsList.tsx and IntegrationsFilterPanel.tsx.
- #15873 **Chat broadcast** loses test_manager.py; E18 retains its cases.
  Runner wiring moved into runner_init/servers.py and communications/lifecycle.py.
- #15293 **Release 0.4.11** loses bundled_content_manifest.json; E27 generates it.
- #15296 **Dependency guard** loses base_guard.py and its regression, withdrawn
  by the approved whole-surface retirement rather than mere path absence.
- #15132 **Plan reachability** loses _plan_artifacts.py and its regression.
  Snapshots replace read staging; writer roles/source grants are retired.
- #11235 **Ambient cleanup** first revision loses GEMINI.md, _session_start.py,
  build-rule/bug skills, deprecated coordinator/dev-loop pipelines,
  nightly-fixes/orchestrator pipelines and old pipeline/task coverage modules.
  The second loses session-lifecycle.yaml and old task coverage. Their retired
  interfaces do not erase the four live optional-session losses.
- #11350 **Launcher cleanup** loses only the explicitly retired Codex chat test.
- #11357 **SDK removal** loses the old Codex/Gemini adapters, conductor manager,
  text_utils, CLI/Codex chat modules and permission/base/text tests. Named
  provider/chat retirements and retained helpers account for these relocations.
- #11246 **Stdio targeting** loses no changed path; implementation is decomposed.

### Batch 5 replacement and retirement ancestors

All full OIDs below are ancestors of the fixed pin, distinct from unproven
revision archives.

| Decision or replacement | Ancestor |
| --- | --- |
| #22163 — Session labels | `a833a68b2529abe309f0f7fa5e0358b48ad87051` |
| #17364 — Wiki setup equivalent | `d1ae71eb55c48aea6df87e4e6aeb6ccd6d76c954` |
| #21796 — Wiki retirement | `708128e7420538dbafab0c7b9612d29176363c3d` |
| #17036 — Integrations equivalent | `9fc3e0fb06a6814a1fbf0b6f172038f057daa954` |
| #21140 — Structured handoffs | `5694253eb6232fc0cae9dc6a27521b176dea98d5` |
| #22237 — Verified code-index guidance | `1cd0d4ba9cb56ed84306d70347080d625be52168` |
| #22197 — Generated manifest retirement | `f54563fc4bcc74d220ff3abcf67e48aa5cc69e1d` |
| #23846 — Dispatch-only spawn hook retirement | `acad860266087cfd21aa5fa90be353e65744ca03` |
| #22308 — Show all skills | `5f4ac0a3ebab8380f3e41fdd53ef8aac2a814961` |
| #11434 — Unified CLI chat sessions | `fbaab4ca2bc8f3a16fe3785e9f2cfff656e35bde` |
| #20654 — MCP 2 migration retaining SDK | `eff0bf72e6d95cbb44811c970fea7a0ec091b8ee` |
| #20475 — Default-agent persona | `e3f9fb6f5cb8567e396460236a8da35e2610a0aa` |
| #12812 — Conductor retirement | `70d2d47345edadbabeea9c8652af04c706a42275` |
| #19368 — Baseline review restoration | `a2b779f60ff0020d8e5704bb37ecc345f1f6e754` |

### Batch 5 deterministic evidence ledger

Native range reads checked complete responses, exact binding and raw UTF-8
excerpt hashes. The validator recomputes them from the fixed Git blobs. Test
citations show retained source, not fresh runtime passes.

| Evidence | Path | Lines | Excerpt SHA-256 |
| --- | --- | --- | --- |
| E1 | `src/gobby/sessions/tmux_window_naming.py` | 81–86 | `b1d1222c08aec5ef5171e35eab333c9969bf6d355bc0c0b2240fc39882a88594` |
| E2 | `tests/sessions/test_tmux_window_naming.py` | 425–434 | `0c500e9b11cdfa7502fb356d24396c68f2cef3085f259e7b653e89134f16af1c` |
| E3 | `src/gobby/agents/tmux/session_manager.py` | 72–77 | `29363f386b42cf216c8ea2cd334f5ef06c2a4c612b5b6c02f2167a3cdc824071` |
| E4 | `src/gobby/cli/init.py` | 113–160 | `4f25facc331dbc8d8071c27e81e0d6a9a95b24ac18347086ece3a43fb21e101f` |
| E5 | `src/gobby/cli/installers/git_hooks.py` | 249–282 | `0b45b85c59f41931fad7b3d4a81d344f2efcbc712fcc1f0f90f25a6d47a75101` |
| E6 | `tests/test_wiki_retirement_contract.py` | 22–30 | `4b22def371af0e93b03f83c9056f5b562c905b2e3613e6167cb6b458186bdc6f` |
| E7 | `web/src/components/activity/ActivityPanel.tsx` | 479–496 | `c2fa06cc0e7091ce01907c86d1ab45dc85559a678622209e195b92a35fe72c51` |
| E8 | `web/src/components/activity/IntegrationsTab.tsx` | 100–180 | `9867482c805b608f1977b72b8e0808144e7e204e8cc5af1ca958f741c07ff77c` |
| E9 | `web/src/components/activity/IntegrationsTab.tsx` | 300–366 | `c276b2edb5b26a589a0c8839ddaccd3fe684a5d3f5bbd75f5cbf6cfc39e78346` |
| E10 | `web/src/components/activity/integrations/ChannelDetailPanel.tsx` | 120–200 | `eb1a9889162083ca87b6f74ede8969677e3a0f7878f3e9129e01a73f99fbf0c3` |
| E11 | `web/src/components/activity/integrations/IntegrationsTabModel.ts` | 105–158 | `3fbc131bed6668770d3eb2feb42e8ab6bca9becf8c8fe54fde5dd0a0f0a5ce7a` |
| E12 | `web/src/components/activity/integrations/IntegrationsTabActions.ts` | 1–50 | `472247bdf059804a5f6b83e5b43db85f03d936d61d7d35de798b7b2b283109ac` |
| E13 | `web/src/components/activity/integrations/__tests__/IntegrationsTab.test.tsx` | 226–402 | `22f6ffec76655be5dc6426e3c4dd41d4b9a751fd8c380da6d98a8f7730620f2f` |
| E14 | `web/src/components/activity/integrations/__tests__/IntegrationsTab.test.tsx` | 490–563 | `8de06e86843c0d92d6aa2365aa6a710f79e96535381a7c0c9a1f707e203e4bc1` |
| E15 | `src/gobby/communications/adapters/gobby_chat.py` | 102–122 | `44e51c87d0603146d56f70507ab392feb937e7199c711d8fb4cb559466addd37` |
| E16 | `src/gobby/runner_init/servers.py` | 336–345 | `f084f63d578fb3883dec3b85168e7e782aee1edf6b333568d04493127c8ee54d` |
| E17 | `src/gobby/communications/lifecycle.py` | 157–176 | `a4489f203acdc593187fdd2bdfcae8d6c25bd02266c414f4b43526c3ce8f6a76` |
| E18 | `tests/communications/test_communications_manager.py` | 2087–2127 | `4953414344092fa797aab5d0a2527d60d7ca9d780513421f4b9ddd1f62fb4780` |
| E19 | `pyproject.toml` | 1–12 | `bea8e34888931746e22c77583b2469ebf5436e6de01a0bcda8b5aa7f18e9cb9d` |
| E20 | `src/gobby/__init__.py` | 1–9 | `c4e582075429f7998e19974175d7d6f3d6f4da0663b28babe52c3c655fa4fc5f` |
| E21 | `uv.lock` | 975–983 | `6a61b8184a6c74820450e2b94969d049a5ffb17ca403f782cb6f30291495a630` |
| E22 | `docs/guides/code-index.md` | 74–102 | `5ff8231e1eae7e57fb281e0fde2a19e6854eb72054d4a8d8ad6ad423d8a8e891` |
| E23 | `docs/guides/code-index.md` | 151–192 | `1f97f6bb89f97cd5bc3e3ea594aeac312cc80805836652c48e86b6409bd20934` |
| E24 | `docs/guides/configuration.md` | 575–595 | `29436996e910da5b7dc3be3881e0d3a20b40310c816462f68af43e4cd3594a98` |
| E25 | `docs/guides/http-endpoints.md` | 765–772 | `34c629a163bdb33bde51dc40541082e022404e55eec73ceea400f8fc45f82d9c` |
| E26 | `docs/guides/search.md` | 61–68 | `5058bd395c9efa36e35ee5ecefd5a05478bc3272a4d69b03309f514b9386413a` |
| E27 | `src/gobby/install/manifest.py` | 67–92 | `98c346898dc9019ebebe5ea51805482a2294375a636fe8f8433304aa4382cca8` |
| E28 | `.gobby/plans/remove-gobby-build.md` | 568–603 | `e4154529258502679153e63b80e55208c635134222977621b8df29a99f1a47b8` |
| E29 | `src/gobby/dispatch/spawn.py` | 87–136 | `71ae42d165b9b60a731de1dc60a240a4da2ff9e985e071cfeef693d7950a7c2b` |
| E30 | `src/gobby/plans/review_evidence.py` | 89–114 | `7dc3da4d1d623cd98d514812987bdd68ce39f0ed924daf866b4c4bde2c47eb72` |
| E31 | `src/gobby/mcp_proxy/tools/plans/review_evidence.py` | 125–160 | `be09ddfdccbcb6cf406720e1d1c75d4d33e5d88ac86ff008d39f4257f1d28118` |
| E32 | `tests/plans/test_review_evidence.py` | 369–385 | `8c3e667d77f61652edcc7408d74ed323d2cacbf6c999b7a4048360fe07c46530` |
| E33 | `src/gobby/agents/sync.py` | 71–75 | `8a99b97e3f27b4d120e3c0b36af3a976aae54f3f457272cb536bdd2f0027d45b` |
| E34 | `src/gobby/utils/json_helpers.py` | 131–135 | `0c8964b738ad262cd2bcac0c0b35a6d07ddc7021158d9df9335c21c055d2075f` |
| E35 | `src/gobby/agents/spawn_executor_support.py` | 68–75 | `333e536b0b9e34770531ca462356e4a6bdd10e9037783c2caea60e0c59d4dbe4` |
| E36 | `src/gobby/agents/spawn_executor_support.py` | 613–640 | `593a0a6a503196cc44aaa4564da6b3118490f5af6e4a0f1aa83dbb2b025aece9` |
| E37 | `src/gobby/agents/spawn_executor_providers.py` | 426–440 | `cab3b6cd35c73d9ec294135331604b1a7aae9e9dff460aa8d276c066beca1090` |
| E38 | `tests/agents/test_spawn_executor.py` | 2395–2460 | `37e5df9a0e072880c8d5374762205644f91b94662a331bde60d9c1aa7aa1ae1e` |
| E39 | `src/gobby/mcp_proxy/tools/task_readiness.py` | 555–562 | `235dc356f0df9fa920d1d04a3651fd45cb8666cc85eec9cafa4706eb83864f2f` |
| E40 | `src/gobby/mcp_proxy/tools/task_readiness.py` | 602–610 | `bcedc43cb96e0e1c53aec4ad1815020bea9bb545f245649e25d893b52201f10b` |
| E41 | `src/gobby/mcp_proxy/tools/task_readiness.py` | 226–242 | `cd3fccd98293829117101fba9f8db9f0582e24b37671c09f0be9f4b01b92ce78` |
| E42 | `src/gobby/mcp_proxy/tools/communications.py` | 44–52 | `dfea39c46ebb67d4c7b06a059bb7c46093b26a73441dc307bc6c53c2dc6c193c` |
| E43 | `src/gobby/mcp_proxy/tools/communications.py` | 319–349 | `a98d9970c2202acefe85c613ab2d530554aee4070a0bd49a679af06f99adfc0c` |
| E44 | `src/gobby/mcp_proxy/tools/communications.py` | 511–529 | `e2ceef0625f2ab28fd64a8c508f1a91f64996c1346c59ca42ee0cd1a079a4687` |
| E45 | `src/gobby/mcp_proxy/tools/skills/search_skills.py` | 108–126 | `c4beb3bd953f8e5332ce486437c7bf008cefc9d6686356aeb15426b98ace89b5` |
| E46 | `src/gobby/mcp_proxy/services/tool_execution.py` | 133–180 | `e76cf7d720b477aff32ef8a0c0bae2a5844816d54d5dcacb6c884dd8fc0d7685` |
| E47 | `src/gobby/mcp_proxy/services/session_context.py` | 28–41 | `105f5103c3bf008432cad53df88885c24fd73b9c7ff85d8cd299d1096ba062d0` |
| E48 | `pyproject.toml` | 33–42 | `951f84ca1d965e1ff99158a75bf9c19227a8b9cba1778fd84401fc9a01bb391f` |
| E49 | `src/gobby/servers/chat_session.py` | 20–26 | `e610c41ef75d6421e164a872ca88d27661b52f24718788ffa797301bfd039ccb` |
| E50 | `src/gobby/servers/chat_session.py` | 314–329 | `23a6b651261cbb2c82edf6738d4fa9462a2edc3f1187670173f28bd639c22d66` |
| E51 | `src/gobby/servers/chat_session_helpers.py` | 71–99 | `bbcb7468041324d49e866e12fc603990432d146c195c6a30c4f48ba4e4225e6c` |
| E52 | `src/gobby/servers/chat_session_helpers.py` | 272–294 | `9c8eed08cabb866fd2d492fcf5f6a1b8590357330384b42371b63d91bbcf35ae` |
| E53 | `src/gobby/mcp_proxy/stdio_proxy.py` | 581–608 | `7161e9317c763e3140f4bf2b9e0a4fa3d2f704d5391a14cea60bad994267867a` |
| E54 | `src/gobby/mcp_proxy/stdio_tools.py` | 332–367 | `17f2438a630352285cbf4f05ac2de25bc8cd3b50b592cc75a8951331356d8b5b` |
| E55 | `tests/mcp_proxy/test_top_level_variables.py` | 25–99 | `d694c8f0ecab59c44f877ff82a8e03ef9ea957f25da5eb57903be69b9552c7e1` |
| E56 | `src/gobby/plans/review_coverage.py` | 309–318 | `8d6a4e2460d391cb3b675eb759510e5b557d184282d6c8750135017f79c761ee` |
| E57 | `src/gobby/plans/review_coverage.py` | 471–481 | `785f1d2df4e0f5509b82021f99b041f3c3cf36b76a3281497a392554e63ab685` |
| E58 | `src/gobby/plans/digests.py` | 9–12 | `d16a8c189f6ffa38ca200355d4a7946c449b0c01b6a256303052d12e5997dc7f` |
| E59 | `src/gobby/plans/review_coverage.py` | 813–821 | `fb81172756fa03c44a6ee1b7f9876a8a6712deff176d52e742ade6ee270593ec` |

### Batch 5 reproducible report validation

The focused validator below reconstructs the original 89-task snapshot,
checks both partial-loss revisions, derives 16 path inventories from Git,
verifies pinned excerpts and explicit retirement ancestors. It uses no ignored
cache or runtime suite. Batch 3 and batch 4 checks remain reproducible above.

```bash
uv run python - <<'PY'
# batch-5-audit
import hashlib
import re
import subprocess
from collections import Counter
from pathlib import Path

PIN = "4ccb3e966ed18fe13cd9f268d55143128d034048"
REPORT = Path(".gobby/plans/research/closed-unlanded-audit-2026-10.md").read_text()
EXCLUDE = {23201, 23252, 23304, 23345, 23519, 23626, 23269}
Q = chr(96)

def git(*args):
    return subprocess.check_output(["/usr/bin/git", *args])

original = REPORT.split("## Every closed task with an unlanded commit\n", 1)[1].split("\n## Per-commit", 1)[0]
proof = REPORT.split("## Per-commit supersession and recovery evidence", 1)[1].split("## Every closed task", 1)[0]
needs_inspection = {
    sha for sha, body in re.findall(r"### \x60([0-9a-f]{40})\x60\n(.*?)(?=\n### \x60|\Z)", proof, re.S)
    if "\nMissing linked revision, semantic supersession not proven." in body
}
inventory = []
for line in original.splitlines():
    if not line.startswith("| #") or not line.endswith("Missing linked revision, semantic supersession not proven |"):
        continue
    columns = line.strip("| ").split(" | ")
    number = int(re.match(r"#(\d+)", columns[0])[1])
    oids = [oid for oid in re.findall(r"\x60([0-9a-f]{40})\x60", columns[2]) if oid in needs_inspection]
    if oids and number not in EXCLUDE:
        inventory.append((columns[1], number, oids))
inventory.sort(key=lambda row: (row[0] or "", row[1]), reverse=True)
assert len(inventory) == 89
batch = REPORT.split("## Semantic recovery review: batch 5", 1)[1]
batch = re.split(r"\n## Semantic recovery review:", batch, maxsplit=1)[0]
classification = batch.split("### Batch 5 classifications", 1)[1].split("Batch totals:", 1)[0]
rows = re.findall(r"\| #(\d+) — .*? \| \x60([0-9a-f]+)\x60 \| (missing|delivered|intentionally superseded) \|", classification)
assert [(int(n), sha) for n, sha, _ in rows] == [(n, oids[0][:10]) for _, n, oids in inventory[60:75]]
assert Counter(v for _, _, v in rows) == {"delivered": 6, "intentionally superseded": 8, "missing": 1}
assert "14 tasks remain" in batch
counts = Counter()
for number in range(1, 6):
    section = REPORT.split(f"## Semantic recovery review: batch {number}", 1)[1]
    section = re.split(r"\n## Semantic recovery review:", section, maxsplit=1)[0].split("Batch totals:", 1)[0]
    counts.update(re.findall(r"\| (missing|delivered|intentionally superseded) \|", section))
assert counts == {"delivered": 34, "intentionally superseded": 19, "missing": 22}
hashes = re.findall(r"\| E\d+ \| \x60([^\x60]+)\x60 \| (\d+)–(\d+) \| \x60([0-9a-f]{64})\x60", batch)
assert len(hashes) == 59
for path, start, end, expected in hashes:
    excerpt = b"".join(git("show", f"{PIN}:{path}").splitlines(keepends=True)[int(start)-1:int(end)])
    assert hashlib.sha256(excerpt).hexdigest() == expected, path
path_rows = re.findall(r"\| #(\d+) — .*? \| \x60([0-9a-f]+)\x60 \| (\d+) \| (\d+) \|", batch)
expected_rows = [(n, oid) for _, n, oids in inventory[60:75] for oid in oids]
assert len(path_rows) == len(expected_rows) == 16
release_refs = {
    17461: ["refs/heads/0.4.43"],
    15711: ["refs/heads/0.4.18"],
    15562: ["refs/heads/0.4.15", "refs/remotes/origin/0.4.15"],
    15329: ["refs/heads/0.4.12", "refs/remotes/origin/0.4.12"],
    15293: ["refs/heads/0.4.11", "refs/remotes/origin/0.4.11"],
    15296: ["refs/heads/0.4.11"],
}
for (number, prefix, present, absent), (n, oid) in zip(path_rows, expected_rows, strict=True):
    assert int(number) == n and prefix == oid[:10]
    paths = git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", oid).decode().splitlines()
    found = sum(subprocess.run(["/usr/bin/git", "cat-file", "-e", f"{PIN}:{path}"], capture_output=True).returncode == 0 for path in paths)
    assert (found, len(paths)-found) == (int(present), int(absent))
    holding = git("for-each-ref", f"--contains={oid}", "--format=%(refname)", "refs/heads", "refs/remotes").decode().splitlines()
    assert holding == release_refs.get(n, [])
    if not holding:
        assert git("rev-parse", f"refs/audit/24076/{oid}").decode().strip() == oid
for oid in inventory[71][2]:
    assert oid in batch
assert inventory[71][1] == 11235
for receipt in ("e051b26e-4553-486e-966a-aa4a910f44ac", "0fca1877-81bd-4528-b67d-6f77c7e65ac0", "12c4bfa0-a058-4063-96b7-1eee7b677115"):
    assert receipt in batch
assert "#24130" in batch
ancestor_section = batch.split("### Batch 5 replacement and retirement ancestors", 1)[1].split("### Batch 5 deterministic evidence ledger", 1)[0]
ancestors = re.findall(r"\x60([0-9a-f]{40})\x60", ancestor_section)
assert len(ancestors) == 14
for ancestor in ancestors:
    subprocess.run(["/usr/bin/git", "merge-base", "--is-ancestor", ancestor, PIN], check=True)
plan = git("show", f"{PIN}:.gobby/plans/remove-gobby-build.md").decode()
assert "src/gobby/build/workspaces.py::*" + Q + " — operation: delete" in plan
assert "src/gobby/dispatch/spawn.py::*" + Q + " — operation: delete" in plan
for path in ("src/gobby/dispatch/base_guard.py", "tests/dispatch/test_dependency_base_guard.py"):
    assert subprocess.run(["/usr/bin/git", "cat-file", "-e", f"{PIN}:{path}"], capture_output=True).returncode != 0
coverage = git("show", f"{PIN}:src/gobby/plans/review_coverage.py").decode()
assert 'return canonical_json_sha256({"plan_hash": plan_hash, "sources": source_hashes})' in coverage
assert coverage.count('json.dumps(attestation, sort_keys=True, separators=(",", ":")).encode()') == 2
assert "[0.4.11]" in git("show", "d4cf939a6dd9ef9b0e466b501c505dbfe17979c3:CHANGELOG.md").decode()
assert "[0.4.11]" not in git("show", f"{PIN}:CHANGELOG.md").decode()
assert sum(line.startswith(Q * 3) for line in REPORT.splitlines()) % 2 == 0
assert REPORT.endswith("_Last verified: 2026-10-12_\n")
print("PASS: 89 inventory; batch 5 order/counts; 75 cumulative classifications; 59 hashes; 16 path/holding checks; both partial-loss revisions/referrals; 14 ancestors; approved retirement; LOW-1 source correction; release scope; Markdown/footer")
PY
```

Validation covers this focused audit, repaired batch 3 and batch 4 checks,
and `git diff --check`. No full suite or heavy command is required.

Validation passed: batch 5's 89-task inventory, order/counts, 75 cumulative
classifications, 59 hashes, 16 path/holding checks, both partial-loss revisions
and referral receipts, 14 ancestors, approved retirement, LOW-1 source correction,
release scope and Markdown/footer. The prior-batch rerun passed batch 3's 16
hashes/15 inventories/four ancestors and batch 4's 40 hashes/15 inventories/
18 ancestors, including prompt-loss and reconciliation-ledger checks.
Whitespace check passed. Only this report changed.

## Semantic recovery review: batch 6 (89 of 89 cumulative)

This final addendum covers positions 76–89: 14 tasks, 15 task/revision rows,
14 distinct revisions. The diagnostic and mode-doc tasks share 184a1e9642.
Evidence stays bound to commit `4ccb3e966ed18fe13cd9f268d55143128d034048`,
tree `428e1e0a2babda4fe2edf5c6bf56ae4950dbaf43`, project
`19b73420-3946-5c6a-8a06-4c7522f77543`. The Lane Manager authorized the
clean fast-forward to batch 5 landed tip
`6462c799e3dbdec1ff83b26571239bf3144266e3` before report edits.

### Batch 6 classifications

| Task and title | Unproven revision | Classification | Pinned evidence and disposition |
| --- | --- | --- | --- |
| #11243 — Add CLI diagnostic probe on SDK text generation failure | `184a1e9642`, `5a20e9e5c9` | missing | Partial loss: the independent post-failure CLI diagnostic, including stream-json --verbose, is absent. E1–E2 retain callback stderr capture/drain and executable discovery without the requested subprocess. #11283 **Consistent SDK diagnostics** and #17976 **Bounded stderr** improve capture without withdrawing the probe. #24136 **Recover CLI diagnostic probe after SDK failure** owns recovery. The second revision's hook suppression survives through E1/E3–E4 and landed headless-settings/provider consolidation. Shared mode-doc rows are accounted under #11241. |
| #11241 — Clean up stale mode='terminal' references in docs, docstrings, and YAML | `184a1e9642` | delivered | Documentation cleanup is carried by equivalent 404e434428 and later mode/document retirement. The shared revision fixes two remaining Merge/Review matrix rows; their agents skill is explicitly deleted by 5c379f5606. #11301 **Mode/template cleanup** withdraws the mode field. Its independent diagnostic hunk remains missing and is routed once under #11243/#24136. |
| #11242 — Fix set_variable receiving unexpected session_id kwarg from proxy | `a7a0e70f03` | delivered | E5–E7 show required target-session signatures and matching arguments in top-level MCP and replacement HTTP routes. Explicit targeting replaces the temporary ContextVar workaround. #20192 **Domain HTTP routes** retires workflows.py and relocates endpoints/tests. Historical test changes follow that call-contract change; batch 5 E53–E55 preserve explicit-target regression coverage. |
| #11238 — Fix /clear and /compact handoff context injection | `121640b3e5` | intentionally superseded | #21140 **Structured session handoffs** replaces source heuristics, generic parent adoption and summary injection with attempt-bound marker consumption. E8–E9 retain explicit clear/compact status and successor ownership. The independent handoff_source fallback, two inject rules and their heuristic tests are withdrawn. Batch 5 also accounts for the related session-start delta. |
| #11232 — Pass CLAUDECODE=1 explicitly in SDK options to unpin claude-agent-sdk | `6467de53b1` | intentionally superseded | Reinjection contradicts retained #8183 **Prevent nested SDK sessions**: E23 strips CLAUDECODE and E3 overrides only auto-memory. #20654 **MCP 2 migration** selects SDK >=0.2.144 (E24), replacing the obsolete workaround/version target. Independent llama-cpp lock/core changes are withdrawn by #11321 **LiteLLM/llama-cpp drawdown**. No restart/provider success is asserted. |
| #11231 — Pin claude-agent-sdk<=0.1.45 and investigate 0.1.53 breakage | `7829ab5193` | intentionally superseded | E24 and #20654 **MCP 2 migration** explicitly require SDK >=0.2.144; the old upper cap conflicts. Independent llama-cpp optional-to-core movement is withdrawn by #11321 **LiteLLM/llama-cpp drawdown**. Historical install/restart criteria do not establish current activation. |
| #11225 — Remove dead executor layer, streaming code, and UI remnants | `f6d3e84b4f` | delivered | #11286 **Dead validation tools**, #11287 **Executor removal**, #11288 **in_process removal**, #11289 **Streaming removal** and #11988 **Dead web-terminal plumbing** carry cleanup. E25–E27 retain tracking-only AgentRunner, startup without executors and the decomposed editor. Complete production-scope checks find no ExecutorRegistry, EmbeddedPTYResult, stream_with_mcp_tools or run_fix_attempt/validate_and_fix. Temporary App agents stubbing is withdrawn by #11988's activity-panel ownership. LiteLLM utilities are later retired by #11319/#11321 **LiteLLM drawdown**. Independent tests/UI changes are accounted below. |
| #11224 — Disable codex-review-gate in template and database | `24c4d53679` | intentionally superseded | Landed 3c2ab4dcd6 deprecates the Codex templates; #11301 **Mode/template cleanup** explicitly deletes both agents and the review gate. Gate defaults disappear with source retirement. No installed DB state or historical one-off DB update is asserted; templates do not establish activation. |
| #11223 — Fix Neo4j auth: resolve $secret reference before passing to Docker | `44efe20794` | intentionally superseded | #14630 **FalkorDB compose**, #14641 **Neo4j runtime sweep** and #14660 **Stale Neo4j config cleanup** withdraw the Neo4j password contract. E22/E30 follow managed startup and resolve FalkorDB secrets before Docker environment construction. Independent SDK ghost-hook suppression and caller consolidation survive via E1/E3–E4/E16–E19 and landed bea14472/af801058. Helpers/tests moved with provider ownership. |
| #11218 — Implement subagent rule isolation via is_subagent variable | `ef6038c2fb` | intentionally superseded | E10–E15 retain overlap-safe counters, defaults, native tracker exemption and regression source. Orchestrator ruling a970cce7 (2026-10-12) explicitly withdraws blanket project-task blocking and edit/commit exemptions: native children inherit the parent's claim, preserving attribution/gates. E13–E14 match. Memory/error guards have named retirements; web SDK callbacks belong to #11434 **Unified CLI chat** retirement. Rich stop-metadata logging is enhancement under the ruling. Dependency/root-version deltas follow current contracts. No recovery route. |
| #11215 — Remove stale session_id references from prompts, skills, and rules | `b9262c2fe5` | intentionally superseded | E5–E7 and batch 5 E53–E55 retain required explicit variable targeting. E28–E29 document its matching signature. #22275 **Folded skills**, #18880 **Task-guidance consolidation**, #11535 **Lean session start**, #15174 **Completion-gate simplification** and #19408 **Obsolete registry retirement** account for five absent skill/rule paths. The instruction regression follows the required-target contract. |
| #11214 — Remove session_id from stdio proxy tool schemas and wire ContextVar fallback | `62029ef16a` | intentionally superseded | Explicit targeting replaces sticky first-caller/header learning and optional variable schemas. E5–E7 and batch 5 E53–E55 preserve matching signatures/targets/tests. #20192 **Domain HTTP routes** relocates endpoints and retires old tests. Batch 5 E46–E47 retain injection only for required schemas; optional-tool gaps belong to #24130 **Caller-session scoping recovery**. Do not restore sticky targeting. |
| #11201 — Fix codex-rescue agent template mode: self → interactive | `2fe1e8c83c` | intentionally superseded | #11301 **Mode/template cleanup** explicitly deletes codex-rescue after landed deprecation 3c2ab4dcd6. This one-field revision has no independent behavior. No installed-registry activation is asserted. |
| #10399 — Fix messages scrolling under ChatInput after Phase2b flex change | `7ae3b093b3` | intentionally superseded | #10400 **Chat scroller grid fix** explicitly replaces the conflicting flex-col wrapper with grid, recording historical Playwright geometry evidence. E20–E21 retain grid/min-h-0, Virtuoso containment and sibling ChatInput after decomposition. No fresh browser test is asserted. |

Batch totals: **3 delivered, 10 intentionally superseded, 1 missing**.
Final cumulative totals: **37 delivered, 29 intentionally superseded,
23 missing**; **all 89 tasks are classified**. Missing means at least one
unretired independent behavior is absent. Shared revisions remain separate
task/revision rows; recovery is routed to the task owning the missing behavior.

### Batch 6 recovery referral and explicit ruling

#11243 **CLI diagnostic probe** retains linked revisions
`184a1e964258fed862908989d4b9943347af7737` and
`5a20e9e5c91c827e37b30050a34272427f642e7b` in exact audit archives, with no
branch/remote holder. Base probe `9bdb7e188308d0151cb8253854cf375557135fbb`
is not a pin ancestor. The first linked revision adds --verbose; the second
adds --bare hook suppression, independently replaced by headless settings
and provider consolidation. Recovery preserves suppression and avoids probing
planned-shutdown cancellation.

Referral receipts: Inbox `a5cf3d05-15b7-4881-bfa6-84a6b3156267`,
Orchestrator `a98efd2c-3b7b-432a-97da-8cfe997e3ffe`, Lane Manager
`79de6539-a658-4302-bb70-f7be69d0beb7`. A citation correction identifies
E1's drain/join at **249–251**, replacing initial 244–246 references:
`3474c8d3-cc84-4b47-8ba5-f2c181c5fc2f`,
`ef5e8dfb-2080-4985-bc48-e4c3fe701480`,
`44de941b-a688-4920-8d65-c095fd851d7e`. Orchestrator receipt
`72447de6-7365-4096-be93-c12dbb3bfa27` routes #24136 **Recover CLI diagnostic
probe after SDK failure**, queued behind this audit and #24130 **Caller-session
scoping recovery**. Hook suppression and mode docs are outside recovery.

#11218 **Subagent isolation** request `51ab138b-c5f7-40ad-ad1d-d7f0afa56d29`
received ruling `a970cce7-3236-4c70-b49c-81fa32570f72` on 2026-10-12:
native children inherit their parent's claim; edit/commit exemptions would
break attribution/gates. Blanket project-task blocking has no current
requirement. #21892 **Task/handoff discipline** governs spawned workers.
Keep the native tracker guard. Rich metadata logging is enhancement.

### Batch 6 touched-path and replacement audit

Git diff-tree's complete inventory uses rename detection disabled: Codex gate
deprecation has seven old/new paths. All 14 distinct revisions have exact
`refs/audit/24076/<full-SHA>` archives and no branch/remote holders. Archive
retention establishes recoverability, not delivery.

| Task and title | Revision | Paths present at pin | Paths absent at pin |
| --- | --- | --- | --- |
| #11243 — Diagnostic probe | `184a1e9642` | 1 | 1 |
| #11243 — Hook suppression | `5a20e9e5c9` | 1 | 0 |
| #11241 — Mode docs (shared) | `184a1e9642` | 1 | 1 |
| #11242 — Variable kwarg | `a7a0e70f03` | 3 | 1 |
| #11238 — Handoff injection | `121640b3e5` | 1 | 3 |
| #11232 — SDK environment | `6467de53b1` | 2 | 0 |
| #11231 — SDK pin | `7829ab5193` | 1 | 0 |
| #11225 — Dead executors | `f6d3e84b4f` | 21 | 27 |
| #11224 — Codex gate | `24c4d53679` | 1 | 6 |
| #11223 — Neo4j auth/SDK | `44efe20794` | 7 | 3 |
| #11218 — Subagent isolation | `ef6038c2fb` | 9 | 3 |
| #11215 — Session-ID docs | `b9262c2fe5` | 3 | 5 |
| #11214 — ContextVar stdio | `62029ef16a` | 3 | 2 |
| #11201 — Rescue mode | `2fe1e8c83c` | 0 | 1 |
| #10399 — Chat clipping | `7ae3b093b3` | 1 | 0 |

Independent deltas and absent paths:

- #11243 **Diagnostic probe** / #11241 **Mode docs**: agents/SKILL.md is
  explicitly removed by 5c379f5606. Its shared diagnostic hunk remains a
  recovery input. sdk_utils.py's former factory no longer owns generation;
  E1/E3–E4 follow the supported suppression replacement.
- #11242 **Variable kwarg** / #11214 **ContextVar stdio**: old workflows.py
  and route tests become typed session routes. Historical MCP tests adapt to
  matching explicit-target signatures. Sticky header/bootstrap targeting is
  superseded; required-only schema injection survives.
- #11238 **Handoff injection**: two heuristic injection rules and monolithic
  handler tests are retired/decomposed. Its independent session-end fallback
  is withdrawn by the marker contract, rather than inferred from file absence.
- #11232 **SDK environment**, #11231 **SDK pin**, #11218 **Subagent isolation**:
  independent llama-cpp dependency/extra/lock changes are withdrawn by #11321.
  SDK follows #20654. Root lock 0.4.0 is superseded by the 0.5.0 integration
  version (batch 5 E19–E21).
- #11225 **Dead executors**: named landed cleanup removes six implementations,
  streaming, factories, runner execution, fix-loop tools and their tests.
  Registry/spawner paths later become managed terminal/run lifecycle. Tracking,
  queries and cancel remain; relevant agents/CLI scopes contain no in_process
  mode. Provider validation survives until typed/provider redesign. LiteLLM
  model/environment utilities are intentionally withdrawn by #11319/#11321.
  Non-string workflow-model mocks retain their prior assertion; only comments
  change. #16308 **Dead resolver removal** explicitly deletes that resolver
  and its tests (6b7c44f296). E31–E32 retain provider settings/form types
  without embedded execution or an execution mode field. #11988 explicitly withdraws App's temporary agents stub
  and keeps management in the activity panel. Embedded editor/color options
  and old Terminal copy belong to retired mode/terminal surfaces. Remaining
  tests adapt autonomous/interactive fixtures, tracking-only setup and
  completion status; executor/fix-loop/streaming tests follow removed APIs.
  The one-line skill-file fixture deletion is whitespace.
- #11224 **Codex gate** / #11201 **Rescue mode**: #11301 explicitly deletes
  both old/deprecated agents and gate paths. Gate-default removal matches
  retirement; the historical database update is unproven and unclaimed.
- #11223 **Neo4j auth/SDK**: hook dispatcher suppression moves before launch
  into headless settings. Summarizer/tests move under utils via #13908 **Tool
  summarizer relocation**. E18–E19 preserve feature calls, prompt paths and
  fallback; E16–E17 preserve import generation after bounded HTTP fetching.
  bea14472/af801058 consolidate provider text/summary/vision/MCP callers and
  adapt their mocks. The old factory/helper names are not required. Neo4j
  password parsing/.env/compose arguments are replaced by FalkorDB's secret
  contract, rather than copied to an absent service.
- #11218 **Subagent isolation**: #11309/#11311 deliver counters/default/native
  tracker behavior, strengthened by #16252 **Overlapping subagents**. Memory
  and error guards have named retirements. Absent edit/commit exemptions,
  blanket task blocking and richer logging have the explicit ruling above.
  SDK web callbacks are retired by #11434 **Unified CLI chat**.
- #11215 **Session-ID docs**: expand/task-transitions are folded; session-start
  error policy, memory-review and error-triage guards have named retirements.
  Surviving prompt/fallback examples follow required targeting.
- #10399 **Chat clipping**: its only wrapper change has named #10400 grid
  replacement, retained after ChatPage decomposition.

### Batch 6 replacement and retirement ancestors

All 35 full OIDs are ancestors of the fixed evidence pin. They establish
replacement or explicit retirement history independently of archive retention.
The diagnostic base probe is separately checked as a non-ancestor.

| Decision or replacement | Ancestor |
| --- | --- |
| #11301 — Mode/template cleanup | `5e83fcd400df54cad0b534ed477fdd1b4e635991` |
| #11321 — LiteLLM/llama-cpp drawdown | `d985e44c7311af0ddd80854f78fd872922fbe881` |
| #14641 — Neo4j runtime sweep | `38f3e8f37cafdc2fd5fff404f39a2aca9b0367b7` |
| #14660 — Stale Neo4j cleanup | `d833c9fa0e03c8b1c542ddbaa22b9b1644da0fcb` |
| #14630 — FalkorDB compose | `e552de756e4c9e73a556e4cda042e4b97b75e7e4` |
| Codex/Neo4j deprecation | `3c2ab4dcd6f4d64f98b52105c457e882aa0e82f4` |
| #11287 — Executor removal | `a686d7923c75f2fcca2e18ab968d7e1024ebc4dd` |
| #11289 — Streaming removal | `c6005ccab4aaaba672dc167626aa377d0695989e` |
| #11286 — Dead validation tools | `89c5ae833e7384006e4c843466879863a98c88eb` |
| #11288 — in_process removal | `a525b4d89fc346d884af236c0f06004beb57d6fb` |
| #11988 — Dead web-terminal plumbing | `070a204e5518f96180e86639f0676f6e1db4a7bb` |
| Headless hook settings | `bea14472c265b0871988568e359a69a7a5c0b8a2` |
| Provider consolidation | `af801058350226fde20f892580cac3e31f10252d` |
| #11283 — Consistent SDK diagnostics | `0b2a015afd7213681c5120cf93e849df9af0cac1` |
| #11241 — Mode docs equivalent | `404e434428d31e98792431a25934f6c747a12624` |
| Obsolete agents skill removal | `5c379f56062996e773e52d27d4e13ff210192261` |
| #8183 — Prevent nested SDK sessions | `136be00bb566289a2355eef9fa2001507e5d5712` |
| #11319 — LiteLLM drawdown | `429e535d71b72f9f702d9fb2c8308fc20d397f64` |
| #11309 — Subagent toggle | `b32855726111a3fa66618902e6bf2abbaa641c74` |
| #11311 — Native tracker guards | `6c82af66c8e3a8de524f46370a42014f0aa31b46` |
| #16252 — Overlapping subagents | `7a795dcfd3d6b21afc1521eecc2994efbfd8f568` |
| #21892 — Task/handoff discipline | `4f4090edc3aae5f0a634351b624b0cf64ed665cd` |
| #22439 — Native tracker retirement | `63733e703c3472f4ec3a0d94aca2e540d67130ee` |
| #15174 — Completion-gate simplification | `6178d41474cd22d50abf97333d37d3f59498046e` |
| #19408 — Obsolete registry retirement | `66dbca284ce8d815deca324548a2118f1a67f731` |
| #20192 — Domain HTTP routes | `99bfc9e2ae3f5cdbf91d6df89e75a806cf9d61e5` |
| #18880 — Task-guidance consolidation | `87a1a71fc3f168e816c6ef8dca493fcd73ec1d49` |
| #22275 — Folded skills | `9acef616df8bdb8e31458d8f88847a0ec48e61ce` |
| #11535 — Lean session start | `72b2d244bf26fe3e7da84bfa0057d05bd1b0c8e0` |
| #10400 — Chat scroller grid fix | `d06ebcbcc12ec7b6b235922f67de54d639a1371f` |
| #13908 — Tool summarizer relocation | `f488c59fc1b4925f44fe40132290354f6c405577` |
| #16308 — Dead resolver removal | `6b7c44f296f12dc6d72f21df5920a2d4d716dead` |
| #21140 — Structured session handoffs | `5694253eb6232fc0cae9dc6a27521b176dea98d5` |
| #11434 — Unified CLI chat | `fbaab4ca2bc8f3a16fe3785e9f2cfff656e35bde` |
| #20654 — MCP 2 migration | `eff0bf72e6d95cbb44811c970fea7a0ec091b8ee` |

### Batch 6 deterministic evidence ledger

Complete native reads used the fixed binding. Hashes cover raw UTF-8 excerpts;
EOF-clamped ranges below give actual bounds. Test citations show retained
source, not a fresh runtime pass. Previous-batch evidence IDs explicitly name
their batch. Key returned lines include E1 `249| await asyncio.sleep(0.2)`
and `251| stderr_text = "\n".join(stderr_lines)`; E23
`104| os.environ.pop("CLAUDECODE", None)`; E24
`36| "claude-agent-sdk>=0.2.144"`; E20
`157| <div className="grid min-h-0 flex-1">` and sibling ChatInput at 208–209.

| Evidence | Bound path | Lines | excerpt_hash |
| --- | --- | --- | --- |
| E1 | src/gobby/llm/claude_runtime.py | 167–263 | `1c4c6e0aab90dc6867a01cfd636e073765a69f9d4e7996bafc5450479993dfdd` |
| E2 | src/gobby/llm/claude_cli.py | 1–90 | `9f3b97a0596b39a8c8ae972b596d6ef909df9497a1973557e4b1efadb9538ab2` |
| E3 | src/gobby/llm/claude_sdk.py | 44–105 | `cf4c947a66230dc2c7f5a02a071db60073e6d9a92a1a942ade3d1c612875a4c2` |
| E4 | src/gobby/runner_init/helpers.py | 48–77 | `c0c710b5874bef01ec0e82f51b302f35cabe577b2647fa9ccf736a1cc95bb3f9` |
| E5 | src/gobby/mcp_proxy/tools/workflows/_variables.py | 1–140 | `9ecd81a1aa866f492583e0089d9c6e0a2692bc9b248715ba353dfe22c45fb51b` |
| E6 | src/gobby/mcp_proxy/server.py | 546–609 | `be3af544ab068e4c9b9b784288ccb4922ed42f4f3e34ac9d75261aaeae2ee2f6` |
| E7 | src/gobby/servers/routes/sessions/variables.py | 63–133 | `2da246cf1200ed727732e6ff036f1e3401fa295d60eba265e9c657bb5e489cf3` |
| E8 | src/gobby/hooks/event_handlers/_session_end.py | 58–115 | `1f179ad9b30902f7c0841c07b07e0ee95f353c663cf3f7b7df995549bb741bd4` |
| E9 | src/gobby/hooks/event_handlers/_session_end.py | 185–236 | `03294c4d58bafc0b048150c2c22f5fde6e72e9c1afd3e4129d09fe21f8a922d4` |
| E10 | src/gobby/hooks/event_handlers/_agent.py | 698–785 | `e70799c19773458ce10bd8ac1cc4c5f31c7627d1f0f8f047cdebec27da2b3920` |
| E11 | src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml | 87–99 | `40f43cc9d695cbe59483ee6f3ca99fe44bb41b14a6619a587e5045f1e7a72aa9` |
| E12 | src/gobby/install/shared/workflows/rules/task-enforcement/block-native-task-tools.yaml | 1–32 | `1be0eb709dba6c41a4e0586d0f4b04ab8ad3a803e81da5d3f304d1282e38798f` |
| E13 | src/gobby/install/shared/workflows/rules/task-enforcement/require-task-before-edit.yaml | 1–44 | `37d46aac715a7f7dc2ef64f8dd702fa775acdbb6776223f64b7f6993af35181b` |
| E14 | src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml | 1–30 | `faec1a0572a821ce240140657919fe59c0d574c1934ca030b7df1ba1f67f1c21` |
| E15 | tests/workflows/test_subagent_rule_integration.py | 79–163 | `64e1858b96a1732715fd8c5acb6550e262a9c65854ec1a308502ff1401b6ddb7` |
| E16 | src/gobby/mcp_proxy/importer.py | 205–246 | `1ffeb278eb58a5d462c5c21c187fc6b32f90e4e2337bd770ab9e3d7621e34b7d` |
| E17 | src/gobby/mcp_proxy/importer.py | 266–306 | `e463d6385e835c6d70a0528ba1bb800458543cd98e2a771369789cc238be6d21` |
| E18 | src/gobby/utils/tool_summarizer.py | 62–102 | `b015ef87efb26bf9f49e4c646f8015e81a0c33cd0ea6c17199895fc90d103036` |
| E19 | src/gobby/utils/tool_summarizer.py | 154–197 | `86651728a9719d1ccaea49cb8d17b2021d4dd02955b74b19a6ebd6f26b7639ba` |
| E20 | web/src/components/chat/ChatMainColumn.tsx | 140–215 | `c4c5071fc3938753fa7cc425a1548e7d2923e6a8cd94f8cb3dd3153917189a37` |
| E21 | web/src/components/chat/MessageList.tsx | 375–413 | `a5da905740c552c8ca72a3803c62e49d78c3408afd38f17a3e58aa9d6788efa0` |
| E22 | src/gobby/cli/daemon.py | 1–18 | `fafe065ab00d5ba70bf31ee1c1bd277470fb7e435d62bb2490c4d5314fb653ce` |
| E23 | src/gobby/runner.py | 95–107 | `2245fba4a3a29cf0b962da5b0d7fefb758aefb2560034480659145454d0de125` |
| E24 | pyproject.toml | 18–86 | `ff9c538455f7f748dfc209eab4d602581495de13938206b389d172fc5c584ca8` |
| E25 | src/gobby/agents/runner.py | 35–82 | `2741859fe5a3ff502a417c44a82d2222218d8f1fdf7cbcc23dccd68914b90996` |
| E26 | src/gobby/runner_init/orchestration.py | 486–500 | `ee8769e44786e741ce49898dc279461faabe9a3be5f55cffe49436b89a29228d` |
| E27 | web/src/components/agents/AgentEditForm.tsx | 140–220 | `766c14d165ee37a8ab5c364be5714a134d4467b53195241043a3c8747520dc21` |
| E28 | src/gobby/install/shared/prompts/mcp/progressive-discovery.md | 64–82 | `43278ed8fed96d49895338d7cc2326438e00aa26b230fec4b16376d13216a3c6` |
| E29 | src/gobby/mcp_proxy/instructions.py | 71–89 | `43278ed8fed96d49895338d7cc2326438e00aa26b230fec4b16376d13216a3c6` |
| E30 | src/gobby/cli/installers/compose_env.py | 147–222 | `86166bf08195843700dbb1adf9f382d689f977858b0b68c16aaa86a5696e01dc` |
| E31 | web/src/components/agents/AgentProviderSettings.tsx | 18–51 | `dff0a6fa1b00aa72573dbf8a7c3db8337bebbaf070cde092ff0259fe485c4dcc` |
| E32 | web/src/components/agents/AgentEditForm.types.ts | 1–65 | `ecfd96fad4a7fa77bbd1ab18a4b5fca81cabc0a9c60d0a7ccf33acd6126c1258` |

### Batch 6 reproducible validation

This audit reconstructs the 89-task inventory from the report's original
table and per-commit proofs; it reads no ignored cache. It also reruns the
relevant batch 3–5 hash, inventory, ancestor and semantic checks, bounding
each addendum before parsing. Run from this worktree:

```bash
uv run python - <<'PY'
# batch-6-audit
import hashlib
import re
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path

PIN = "4ccb3e966ed18fe13cd9f268d55143128d034048"
REPORT = Path(".gobby/plans/research/closed-unlanded-audit-2026-10.md").read_text()
EXCLUDE = {23201, 23252, 23304, 23345, 23519, 23626, 23269}
Q = chr(96)

def git(*args):
    return subprocess.check_output(["/usr/bin/git", *args])

@lru_cache
def source(path):
    return git("show", f"{PIN}:{path}").decode()

pin_paths = set(git("ls-tree", "-r", "--name-only", PIN).decode().splitlines())
pin_history = set(git("rev-list", PIN).decode().splitlines())

def exists(path):
    return path in pin_paths

def ancestor(oid):
    assert oid in pin_history, oid

# One topological graph walk proves exact branch/remote containment.
refs = [line.split(" ", 1) for line in git(
    "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/remotes"
).decode().splitlines()]
reach = {}
for index, (oid, _) in enumerate(refs):
    reach[oid] = reach.get(oid, 0) | (1 << index)
graph = subprocess.check_output(
    ["/usr/bin/git", "rev-list", "--topo-order", "--parents", "--stdin"],
    input=("\n".join(oid for oid, _ in refs)+"\n").encode(),
).decode().splitlines()
holders_by_oid = {}
for line in graph:
    oid, *parents = line.split()
    bits = reach.pop(oid, 0)
    holders_by_oid[oid] = bits
    for parent in parents:
        reach[parent] = reach.get(parent, 0) | bits

def holders_for(oid):
    bits = holders_by_oid.get(oid, 0)
    return [name for index, (_, name) in enumerate(refs) if bits & (1 << index)]

original = REPORT.split("## Every closed task with an unlanded commit\n", 1)[1].split("\n## Per-commit", 1)[0]
proof = REPORT.split("## Per-commit supersession and recovery evidence", 1)[1].split("## Every closed task", 1)[0]
unproven = {
    oid for oid, body in re.findall(r"### \x60([0-9a-f]{40})\x60\n(.*?)(?=\n### \x60|\Z)", proof, re.S)
    if "\nMissing linked revision, semantic supersession not proven." in body
}
inventory = []
for line in original.splitlines():
    if not line.startswith("| #") or not line.endswith("Missing linked revision, semantic supersession not proven |"):
        continue
    columns = line.strip("| ").split(" | ")
    number = int(re.match(r"#(\d+)", columns[0])[1])
    oids = [oid for oid in re.findall(r"\x60([0-9a-f]{40})\x60", columns[2]) if oid in unproven]
    if oids and number not in EXCLUDE:
        inventory.append((columns[1], number, oids))
inventory.sort(key=lambda row: (row[0] or "", row[1]), reverse=True)
assert len(inventory) == 89

sections = {}
all_rows = []
for n in range(1, 7):
    section = REPORT.split(f"## Semantic recovery review: batch {n}", 1)[1]
    section = re.split(r"\n## Semantic recovery review:", section, maxsplit=1)[0]
    sections[n] = section
    table = section.split(f"### Batch {n} classifications", 1)[1].split("Batch totals:", 1)[0]
    rows = []
    for line in table.splitlines():
        if line.startswith("| #"):
            columns = line.strip("| ").split(" | ")
            rows.append((int(re.match(r"#(\d+)", columns[0])[1]),
                         re.findall(r"\x60([0-9a-f]+)\x60", columns[1]),
                         columns[2]))
    expected = inventory[(n-1)*15:min(n*15, 89)]
    assert [(number, prefixes[0]) for number, prefixes, _ in rows] == [
        (number, oids[0][:10]) for _, number, oids in expected
    ], n
    if n == 6:
        assert [prefixes for _, prefixes, _ in rows] == [[oid[:10] for oid in oids] for _, _, oids in expected]
    all_rows.extend(rows)
assert len(all_rows) == len({row[0] for row in all_rows}) == 89
assert Counter(row[2] for row in all_rows) == {
    "delivered": 37, "intentionally superseded": 29, "missing": 23,
}
batch = sections[6]
assert [row[0] for row in all_rows[75:]] == [
    11243,11241,11242,11238,11232,11231,11225,11224,11223,11218,11215,11214,11201,10399,
]
assert Counter(row[2] for row in all_rows[75:]) == {
    "delivered": 3, "intentionally superseded": 10, "missing": 1,
}

hash_counts = {3:16, 4:40, 5:59, 6:32}
for n, count in hash_counts.items():
    hashes = re.findall(r"\| [CDE]\d+ \| \x60?([^\x60 |]+)\x60? \| (\d+)–(\d+) \| \x60([0-9a-f]{64})\x60", sections[n])
    assert len(hashes) == count, (n, len(hashes))
    for path, start, end, expected in hashes:
        excerpt = b"".join(git("show", f"{PIN}:{path}").splitlines(keepends=True)[int(start)-1:int(end)])
        assert hashlib.sha256(excerpt).hexdigest() == expected, (n, path)

release_refs = {
    17462: ["refs/heads/0.4.44"], 17461: ["refs/heads/0.4.43"],
    15711: ["refs/heads/0.4.18"],
    15562: ["refs/heads/0.4.15", "refs/remotes/origin/0.4.15"],
    15329: ["refs/heads/0.4.12", "refs/remotes/origin/0.4.12"],
    15293: ["refs/heads/0.4.11", "refs/remotes/origin/0.4.11"],
    15296: ["refs/heads/0.4.11"],
}
for n, count in ((3,15),(4,15),(5,16),(6,15)):
    path_rows = re.findall(r"\| #(\d+) — .*? \| \x60([0-9a-f]+)\x60 \| (\d+) \| (\d+) \|", sections[n])
    expected = [(number, oid) for _, number, oids in inventory[(n-1)*15:min(n*15,89)] for oid in oids]
    assert len(path_rows) == len(expected) == count, n
    for (number, prefix, present, absent), (task, oid) in zip(path_rows, expected, strict=True):
        assert int(number) == task and prefix == oid[:10]
        paths = git("diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", oid).decode().splitlines()
        found = sum(exists(path) for path in paths)
        assert (found, len(paths)-found) == (int(present),int(absent)), (task,oid)
        holders = holders_for(oid)
        assert holders == release_refs.get(task, []), (task,holders)
        if not holders:
            assert git("rev-parse", f"refs/audit/24076/{oid}").decode().strip() == oid
assert len({oid for _, _, oids in inventory[75:] for oid in oids}) == 14
ancestor_text = batch.split("### Batch 6 replacement and retirement ancestors",1)[1].split("### Batch 6 deterministic evidence ledger",1)[0]
ancestors = re.findall(r"\x60([0-9a-f]{40})\x60",ancestor_text)
assert len(ancestors) == len(set(ancestors)) == 35
for oid in ancestors:
    ancestor(oid)
assert "9bdb7e188308d0151cb8253854cf375557135fbb" not in pin_history
for receipt in (
    "a5cf3d05-15b7-4881-bfa6-84a6b3156267","a98efd2c-3b7b-432a-97da-8cfe997e3ffe",
    "79de6539-a658-4302-bb70-f7be69d0beb7","72447de6-7365-4096-be93-c12dbb3bfa27",
    "a970cce7-3236-4c70-b49c-81fa32570f72","3474c8d3-cc84-4b47-8ba5-f2c181c5fc2f",
    "ef5e8dfb-2080-4985-bc48-e4c3fe701480","44de941b-a688-4920-8d65-c095fd851d7e",
):
    assert receipt in batch
assert "#24136" in batch and "249–251" in batch

# Retained contracts and specifically absent diagnostic implementation.
runtime = source("src/gobby/llm/claude_runtime.py")
assert 'options.stderr = lambda line: stderr_lines.append(line)' in runtime
assert r'stderr_text = "\n".join(stderr_lines)' in runtime
assert 'options.settings = str(_HEADLESS_SETTINGS)' in runtime
assert "with fixed_textgen_cwd() as neutral_cwd:" in source("src/gobby/llm/claude_sdk.py")
for path in ("src/gobby/llm/claude_runtime.py","src/gobby/llm/claude_cli.py","src/gobby/llm/claude_sdk.py"):
    assert not re.search(r"create_subprocess|subprocess\.(run|Popen)|_probe_cli", source(path))
helpers = source("src/gobby/runner_init/helpers.py")
for hook in ("SessionStart","SessionEnd","UserPromptSubmit","PreToolUse","PostToolUse","PreCompact","Stop","SubagentStart","SubagentStop","PermissionRequest"):
    assert f'"{hook}": []' in helpers
assert 'os.environ.pop("CLAUDECODE", None)' in source("src/gobby/runner.py")
assert '"claude-agent-sdk>=0.2.144"' in source("pyproject.toml")
assert not re.search(r"(?m)^  mode:", source("web/src/components/agents/AgentEditForm.types.ts"))
for path in ("web/src/components/agents/AgentProviderSettings.tsx","web/src/components/agents/AgentEditForm.types.ts"):
    assert "embedded" not in source(path)
variables = source("src/gobby/mcp_proxy/tools/workflows/_variables.py")
assert "session_id: str," in variables and "resolved_session_id" in variables
assert "session_id=session_id" in source("src/gobby/mcp_proxy/server.py")
assert "session_id=" in source("src/gobby/servers/routes/sessions/variables.py")
assert "adjust_counter_and_derive_boolean" in source("src/gobby/hooks/event_handlers/_agent.py")
assert "  is_subagent:\n    value: false" in source("src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml")
assert "is_subagent" in source("src/gobby/install/shared/workflows/rules/task-enforcement/block-native-task-tools.yaml")
for path in ("require-task-before-edit.yaml","require-commit-before-status.yaml"):
    assert "is_subagent" not in source("src/gobby/install/shared/workflows/rules/task-enforcement/"+path)
for path in ("src/gobby/mcp_proxy/importer.py","src/gobby/utils/tool_summarizer.py"):
    assert "call_feature" in source(path)
assert 'className="grid min-h-0 flex-1"' in source("web/src/components/chat/ChatMainColumn.tsx")
assert "<ChatInput" in source("web/src/components/chat/ChatMainColumn.tsx")
assert "Virtuoso" in source("web/src/components/chat/MessageList.tsx")
for pattern, paths in (
    ("ExecutorRegistry|EmbeddedPTYResult|stream_with_mcp_tools|run_fix_attempt|validate_and_fix",["src/gobby","web/src"]),
    ("in_process",["src/gobby/agents","src/gobby/cli"]),
):
    result = subprocess.run(["/usr/bin/git","grep","-E",pattern,PIN,"--",*paths],capture_output=True)
    assert result.returncode == 1, result.stdout.decode()
for path in ("src/gobby/llm/resolver.py","tests/llm/test_resolver.py"):
    assert not exists(path)

# Prior-batch semantic regressions and ancestor ledgers.
prompt_losses = sections[3].split(Q*3+"text\n",1)[1].split("\n"+Q*3,1)[0].splitlines()
assert len(prompt_losses) == 6
for path in ("src/gobby/install/shared/prompts/handoff/session_end.md","tests/prompts/fixtures/handoff_session_end_golden.md"):
    historical = git("show", f"19ad378d4e65347ec6406b04d83597c57ed2e342:{path}").decode()
    assert all(line in historical and line not in source(path) for line in prompt_losses)
for oid in ("cc82f7a5a395a0fa033c5bdfa8cf1376b4ced824","06112ec3cc40a44b66cb6dde1ceb63590525fc73","95e809ee251d64e742b68032065c241cce34b179","b705b0254841bbcadd3bb0b55e2c8dc5f5ec08ba"):
    ancestor(oid)
for n in (4,5):
    if n == 5:
        text = sections[n].split("### Batch 5 replacement and retirement ancestors",1)[1].split("### Batch 5 deterministic evidence ledger",1)[0]
        oids = re.findall(r"\x60([0-9a-f]{40})\x60",text)
        assert len(oids) == 14
    else:
        text = sections[n].split("ancestors = (",1)[1].split(")",1)[0]
        oids = re.findall(r'"([0-9a-f]{40})"',text)
        assert len(oids) == 18
    for oid in oids:
        ancestor(oid)
for packet in ("06-tasks-plans-review-learning.md","07-workflows-skills-orchestration.md","08-mcp-proxy-hub-services.md","09-servers-sessions-runtime.md"):
    source(".gobby/plans/completed/coderabbit-fixes/"+packet)
plan = source(".gobby/plans/remove-gobby-build.md")
assert "src/gobby/build/workspaces.py::*"+Q+" — operation: delete" in plan
assert "src/gobby/dispatch/spawn.py::*"+Q+" — operation: delete" in plan
for path in ("src/gobby/dispatch/base_guard.py","tests/dispatch/test_dependency_base_guard.py"):
    assert not exists(path)
coverage = source("src/gobby/plans/review_coverage.py")
assert 'return canonical_json_sha256({"plan_hash": plan_hash, "sources": source_hashes})' in coverage
assert coverage.count('json.dumps(attestation, sort_keys=True, separators=(",", ":")).encode()') == 2
assert "[0.4.11]" in git("show","d4cf939a6dd9ef9b0e466b501c505dbfe17979c3:CHANGELOG.md").decode()
assert "[0.4.11]" not in source("CHANGELOG.md")
for receipt in ("e051b26e-4553-486e-966a-aa4a910f44ac","0fca1877-81bd-4528-b67d-6f77c7e65ac0","12c4bfa0-a058-4063-96b7-1eee7b677115"):
    assert receipt in sections[5]
assert "7f719a24-e8a6-4e13-8bb9-ee73adf3cdce" in sections[4]
assert "db116bc8-3028-4307-8307-f5b3f7cd340e" in sections[4]
assert sum(line.startswith(Q*3) for line in REPORT.splitlines()) % 2 == 0
assert REPORT.endswith("\n\n_Last verified: 2026-10-12_\n")
assert not REPORT.endswith("\n\n\n_Last verified: 2026-10-12_\n")
print("PASS: all89 classifications/order/counts; batch6 32 hashes/15 inventories/14 archives/35 ancestors/referrals/ruling; retained contracts and diagnostic absence; prior3/4/5 115 hashes/46 inventories/36 ancestors and semantic regressions; Markdown/footer")

PY
```

Validation passed with the direct `uv run python` audit above: all 89 ordered,
unique classifications and final counts; batch 6's 32 pinned excerpt hashes,
15 path inventories, 14 exact archives with no branch/remote holders,
35 replacement ancestors, referral/ruling receipts, retained contracts and
diagnostic absence. The same run rechecked batch 3–5's 115 hashes,
46 inventories, 36 ancestors, live prompt losses, reconciliation packet
availability, approved retirement, LOW-1 source correction and release scope.
The original sequential containment run was interrupted for speed; the final
passing audit uses one topological branch/remote graph walk and one pinned
path inventory. Native evidence and Git bytes agree. `git diff --check` passes.
No fresh provider, installed-registry, release-publication or browser pass is
claimed. The report is the only changed file.

_Last verified: 2026-10-12_
