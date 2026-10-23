## Research Findings

### Research Questions

- Which open direct children of #24818 remain valid under both v4 documents?
- Which requirements survive the removal of lane relays, transcript proof and activation receipts, including the later epic-team and workspace changes?

### Domain Context

Audit date: 2026-10-23 UTC. Researcher: gobby#17360, claimed research spike #24823. Inventory: `gobby-tasks:list_tasks(parent_task_id="#24818", closed=false, limit=100)` returned **47 open direct children**; every actionable card was read with `get_task`. Task descriptions are historical incident evidence unless corroborated below. No runtime reproduction was performed.

**Counts: KEEP 23; RESCOPE 15; MOOT at 6.1 1; CLOSE now 0. Eight excluded in-flight rows have no verdict. Total: 47 unique children.**

This audit is necessary: removing a workflow mechanism does not establish that a related runtime defect is fixed. KEEP means substantive scope remains valid, not approval to launch secondary Python work now.

Authority shorthand:

- **R**: [runbook-devteam-redesign.md](../runbook-devteam-redesign.md), together with #24797's explicit operator overrides. Main-checkout R is materially stale: five states, the definition deletion list and the old review authority. The reconciled R on `task-23850` at `01399a39f35a407d464abc36750a753b8c8a6fc8` has seven states, protected definitions and Decision 14's role-owned step records. Branch content was read as additional evidence, not treated as landed code.
- **A**: [runbook-devteam-addendum.md](../runbook-devteam-addendum.md), approved at `c5e43f1500`, Decisions 15–27 and §§7.1–9.4. It follows R and adds epic teams, category reviewers, generic grants and scoped workspaces.
- Memory `a68f8994-da4a-5be5-afdd-c1823918e679`: single unexpanded runbook/task-system tasks; secondary Python work waits; moot-once-v4 rows close together at R §6.1; the earlier automatic-clear ruling includes researcher/Admin seats.
- Memory `c3fa8eb8-8fab-5a9e-8366-3bb0963d1110`: v4, then working/tested 1.0 hub/node, then the remaining critical-path Python-to-Rust work. Validity and priority are separate.
- Memory `8ccd54f8-f846-53ed-8234-ae6eeb6af416`, applied explicitly in #24797: agents used by runbooks stay. Preserve log-monitor, metrics-monitor, recovery-steward, task-close-reviewer, expansion-qa and current planning roles. Retiring automatic close-review runtime does not authorize deleting its protected definition.

The current `project_policy` was read; it still has lane defaults and an older cap. Reconciled R Decision 9 removes heavy-slot grants/caps and centrally sets per-process Cargo jobs to 4. Existing teams work through release windows; new-team admission and landing wait. A adds epic/leaf worktrees with title slugs, parallel leaf claims, merge-only history, end-of-epic review and batch close. Neither source authorizes losing another session's edits.

### Evidence & Sources

“Card” below means the full PostgreSQL task description and criteria read through MCP. E-identifiers name bounded gcode evidence listed after the inventory. Each RESCOPE proposal is one or two sentences. No task records were edited.

| Task | Short title | Verdict | Reason / replacement scope | Evidence or v4 section |
| --- | --- | --- | --- | --- |
| #23902 | Cargo and test-hub load | RESCOPE | Retain measured host/test-hub efficiency research using v4's shared target and central jobs=4 as the baseline. Remove old grant/cap assumptions, avoid duplicating the cache implementation and coordinate comparative load windows. | R Decisions 9–11, §5.1 explicitly retain wider performance ownership here; card assumes jobs=3, per-tree caches and older caps. |
| #24590 | Later-claim path attribution | RESCOPE | Keep shared live-interval attribution and current dirty-edit remedies. Drop the folded gate-10 transcript/tree-credit refusal requirement. | R §§1.1, 1.4 retain attribution but remove validation-credit gates; E03; card's A-to-B ownership failure. |
| #24720 | Multiline consensus latch | KEEP | Planning roles survive and the whole-message regex defect remains visible. | E01; R Decision 12 as overridden; A §7.1 leaves Plan Adversary behavior name-keyed. |
| #24722 | Validate consumed message envelopes | RESCOPE | Validate only envelopes still consumed by surviving seat transitions, sharing the receiver grammar. Remove retired parking/resume and reviewer-landing requirements rather than reconstructing authority beside step records. | R §§2.3–2.4, 3.2; reconciled §2.4 explicitly retires park/resume grammar; A §7.1; E01. |
| #24725 | Handoff delivery and saturation | RESCOPE | Keep false composer occupancy, stuck staged delivery, bounded consumption and coherent stale-marker deadlines. Replace the blanket fleet-hold scenario with existing-team reconnect/compaction and isolated concurrent consumers. | R §§4.1–4.2 remove fleet drain, not handoff delivery; A §9.3; card's four defects. |
| #24766 | Catastrophic regex / projection failure | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #24797 | Implement v4 redesign | — | In-flight exclusion; no verdict. Its explicit operator overrides inform other rows. | Explicit research brief. |
| #24813 | Pre-stop start proof | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #24816 | Re-handshake after principal drain | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #24416 | Re-adopt resumed pane | KEEP | V4 still resumes/replaces seats; a stale pane binding remains a defect. | R §§3.1, 3.3, 4.1; A §§8.2, 9.3; card's pane/live-terminal mismatch. |
| #24419 | External gcode read escape | KEEP | External source remains outside the index; workspace scope is a separate boundary. | A §9.2; card's `invalid_path_scope` reproduction and promised escape. |
| #24493 | Foreign writes during Git operations | KEEP | V4 depends on merges that preserve live foreign edits. The operation-start ownership planning question survives even though epic branches never rebase. | E03; R §2.4; A Decisions 19, 24 / §§8.2, 8.5. Planning scope remains; no implementation decision is invented. |
| #24618 | Slow restart/stop probe | KEEP | Both decisions still use the one-second health helper; Release Manager ownership does not fix load sensitivity. | E14; R §4.2 reuses lifecycle primitives. |
| #24623 | Release history and close-proof leftovers | RESCOPE | Retain prior-claim audit/independence history after verifying the surviving consumer. Drop deleted close-presence/docstring/proof tests and update ambiguous release wording only where it survives R §3.5. | E12; R §§1.3–1.4 retire proof consumers, §§2.3–2.4 retain former-claimant exclusion; A Decisions 16, 21. |
| #24640 | Refused fast-forward dirties checkout | KEEP | Protected-branch refusal must precede checkout mutation under Dev Manager landing too. | R §2.4 retains transactional landing safety; A Decision 19 requires clean merge input; card records unchanged HEAD with rewritten index/tree. |
| #24643 | Expired wait reused | KEEP | Event-driven waits survive; the existing-row query checks waiting outcome without expiry. | E02; R §3.4; A §§8.2–8.5. |
| #24728 | Intermediate blobs in task diff | KEEP | Review still requires final leaf/epic content and correct exclusion of imported integration changes. | E06 locates per-commit streaming; R §§2.3–2.4; A Decisions 19, 21, 24. Reported wrong blobs were not reproduced. |
| #24730 | Wrong ambient claim session | KEEP | Actual caller identity remains essential for leaf claims, grants and attribution. | R §§2.3, 3.1; A Decision 16 / §8.1; card's omitted/explicit-session repro. E11 corroborates a related seam, not this exact race. |
| #24732 | Task paging skips/elides rows | RESCOPE | Prove complete stable paging for surviving filters, compact/default views, large rows and refs batches. Keep candidate-event filter coverage only while that filter remains; retire its tests with its consumer otherwise. | R §§2.2–2.4 change task/review surfaces; E13; card contains both paging and output-budget cases. |
| #24733 | Transient status / stale previews | RESCOPE | Keep settled-status waits and current session previews. Bind freshness to current tasks, step records and landing events rather than retired readiness/activation-receipt vocabulary. | R §§2.2–2.4, 3.3; A §§8.2–8.4; E02; card's two defects. |
| #24742 | Partial WORKTREE_CREATE succeeds | KEEP | Isolated task/epic trees still require correct records, sidecars and hooks; failures still log and return success. | E05; R §3.1; A §§8.1–8.2. |
| #24750 | Wait notice omits owner | KEEP | Managers/Admin still need to map waits to owners; rendering omits owner metadata. | E04; R §3.4; A §§8.2–8.5. |
| #24780 | Failing CI / release workflows | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #24792 | Script-write attribution | RESCOPE | Keep claimed repository script-write attribution, preserving existing foreign owners and excluding external workspace writes. Leave approval-parser false positives to #24804. | R §1.1 retains dirty-edit invariant; A §9.2 deliberately exempts literal workspace writes from the ledger; card's incidents; E03. |
| #24794 | Researcher report commit/close | RESCOPE | Reconcile standalone report submission with role-owned implementation, research review and closing, proving main-checkout report commit plus an authorized closer without a helper worktree. Preserve work-bearing commits and role grants rather than exempting research from evidence. | E15 already allows plan Markdown direct commits; E16; R §2.3, A §7.2. This session's authorized commit does not prove generic researcher close. |
| #24804 | Approval guard false positives | KEEP | Dirty-path approval remains operator-only; harmless operands, expansions and heredoc bodies must remain usable. | Card's multiple concrete false positives; A §9.2 retains opaque-script claim enforcement. Coordinate parser ownership with excluded #24766. |
| #24806 | Reused tree rebases linked history | KEEP | Replacement/resume survives; exact linked SHAs must remain reachable. Reuse still invokes rebase. | E07; R §§2.3–2.4, 3.1; A Decision 19 / §8.5 prohibits rewriting linked history. |
| #24811 | Cross-worktree gcode guidance | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #24814 | Qdrant checksum speed | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #24823 | This validity research | — | In-flight exclusion; no verdict. | Explicit research brief. |
| #22660 | Clear and restore between tasks | RESCOPE | Apply automatic task-boundary clear to seats that actually persist across tasks, reconciling the researcher/Admin ruling; use teardown for single-task teams and retain epic primaries through their epic. Preserve authorized successor role restoration/messages and A §9.3 workspace inheritance. | R Decisions 2, 12 / §3.3; A Decisions 17, 27 / §§8.2, 9.3; memory a68f8994. Workspace persistence alone does not decide standalone seat lifetime. |
| #22819 | Atomic description append | KEEP | Atomic small amendments still help the Orchestrator; step records do not replace descriptions. | A Decision 21 reserves task edits to Orchestrator; card specifies existing validation and no patch language. Valid secondary enhancement, not a v4 blocker. |
| #22926 | Temporary notes outside checkout | RESCOPE | Use A's session/epic workspace paths and read boundaries for scratch guidance, preserving tracked deliverables in repository scope. Drop old gate-8/PD wording and avoid a second tmp-location mechanism where the workspace contract solves it. | A Decision 27 / §§9.1–9.4; R §§1.1, 1.4. Addendum delivery is later than R §6.1, so this is not moot at that cutover. |
| #23615 | Rollback hides failed removal | KEEP | Safe cleanup remains essential; rollback still ignores the returned deletion result. | E08; R §3.3; A §8.2 preserves dirty/live/unmerged refusals; absorbed diagnostics remain valid. |
| #23818 | Held compact bypasses settle | KEEP | Admin/epic seats still compact, and held commands still bypass settle/interrupt conditions. | E09; A §9.3 retains compaction/resume. No current provider-version or live-repro claim is made. |
| #23895 | Conversation re-ID loses seat | KEEP | Admin/epic primaries still need identity, terminal and mailbox continuity across re-ID. | R §§3.1, 3.3; A §§8.2, 9.3; card's binding loss. Trigger remains uncertain. |
| #24121 | Historical handoff retrieval | KEEP | Attributable, non-consuming handoff reads remain useful for retained research and recovery. Filesystem workspace restrictions do not themselves retire handoff APIs. | R Decision 12; A Decision 27; bounded gcode search found no `get_handoff_record` or `search_handoffs` in `src/gobby/mcp_proxy/tools/sessions/`; card specifies scope/docs. |
| #24130 | Optional-session caller scoping | KEEP | Caller scoping survives generic grants; required-only injection and argument-conditional skill scope remain visible. | E11; R §2.3; A §§7.1–7.2. Preserve explicit targets and the exclusion of skill-list allowlisting. |
| #24132 | Credential rotation race | RESCOPE | After #24816 lands, investigate only residual concurrent rotation/issuance failures, including the recorded lost-binding race. Credit identical cases resolved there instead of building duplicate revoked-generation recovery. | E10; R §4.2 needs reconnect without drain; card's contention race differs from #24816's revoked-binding uniqueness diagnosis. |
| #24169 | Stop gate versus blanket restart hold | MOOT at 6.1 | The get_handoff-only fleet-hold conflict belongs to the retired drain model. Remove its old hold/stop rules at rollout; existing teams continue while landing/new admission wait. | R Decision 9 / §§3.5, 4.1–4.2, 6.1. Handoff consumption remains required. Check operator-recovery rules at reconciliation so a separately retained recovery hold does not silently preserve the conflict. |
| #24197 | Stale landing / overlap / receipt bugs | RESCOPE | Carry changed-card/stale-review, author/team-held work, ancestry-aware overlap and idempotent provenance cases to step records and Dev Manager landing. Drop approval-receipt remedies; test-only activation classification belongs to the pinned release check plan. | R §§2.3–2.4, 4.1–4.2; A Decisions 21–24 / §§8.4–8.5. Absorbed bugs retain safety meaning despite receipt retirement. |
| #24235 | Recover seats after Codex re-login | KEEP | Codex seats and in-window replacement remain; distinct auth classification, one alert and authorized recovery still matter. | R Decisions 5, 9 / §§3.2, 4.1–4.2; A §8.2; exact historical error in card. Current provider behavior was not externally asserted. |
| #24301 | Ended-session decision callback | KEEP | Assistant decision routing survives; callback origin is set directly and excluded from attached fallback. | E17; R Decision 1 / §3.4; card requires a notice, preserving origin-bound decision semantics. |
| #24304 | Reviewer JUnit / scratch failures | RESCOPE | Keep sandbox/output-path and literal-scratch fixes needed for independent reruns, using shared Cargo targets and scoped workspaces. Remove transcript-proof/RM-grant workarounds and confirm the JUnit denial cause before widening writes. | R Decision 3, §§1.2–1.4, 5.1; A §§9.1–9.2; card marks the sandbox cause unconfirmed. |
| #24321 | Inherit provider displayed as Claude | KEEP | Portable reviewer definitions need faithful stored provenance; the rewrite remains present. | E18; A §7.3 explicitly requires `provider: inherit`. |
| #24399 | Safe own-claim release | RESCOPE | Reconcile the existing release surface with team grants for safe maker leaf handoff while the manager keeps the epic claim. Avoid a duplicate tool or unrestricted release of foreign claims. | E19 is coordinator-only; E20 releases on exhausted review, not normal maker handoff; A Decisions 16–17 / §8.5 require leaf release. |
| #24790 | Reporter reference size ceiling | KEEP | Neither v4 document retires Reporter guidance; the bounded reference decomposition remains valid. | R Decision 12 as overridden; card records 19,886-character warning and scoped reference tests. Recheck bytes and installed use at execution; deletion is not inferred from Admin membership. |

#### Repository evidence ledger

Main checkout HEAD: `516a2019409cde7b65704a33488ea6d4adf1a962`. Branch evidence E16/E20: #24797 tree `/Users/josh/.gobby/worktrees/gobby/task-23850`, HEAD `01399a39f35a407d464abc36750a753b8c8a6fc8`. These are observed source excerpts, not installed-rule or runtime assertions. gcode evidence schema 1, tool version 1.9.48. Ranges use observed line numbers.

Reproduce a range using `gcode evidence --request-json '{"schema_version":1,"operation":"read","read":{"kind":"range","path":"<path>","start_line":<start>,"end_line":<end>}}'`. For branch evidence add `--project /Users/josh/.gobby/worktrees/gobby/task-23850` before `evidence`.

| ID | Source and observed fact | Excerpt SHA-256 |
| --- | --- | --- |
| E01 | `src/gobby/workflows/engine/seat_transitions.py:330–377`: consensus regex uses whole message; message consumers remain. | `1e12f86fa4a93c21656ba50954a3f7dded978f5d42f573c1288363cd84186062` |
| E02 | `src/gobby/storage/coordination_waits.py:48–94`: waiting-row reuse lacks expiry predicate; status conditions remain. | `bb95bbb82a32f08cd09ebcc77dcc3fb50f3cc42b1be49fb122a78573cbd853b8` |
| E03 | `src/gobby/workflows/commit_guard.py:282–320`: operation paths removed before owner lookup. | `b686313a2b32abe26e82b7120a68bfa2984dce9b5108348fbd66ea082300efe2` |
| E04 | `src/gobby/hooks/pending_messages.py:116–150`: suffix renders run/task/completion, omits owner session. | `77798be3a0841b8ff43c7d17508b4b5068e6b8c27b7595e5d5052f47bcb3af79` |
| E05 | `src/gobby/hooks/event_handlers/_misc.py:603–651`: record/setup errors log and creation still succeeds. | `9f34dd8d1a33e16aee197055593d088d639906622702ce6720a87fc04517402e` |
| E06 | `src/gobby/tasks/diff_paging.py:477–500`: streams selected commits through `git show`; not proof of the exact bad net blobs. | `38295a65bba6ed6cb819df34d88cfe970182c7cda212db6a26b3f264e2ebe980` |
| E07 | `src/gobby/agents/worktree_reuse.py:112–157`: non-ancestor base triggers rebase. | `393253f72c5a84222dff57e670b391c31ba9566d98d0808bc5ad9b738840c2e9` |
| E08 | `src/gobby/worktrees/creation.py:245–279`: cleanup awaits deletion without inspecting result. | `29fa31c3f47b9c833a7c334c57a3cbfccff10c14202b3514ce5fc20884f7cc06` |
| E09 | `src/gobby/mcp_proxy/tools/sessions/_terminal_compaction.py:620–660`: held command bypasses settle/interrupt guards. | `0f4688c5adba0b415423aa613567501616ea557a3b50d914b1bc290cb822f9b5` |
| E10 | `src/gobby/storage/managed_credentials.py:385–447`: revoked/draining bindings filtered; failed rotation raises named race error. | `581d0aaf18a74cd3f5690c80a8283b88292842ca41e0d21932a379a9bfd41d27` |
| E11 | `gcode grep -n 'required\|session_id' src/gobby/mcp_proxy/services/tool_execution.py -m 25` finds required-only helper at 133–154; `src/gobby/mcp_proxy/tools/skills/search_skills.py:100–124` checks supplied session. | Search-skills range: `5560e0859396f4a8084363828ac71c5a1a6793fb37a2e5147385f1752ee454c4` |
| E12 | `src/gobby/storage/tasks/_transitions.py:255–290`: release clears ownership/projection; no explicit history insert here. Confirm downstream behavior before fixing. | `51bb62776d70c7a76275b926fedba552ecb69cfe4f184db86e67e33cc77047a5` |
| E13 | `gcode grep -n 'candidate\|next_offset\|compact' src/gobby/mcp_proxy/tools/tasks/_listing.py -m 25`: bounding/continuation at 24–130, refs remapping at 194–207. | Search only; no range hash or paging-correctness claim. |
| E14 | `src/gobby/cli/daemon_lifecycle.py:105–118`, `358–392`, `529–558`: one-second helper used without override by both decisions. | Helper `e95c3790feb790747a31cb5f26d315f7143a9867f26ca8d3943adfeded46e8f4`; stop `9da9e7e6c984ebf994ebd326b5549b0fc6f194fddca63b0201f19ff60518abc2`; restart `e077f46cc5b02f03e294e8db1f824f3429aa6b0cadf169a36919ecf1d104e5c5` |
| E15 | `src/gobby/tasks/landing_policy.py:10–38`, `80–115`: `.gobby/plans/` Markdown direct commits allowed; this does not establish researcher close grants. | Prefix `656727b2df758670da96c354064ab2486fe6d92f21a5c7ef679389e92ae142ae`; classifier `01729a8dae4998250db99b13faaad1e820d1931ddc2fa885562a31535f5566b6` |
| E16 | Branch `src/gobby/tasks/step_records.py:215–246`: enabled policy requires implementation/closing for non-epics, plus category-selected test/review. | `c66bb23561b8116cb9bbdb9434be5f12f956f28ea48c2dcd900ec55caf3b7e9c` |
| E17 | `src/gobby/communications/inbound.py:157–218`: direct callback origin, attached fallback excludes callbacks. | `f108e65efb09501c293dc05b7400bd7ab342d80a2aa9aaf9a21230728cd84159` |
| E18 | `src/gobby/mcp_proxy/tools/workflows/_agents.py:119–126`: None/inherit rewritten to Claude. | `136dceedefffeeaa237a039a0ee17fc6baeca25763a889d17a8a66734c1eb9dc` |
| E19 | `src/gobby/mcp_proxy/tools/tasks/_claim_release.py:45–110`: coordinator-only release verifies holder/task. | `fc4f3930d32b1764aeb37a6910c0b05d6ace0138bd123ac0b9dfbdb9e7b40409` |
| E20 | Branch `src/gobby/tasks/step_records.py:387–434`: visible release handles exhausted review, not general maker handoff. | `af45f183aca90bad0d53eb445c1ec3dd3fe1b006fadea094d7c31a066e48f8e4` |

#### Implications for the Orchestrator

- Apply fifteen scope replacements as single unexpanded tasks, preserving surviving incidents/absorbed criteria. Apply the one moot verdict at R §6.1 together with the old policy retirement. Do not close addendum-covered work early merely because it is planned later.
- Preserve protected standalone/planning definitions. Do not equate a workspace flag with every seat's lifetime. Do not rebuild mechanisms A already owns.
- Keep validity separate from staffing priority. #22819 and wider host tuning remain secondary to v4 and hub/node delivery.
- Coordinate residual credential and approval-parser ownership with active fixes. This report proposes no new task tree and makes no task edits; Orchestrator applies verdicts and closes #24823.

### Risks

- Main R, reconciled branch R, live policy and installed rows differ. R §6.1 must reconcile live state; this report does not declare anything activated.
- No source edits, daemon restarts, process control, external mutations or incident reproductions occurred. Historical provider/auth/UI claims remain investigation inputs. Intended delivery or a missing search hit is insufficient for CLOSE now; none is recommended.
- #24132 may partly disappear under active #24816. #24792's parser overlap belongs with #24804/#24766. Credit active fixes before implementing residual scope.
- Inventory covers direct children only and is a time snapshot. Recheck before applying verdicts if task topology changes. The eight exclusions are fixed by the brief regardless of later claim changes.
- The sole authorized artifact is this report. Other sessions' staged/dirty paths must remain untouched. Tests of production behavior are outside this research audit; validate report coverage and commit isolation only.
