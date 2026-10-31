## Research Findings

### Research Questions

- Which code gates encode workflow policy outside replaceable rules or agent definitions?
- Which duplicate installed YAML, which need configurable replacements, and which preserve authentication or data integrity?
- Which proposed implementation leaves remove the most fleet friction?

### Domain Context

- #25394, research spike under #24818; researcher gobby#18787. Research only; no production changes or mutating runtime probes.
- AGENTS.md rule 16 requires configurable authorities and applicability. Fetched memories `0ab3ba06-e111-5ef3-b3ff-78e0ef08e1e3` and `09f8a0d7-d7fe-580d-862e-09a01acc584b` confirm the operator's ruling and the specific instruction to remove duplicate rule checks during the port. Searched project memory before recording findings.
- Existing definition mechanisms include `capabilities`, `task_step_writes`, `task_step_scope`, `task_close_scope`, `task_close_reasons`, `required_review_capabilities`, rule selectors, workflow variables and step tool gates. Reuse these before adding fields.
- Code must still validate identity, provenance and atomic transitions. A generic capability check processing installed YAML is not itself a hardcoded role policy. A fixed workflow decision concealed inside that check can be one.
- Initial HEAD: `183832aeb4a3c67332be30eef958a53cf6bcd956`. Source refresh: `0af3f464b6bb2c9ee6bc9e121c0e3f59230ef9c2` at 2026-10-31 06:06–06:08 UTC. #25388 landed during research; its holder-reclaim fix is incorporated below. Hashes identify exact working-tree excerpts, not a claim that all observations came from one immutable HEAD.

### Evidence & Sources

Used `gcode tree`, `outline`, `search-symbol`, scoped `grep` and `evidence` range reads across MCP task/session/workflow/spawn handlers, task close/land/step modules, team admission/dispatch and the rule engine. Broad identifier searches separated provider message roles from agent-definition names. This static sweep does not prove that every dynamic path was exercised.

Installed registry observations, approximately 06:01–06:05 UTC on 2026-10-31:

| Installed object | Observed state | Implication |
| --- | --- | --- |
| `require-handoff-feedback-before-authoring` | disabled; survey-active/unsubmitted block on handoff/exit | Handler still duplicates the condition (F1). |
| `block-clear-session-with-claimed-tasks` | enabled; blocks `set_handoff(clear_session=true)` with claims | Handler duplicates this policy too (F2). |
| `seat-no-pipeline-launch` | enabled; exception calls `dev_pod_launch_allowed(...)` | Helper hardcodes `devteam` (F4). |
| `prefer-gcode-for-source-read` | enabled; ordinary navigation block | Engine gives its name special remediation treatment (F9). |
| `consecutive-tool-block` | `get_rule` returned not found | Engine synthesizes this apparent rule (F10). |
| `dev-manager` | enabled; `capabilities=[manage_task_claims, land_tasks, accept_resume_retype]`; `task_step_writes=[closing]`; `team_manager=true` | Declared authority exists, but dispatch adds fixed workflow restrictions. |
| `code-reviewer` | installed result `4df1511a-1eb8-4c99-842b-6f918c7bb3e0`, matching chunk 5: `task_step_writes=[review]`; step workflow present | Fixer code grants implementation writes absent from that list (F5). |

States came from `gobby-workflows:get_rule` and `get_agent_definition`, not template inference. Rule inventory result `cd9f33f0-f060-4078-befd-84b81039ad0a` contained 236 rows. YAML names and fields described as proposed below do not yet exist.

Proposed implementation leaves, ranked by expected fleet friction. Ranking is qualitative, based on affected operations rather than measured denial frequency.

| Rank / leaf | Findings and enforced behavior | Existing coverage and replacement |
| --- | --- | --- |
| 1. Remove duplicate handoff policy; fold F1 into #25391 | **F1:** `_terminal.py:428–451` reads survey state and returns `feedback_required` despite the disabled installed rule. **F2:** `:452–461` returns `active_task_requires_compact` for claims plus `clear_session=true`. E1. | F1: delete the duplicate; `require-handoff-feedback-before-authoring` owns applicability. F2: `block-clear-session-with-claimed-tasks` already declares it. Reconcile #25391's explicit instruction to keep F2 before proposing its removal. Preserve session identity and compaction delivery integrity. |
| 2. Declare team claim and dispatch policy | **F3:** `_lifecycle_claim.py:131–160` forbids epic-root claims and restricts descendant claims to team seats with hardcoded `category != planning` exemption. Current-holder reclaim now works after #25388. E2. **F4:** `condition_helpers_sessions.py:980–985` permits only `name=devteam` or `pipeline:devteam`. E3. **F4b:** `team_dispatch.py:343–359,395–437` infers routing, checkout and slot policy from step-write grants; `_reviewer:230–244` forbids `task_id` and a second live seat. E4–E6. | Existing `seat-no-spawn`, `seat-no-pipeline-launch`, `spawnable_agents`, `resumable_team_agents`, `team_manager`, `checkout_mode` declare partial scope. Proposed `team-claim-scope` rule owns root/descendant restrictions and definition-selected exemptions. A definition variable such as `team_pod_pipelines: [devteam]` supplies allowed names to a generic pipeline-scope predicate. Proposed definition `team_seat` policy (task binding, checkout binding, concurrency slot), or equivalent rule inputs, replaces inference of unrelated authority from step-write grants. Preserve atomic ownership and team/tree identity validation. |
| 3. Declare review-loop authority and thresholds | **F5:** `step_records.py:299–340` grants any current claimant an implementation-step exception at the second rejection, bypassing `task_step_writes`. E7. **F6:** `_claim_release.py:207–222` rejects `land_tasks` recipients and allows an epoch reviewer to take a claim only at the second rejection. E8. **F6b:** `_de_escalation.py:10–12` fixes rejection limit at three; `step_records.py:488–489,547–562` blocks more records and releases/escalates at the limit. E9–E10. | Reviewer/manager prose describes this workflow, but prose does not declare machine-readable grants. Add `implementation` explicitly to the reviewer definition's existing `task_step_writes`; its fix-step MCP allowlist already names `record_implementation_step`. Delete the implicit grant; rule/step applicability governs when it is usable. Proposed `review-claim-handoff-policy` and `review-exhaustion-policy` rules own eligibility and exhaustion, with YAML variables `review_rejection_limit` and `review_fixer_after_rejections`. Keep locked, audited transitions and independent-review provenance. |
| 4. Move direct integration and close exceptions into YAML; existing #25393 | **F7:** `landing_policy.py:31–33,98–109` hardcodes Markdown/coverage allowances; `git_hooks.py:81–86` renders them into the reference-transaction guard. E11–E12. **F7b:** `step_records.py:231–242` always selects implementation/closing when enforcement is on, skips epic steps, and varies test/review only by configured category. No path-specific close-step exception is declared here. E13. | #25393 already specifies capability path globs, a rule evaluating every staged path, hook enforcement of that verdict, and YAML-declared reduced close-step requirements. Move current allowances too. Preserve linked evidence, dirty attribution and ancestry facts. Do not create a duplicate leaf. |
| 5. Consume declared landing evidence policy | **F8:** `land_commit.py:377–399` always disqualifies reviewers as landers, always requires independent review, requires implementation for every non-epic, and requires security approval for a fixed label/capability combination. E14. | Existing project `TaskClosePolicyConfig` and definition `required_review_capabilities` model part of this; the landing helper does not consume them. Proposed `task-landing-evidence-policy` rule and declared steps/review capabilities select applicability. Code validates exact SHA, epoch and independence for selected requirements. Preserve bundled independent/security-review defaults. |
| 6. Remove name-dependent engine policy | **F9:** `blocked_tool_recovery.py:17–20,220–228` gives only two named code-index rules a remediation exception. E15–E16. **F9b:** `core.py:892–897` uses agent names `autonomous`/`interactive` in audience routing. E17. **F10:** `core.py:398–443` blocks tools before ordinary rule evaluation under synthetic `consecutive-tool-block`; a variable changes threshold, but selectors cannot disable it. E18. | F9: block effects declare generic remediation state/predicates so renamed rules preserve behavior. F9b: derive audience from execution context or existing workflow variable `_audience`, without named-agent fallbacks. F10: ordinary `consecutive-tool-block` rule uses the existing `max_consecutive_blocked_tool_attempts` variable and a pure counter predicate. Keep batch/counter tracking as engine mechanism. The bundled variable is not a YAML switch for the gate itself. |
| 7. Declare terminal speed-command policy | **F11:** `_terminal_send_keys.py:20,31–40,152–158` hardcodes `/fast`, refusing it for every caller and directing the user to run it. E19–E20. | No matching speed-command rule was found in scoped bundled YAML search. Proposed `block-provider-speed-toggle` rule declares commands/applicability; definitions declare exclusions. Keep payload/idempotency validation in code. No external provider behavior was verified; the finding concerns the source's refusal policy. |

Credential-boundary candidates needing a security-preserving design, not immediate deletion:

| Candidate | Current behavior and evidence | Proposal |
| --- | --- | --- |
| C1. Workflow-definition mutation | `workflows/__init__.py:227–252` denies all non-None request principals; guard also used at 493,512,528,547,574,605. E21. | If maintenance seats should write definitions, declare `manage_agent_definitions` plus mutation scope. Enforce trusted authorization and prohibit self-granting. Otherwise document the credential boundary. No operator decision to relax it is inferred. |
| C2. Cron mutation | `cron.py:32–45,132–133` rejects agent-credential shell creation; guard also protects update/run/toggle/delete at 261,269,286,309,331. E22. | Optional declared `manage_cron`/`manage_shell_cron` and target scope must retain authenticated shell-execution boundaries. A rule exclusion alone cannot grant this authority. |
| C3. Daemon configuration mutation | `config.py:57–67` refuses `AgentApiTokenClaims` regardless of definition authority. E23. | Consider `manage_daemon_config` and permitted public keys if operator workflows need it. Preserve revision concurrency, secret isolation and trusted authorization. Blanket refusal is not proven to malfunction. |

Evidence ledger: full SHA-256 `excerpt_hash` values from `gcode evidence` schema 1, tool 1.9.83. Inclusive ranges. Reproduce using `gcode evidence --request-json '{"schema_version":1,"operation":"read","read":{"kind":"range","path":"PATH","start_line":START,"end_line":END}}'`.

| ID | Repository path and range | Excerpt hash |
| --- | --- | --- |
| E1 | `src/gobby/mcp_proxy/tools/sessions/_terminal.py:408–463` | `dd744ca1a29b86e0712b7d3f3ed9f3cea65784ad59c765340eab6ede6be1e7f2` |
| E2 | `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py:119–170` (refreshed) | `8f99d1a5bbbe744f35db6242a4225d951727edcfa2785a869621915332ad2607` |
| E3 | `src/gobby/workflows/condition_helpers_sessions.py:966–998` | `3131f9dff1e8bfabd6592f613548847a0767ff9a2128d2c9e212bed4c3d3451d` |
| E4 | `src/gobby/agents/team_dispatch.py:285–395` (refreshed) | `9fc4e9da11e24bb4d6f7d4788d512fa709a5830e2735e4fcf06ba0b2626a32b0` |
| E5 | `src/gobby/agents/team_dispatch.py:395–437` (refreshed) | `c0166d4d878803661fcaeee2f75c5faeddb4a0a581562b4b5564ca596d3dcd72` |
| E6 | `src/gobby/agents/team_dispatch.py:90–252` | `d8092e3b6090e1040f49f2ffa7ea3d8cafb2af990a0c93937f6b54626bb8d001` |
| E7 | `src/gobby/tasks/step_records.py:287–343` | `08ac6053a90add4ff199710e7438889c59bea2be05e7e68136a1755ac71d376c` |
| E8 | `src/gobby/mcp_proxy/tools/tasks/_claim_release.py:176–226` | `24f89625247ddfe89496450db423b4c750cbfa6b92ed0f97e86f6a263635dada` |
| E9 | `src/gobby/storage/tasks/_de_escalation.py:1–42` | `320443bd75fc6b486885eb7d81ccd4c212c4776c1c76f656959cf8f635905da5` |
| E10 | `src/gobby/tasks/step_records.py:480–567` | `0e126e95f9e1f6fdfb018bc6a1c9e420e619e15db04dd4bc87559b276197468f` |
| E11 | `src/gobby/tasks/landing_policy.py:19–109` | `10a8a73b9c9f92102dd487fcaeaced96937d5752c0c8eaf0b5015b2decc325d3` |
| E12 | `src/gobby/cli/installers/git_hooks.py:80–195` | `c944c88544673010c69f56b9e67357cbd8789dd50fd475bc4e92598fbfc3d1bc` |
| E13 | `src/gobby/tasks/step_records.py:170–282` | `1ed96f4e2a95b9394cba5e358359833e8dd7663ba5b393e3f939ac6e93a4ebc1` |
| E14 | `src/gobby/tasks/land_commit.py:350–403` | `6906e0ae05f660fd9dbf76cfeebc2485a349f2ebe613b5df552eb91a6ce9175c` |
| E15 | `src/gobby/workflows/engine/blocked_tool_recovery.py:1–137` | `065f8f115a29768d7ea301f371156d0085d0dea3edf315b7bdc8873a2845f133` |
| E16 | `src/gobby/workflows/engine/blocked_tool_recovery.py:218–263` | `da5b2892f1bb247783c0f1f250fb07195eb292e1332364aaf886d2639ebddaba` |
| E17 | `src/gobby/workflows/engine/core.py:875–901` | `b04457baed5c2a8d9a53bee62e6e7a0436721b6157dc91ef9700156be39dd633` |
| E18 | `src/gobby/workflows/engine/core.py:390–466` | `4f681f60b371064e6d484ab88c86006b35b6bfd9e3ffbd959e016177589fab4f` |
| E19 | `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py:18–62` | `d8303da427d3bfb1c241325486811fe3062c54e1d4c23e8512537a7988513282` |
| E20 | `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py:140–180` | `4d6f4dad8815283a1edcfaecb263774e668e35aafc4e516beeac288ae8ce9958` |
| E21 | `src/gobby/mcp_proxy/tools/workflows/__init__.py:218–255` | `3979d31ca8df13fa74682d11e7eb42c01ce289f097f241522a0efbdaf65b4af6` |
| E22 | `src/gobby/mcp_proxy/tools/cron.py:20–150` | `0e0995e69e9ec2ba328beae5f8dd103cb5dbc4b31aee74deadeb1a22c1dc8858` |
| E23 | `src/gobby/mcp_proxy/tools/config.py:24–90` | `7f2c4ea8524762bd598ae3507810b19468f6f3a715161b418513edae24954bcb` |

Checked counterexamples/boundaries:

- `_lifecycle_close.py:80–130` processes declared capabilities, close reasons, scope and protections: the desired configurable pattern. Hash `e02196e5d0a823a77af325dd4ab43fdaace43542891a205750f66aacfe7b5633`.
- `close_invariants.py:1–160` checks children, linked evidence, attributed dirty state and ancestry: explicit task data contracts. Changing step policy does not authorize deleting these. Hash `6fc77232b0f34e31577365242f495b04a53e85cf4e0754192912eab9b9fe194b`.
- `workflows/_pipelines.py:555–632` derives approval identity from authenticated caller: a trust boundary. Hash `0d23fb6e01a5443030a0b21101b0fb8b3a42ff2d34bfc8abb315fd1d6e064bce`.
- `evaluation.py:330–352` names `require-step-completion` for extra diagnostics in the inspected branch; excluded from behavioral findings. Hash `23edcc88c6885077833f64712f61245b1cfb62cc98294cdff1bdcb1ac2fde11c`. Synthetic engine observability labels alone do not prove named-rule authority violations.
- `team_lifecycle.py:195–290,870–898` binds admission to a fixed step ID and validates task/tree identities. Consider this rigid admission contract when generalizing runbooks; it is not a comparison against an agent/pipeline definition name. Hashes `0ef947e08bfde05f8777a6effed2cbbfdf8a96328178749b70149ee0d01aa864`, `0b0c2a1ae5ceb3a171d72bad7b20521b5b5a383c9d8cbacf9752038d0e6469f8`.

Validation: static gcode evidence reads; installed registry checks; related task reads #25388/#25391/#25393; Git history comparison during concurrent landing. A read-only `uv run --no-sync python` audit independently recomputed every ledger range's SHA-256 from current file bytes: 23 checked, zero mismatches. Report-only whitespace/diff review passed. No pytest, restart, process termination, agent launch or shared-state behavior probe was needed.

### Risks

- #25391 explicitly requires keeping the clear-session active-task check. F2 is a separate reconciliation proposal, not permission to override that task's accepted criteria. Any operator decision goes through the Assistant.
- #25393 was actively claimed during research and already covers F7/F7b. Fold findings into that work and recheck the implementation before filing follow-ups. #25388 is closed; its holder-reclaim bug is resolved.
- No denial-frequency or performance measurements were made. Later commits and installed overrides may change findings; verify hashes and registry rows before implementing.
- C1–C3 protect powerful operations. YAML authorization must retain a trusted boundary and prevent agents granting themselves privileges. Blanket deletion is unsafe and is not proposed.
- Proposed fields/rules require implementation design. These are grouped research proposals, not a decision-complete execution plan; load the plan contract before converting them into one.
- Keep SHA/epoch binding, author independence, provenance and atomic transitions while moving policy selection. A YAML toggle must govern all relevant entry points so the Rust port does not reproduce hidden gates.
