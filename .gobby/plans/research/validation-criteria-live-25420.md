## Research Findings

Yes: task validation criteria remain live, requiring nonblank text on non-epic creation/update and supporting plan coverage, scope, test guidance and agent review; completion and landing are gated by approved role-owned step records rather than an automatic evaluation of that text.

### Research Questions

- Where is `tasks.validation_criteria` written, forwarded, displayed and evaluated in current `0.5.0` source?
- What do close, landing and the Test Writer/Developer/Code Reviewer actually enforce?
- Which descriptions need the follow-up wording change in #25421?

### Domain Context

- Research spike #25420 under #24818; source-only investigation in the main checkout. No product code or live task criteria were changed. Existing mechanisms already retain and consume this field; adding a replacement field or validator is unnecessary to answer Josh's question.
- The database column remains nullable to accommodate epics and historical rows. `tasks_require_validation_criteria` requires nonblank criteria on non-epics. Its `NOT VALID` declaration leaves historical rows unvalidated while enforcing new inserts/updates. Source evidence: `crates/gcore/assets/schema/baseline.sql:3889` and `:4669`; migrations `476_runbook_review_state.sql:45-51` and `477_runbook_task_states.sql:53-59`.
- The earlier Assistant statistic (128 of 132 recent tasks) is context supplied by the task, not a count independently re-run in this research. The source trace establishes retention and consumption without relying on that statistic.
- Bundled YAML and procedures below establish the repository contract. This report does not assert that every bundled template exactly matches its installed DB row, or that a particular agent actually read its instructions on a particular run.
- Complete `gcode evidence` range records, including observation time, source content hash, excerpt hash, checkout and HEAD binding, accompany this report in the losslessly compressed `validation-criteria-25420-evidence.jsonl.gz`. The evidence index below ties each source occurrence to its range and hash. `validation-criteria-25420-inventory.txt` includes test and historical-document matches as well as production matches.
- Search method: `gcode grep -w validation_criteria src web crates docs`, exact symbol/caller searches and scoped `criteria`/`validate_task` searches; followed by tracked-file completeness inventories for `validation_criteria`, `validationCriteria`, `validation-criteria`, and descriptive phrases `validation[ _-]criteria|acceptance criteria|stored criteria|criteria_section`. Git inventory supplements indexed search because an archived document was unindexed; current behavior citations remain deterministic gcode reads. `validation-criteria-25420-descriptions.txt` preserves the broader prose inventory. A tracked-file exclusion audit found only `CHANGELOG.md` outside the source/docs/tests/plans scopes. Historical plan values under `.gobby/plans/` are excluded from the parameter-description scope.
- Validation: read-only `uv run --no-sync python -c` audits checked the SHA-256 source bytes and excerpts for all 211 records across 96 files, and coverage of all 240 current parameter occurrences in the scoped inventory. Both passed; no evidence warnings remain. Temporary collection scripts were removed. No product tests were run for this source-only research report.

### Evidence & Sources

#### Writers and write-through paths

| Path | What happens to criteria |
| --- | --- |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:48-64,94-119,177-184,231` | `create_task` exposes the field, checks effective state with `require_validation_criteria`, checks malformed named test references, and passes text into task storage. The parameter is schema-optional because epics can omit it; handler validation makes it mandatory for other task types. |
| Same file `:537,575-608,694-697` | `update_task` computes effective task type/criteria from current and supplied values, rechecks the invariant and forwards an explicitly supplied string. `None` preserves current criteria through this MCP interface; blank text cannot clear a non-epic. |
| Same file's `create_task_tree`; `src/gobby/tasks/tree_builder.py:177-194` | Tree creation forwards each node's criteria into `TaskManager.create_task`. It is an additional entry path, not an independent semantic evaluator. |
| `src/gobby/storage/tasks/_manager.py:179-198,218-236`; `_creation.py:41-64,83-104,150-210`; `_decomposition.py:22-71` | Manager/create-with-ID/decomposition paths forward criteria. The insertion boundary normalizes and requires it before writing the SQL column. |
| `src/gobby/storage/tasks/_update_facade.py:44,64-80,109`; `_updates.py:93,161-163,292,359` | The storage facade enforces the invariant when type or criteria changes; the lower update helper writes `validation_criteria = %s`. These are persistence and presence checks, not judgments of deliverable quality. |
| `src/gobby/tasks/criteria_text.py:10-26` | Shared normalization strips whitespace and rejects absent/blank criteria for every task type except `epic`. It does not measure precision or completeness. |
| `src/gobby/servers/routes/tasks.py:65,96,299-326,339-380` | HTTP create accepts and forwards criteria to the same manager. PATCH loops over explicitly supplied model fields, so it also writes criteria despite not mentioning the field inside that loop. Storage remains the invariant boundary. |
| `web/src/components/tasks/TaskCreateForm.tsx:45,144`; `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:52,173-190`; `taskFieldRouting.ts:18` | Creation submits trimmed criteria when entered; the detail editor displays and commits criteria through the PATCH field route. Blank non-epic submissions are rejected downstream. |
| `src/gobby/cli/tasks/crud.py:162-198,212-262`; `_crud_mutations.py:18-55,100-122` | Operator CLI create/update accepts `--validation-criteria`; create validates presence and both paths forward to storage. Research did not execute these operator-only mutations. |
| `src/gobby/plans/manifest_emitter.py:150-171,398-404`; `manifest_parser.py:23,34,233` | Manifest synthesis derives criteria from source acceptance items; parsing requires and preserves the manifest field. Synthesis also reads title/criteria to infer the implementation domain when it is not supplied. This writes plan metadata which later becomes task criteria. |
| `src/gobby/tasks/expansion/_contract.py:247-279` and following body | Contract compilation starts with manifest criteria, appends acceptance artifact references, and adds explicit RED/GREEN/final-green/test-quality evidence text for TDD entries. |
| `src/gobby/tasks/expansion/_compile.py:397`; `_apply.py:178,234`; `_reconcile.py:265`; `_deferrals.py:114` | Expansion recognizes both `validation` and `validation_criteria`, creates phase/container and leaf criteria, updates reused tasks during reconciliation and writes criteria onto deferral tasks. |
| `src/gobby/install/shared/workflows/pipelines/team-planning.yaml:148` | The planning runbook authors criteria for its planning task: decision-complete plan, validation and stamps. |
| `src/gobby/tasks/generation_schemas.py:51,68`; `src/gobby/install/shared/prompts/expansion/system.md:38,73`; `src/gobby/tasks/prompts/expand-task.md:17,78` | The current expansion JSON contract uses the alias `validation`; prompt guidance also describes `validation_criteria`. These author criteria before the application path above persists them. The latter prompt's table marks the field optional, which disagrees with the non-epic storage requirement. |
| `src/gobby/sync/task_review_serialization.py:29,90`; `src/gobby/sync/tasks.py:548-553,610` | Export serializes the field as review metadata key `criteria`; import deserializes, applies `require_validation_criteria`, then writes it with the task. This is preservation across sync, not a new semantic check. |
| `src/gobby/agents/team_teardown.py:666,710` | Cleanup-finding tasks receive explicit criteria about ending runs/retaining handoffs and removing or explaining retained trees. |
| `src/gobby/feedback/actions.py:369` | Feedback-created research work receives criteria requiring the recurring theme to be resolved or explicitly declined. |
| `src/gobby/storage/tasks/_close.py:100-105`; `src/gobby/projects/purge.py:111-116` | Legacy-row maintenance backfills missing non-epic criteria with an explanatory placeholder while preserving existing values and epics. Close and purge therefore contain writers in addition to normal creation/update. These placeholders do not prove acceptance. |
| `src/gobby/tasks/import_criteria.py:6` | An external-issue criteria-string helper exists, but repository symbol search found no production caller. It is an available authoring helper, not evidence of a live import writer. |

#### Readers, forwarding and ignored fields

| Consumer | Actual use and limit |
| --- | --- |
| `src/gobby/mcp_proxy/tools/tasks/_formatters.py:159`; `src/gobby/storage/tasks/_models.py:281,365-366,426` | DB decoding, full serialization and the default actionable `get_task` card preserve criteria. The model/formatter themselves only transport the text. |
| `src/gobby/plans/_task_store.py:22,43,110,131`; `coverage.py:365,568,764`; `_artifact_refs.py:25-86`; `deferral.py:100-102,172-176`; `coverage_manifest.py:424` | Coverage and deferral checks read criteria to confirm that mapped acceptance artifacts/behavior references are represented. The task adapter can fall back to description if criteria are absent; coverage-manifest serialization forwards the displayed criteria snippet. These checks establish coverage/reference fidelity, not implementation correctness. |
| `src/gobby/mcp_proxy/tools/tasks/_task_scope.py:172-180` | Explicit `test:` and `file:` references in criteria extend an already declared scope from task targets/description. If that declared scope is empty, the function returns before reading criteria; criteria alone do not establish scope here. |
| `src/gobby/tasks/expansion/_compile.py:499-533`; `src/gobby/tasks/commits.py:442-475`; `related_tests.py:58-66` | Expansion reads task criteria to discover relevant files and derive related-test search terms. |
| `src/gobby/tasks/commits.py:510-529` | `extract_mentioned_symbols` can read criteria, but symbol search found no production caller. Keep this separate from the actively called file-discovery helper. |
| `src/gobby/workflows/claimed_task_extra_skills.py:38-104` | Claim metadata reads criteria for TDD cues and named acceptance-test paths, projecting additional skills and variables. This selects guidance; it is not write authorization or a close verdict. |
| `src/gobby/servers/routes/agent_spawn.py:149-150` | Appends nonempty criteria to an agent's task prompt as a Validation Criteria section. Forwarding makes the text available; it does not prove compliance. |
| `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:52` and its rendering branch | Reads and displays criteria in task details, including read-only mode; edit mode writes them. `taskNormalization.ts:118` checks the payload's optional-string shape. `TasksTabActions.ts`, `types/tasks.ts` and field routing transport/type the field. |
| `src/gobby/cli/tasks/_crud_detail.py:10-58` | Normal `show_task_impl` loads a task but prints state, ownership, metadata, labels, blockers and description, omitting validation criteria. This reader ignores the field for presentation. |
| `src/gobby/storage/tasks/_close.py:214-237` | Automatic ancestor closure stops for a non-epic parent with nonblank criteria. Parent-owned acceptance obligations therefore prevent automatic closure on the last child. Epics do not receive this criteria-specific protection. |
| `src/gobby/install/shared/workflows/agents/test-writer.yaml:23-26,169` | Test Writer reads the task, writes tests pinning its acceptance criteria, and maps an out-of-order implementation's existing tests to criteria. This is a seat duty. |
| `src/gobby/install/shared/workflows/agents/developer.yaml:34` | Developer instructions require resolving named behavior tests from stored criteria and preserving RED/GREEN chronology. The source still describes a close barrier that the current close evaluator does not implement. |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md:6,12` | Code Reviewer fetches the task, runs its named tests and reviews the exact test SHA against stored criteria and RED evidence; later implementation is reviewed independently. Approval is recorded for the exact SHA. |
| Other review/coordination procedures | Research Reviewer and Docs Reviewer fetch and judge against task criteria; Dev Manager routes criteria-based tests; Epic Reviewer uses criteria and descendant scope for non-code epics; Consolidator merges criteria when directed to fold work. These are described in the description inventory below. |

Forward-only storage/transport paths do exist: model decoding/serialization, manager facades, MCP formatting, HTTP broadcast, sync review serialization, web payload types/routing, tree building, and spawn-prompt construction. They retain or deliver criteria without evaluating whether the deliverable meets them. That is an intentional separation of duties, not evidence that the field is globally unread. The CLI detail omission, unused symbol helper and unused external-issue helper are narrower exceptions.

#### Close, criteria review and landing

- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:46-219` loads the task, verifies caller scope and repository/commit context, captures attribution, resolves required step approvals, then calls `evaluate_close_invariants`. It never passes criteria to a semantic evaluator.
- `src/gobby/tasks/close_invariants.py:21-62` accepts children, commits, attributed checkouts, integration ancestry and required/missing steps; there is no criteria argument. `src/gobby/install/shared/skills/gobby/references/tasks/closing.md:70` explicitly leaves acceptance criteria, testing and quality with developers and independent reviewers. The close path does not enforce backticked commands or dispatch an LLM checklist review.
- `src/gobby/tasks/step_records.py:168-283` selects approved records in the task's review epoch and binds implementation/review/closing steps to the linked tip; required categories/capabilities come from project policy and the closer's definition. These records attest the responsible seat's judgment. They do not independently parse or grade `validation_criteria`.
- `src/gobby/tasks/land_commit.py:374-402,510-555` checks independent exact-SHA review, non-epic implementation approval and optional security-review authority. Its review-blocker function does not directly check a test-step record. Landing does not read `validation_criteria` or perform a criteria review of its own; test acceptance is handled by workflow review and applicable close-step policy.
- Searches for `validate_task`, `criteria_section`, `TASK_CLOSE_VALIDATION_SCHEMA`, `external_validation/` and `validation-criteria` found no active production criteria-review caller. `TASK_CLOSE_VALIDATION_SCHEMA` remains defined/exported, and external-validation plus criteria-generation prompt templates remain bundled. They are residual descriptions/schema assets, not proof that close invokes them.
- `create_task`'s schema at `_crud.py:423` still says `validate_task` checks the diff; create/update at `:423,696` still promise mandatory exact backticked close commands and syntax rejection. Current `_task_invariant_error` only checks presence and malformed named test references. The automatic-command and automatic-diff-validation wording is stale. The field's substantive acceptance purpose remains current.

#### Description inventory for #25421

The primary parameter descriptions are these; presence requirements and artifact-reference guidance should survive any wording edit unless separately changed by authorized work:

| Location | Description or instruction |
| --- | --- |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:118-119` | Create-handler docstring: acceptance criteria for validating completion; required except epic. |
| Same file `:372,418,423` | Create brief, category cross-reference and full parameter schema. `:423` contains the stale automatic-validator/command claims. |
| Same file `:511,521` | Get-task tool/brief descriptions explicitly promise the criteria field on the actionable card. |
| Same file `:696,706` | Update parameter description and category cross-reference; criteria cannot be cleared on a non-epic; stale exact-command claims. |
| `src/gobby/servers/routes/tasks.py:65,96` | HTTP/Pydantic descriptions: Acceptance criteria; New validation criteria. |
| `src/gobby/cli/tasks/crud.py:166-167,215` | CLI help: observable completion criteria, required unless epic; new observable completion criteria. |
| `src/gobby/storage/tasks/_decomposition.py:40` | Storage docstring: observable acceptance criteria for every non-epic task. |
| `src/gobby/tasks/criteria_text.py:7,11,19,23-24` | Criteria normalization/invariant docstrings and rejection message. |
| `src/gobby/tasks/commits.py:448,455,517`; `related_tests.py:65` | File/symbol discovery and related-test context descriptions. These describe consumers, not the public parameter's meaning. |
| `src/gobby/tasks/prompts/expand-task.md:17,69,76-78` | Expansion parameter table and generation rules. Includes examples at `:33,42,51`; table says optional. |
| `src/gobby/install/shared/prompts/expansion/system.md:38,73,84` | Expansion alias `validation`, evidence requirements and test-intent guidance. |
| `src/gobby/install/shared/prompts/validation/criteria.md:3,15-33` | Bundled criteria-generation prompt and specificity examples; no active consumer found. |
| `src/gobby/install/shared/prompts/external_validation/{agent,external,spawn,system}.md` | Bundled criteria-section inputs and QA-verdict instructions; no active production caller found. |
| `src/gobby/install/shared/skills/gobby/references/tasks/creation.md:11-15,26` | Required non-epic field, observable/specific/complete criteria and reference syntax/example. |
| `src/gobby/install/shared/skills/gobby/references/tasks/{overview,closing,live-work,artifacts}.md` | Task acceptance context, responsibility at close, live-work criteria and scope discipline. |
| `src/gobby/install/shared/skills/gobby/references/plan/approval.md:7`; `review/evidence.md:11-28` | Preserve acceptance items in derived criteria; review uses criteria/scope/TDD evidence. |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/{code-reviewer,dev-manager,docs-reviewer,research-reviewer,orchestrator}.md` | Role-specific use, review and approved consolidation of criteria. |
| `src/gobby/install/shared/workflows/agents/{test-writer,developer,epic-reviewer,docs-reviewer,consolidator}.yaml` | Seat criteria duties; Developer includes stale close-enforcement wording. |
| `src/gobby/install/shared/skills/annotate/SKILL.md:54-60`; `tech-writer/SKILL.md:168` | Task authoring with observable criteria and criteria-based documentation completion. |
| `docs/guides/tasks.md:68,73,78`; `cli-commands.md`; `http-endpoints.md:474`; `spec-writing.md:233`; `task-expansion.md:327,338` | User-facing requirement/examples, CLI option, HTTP editable field and one-line pass/fail manifest descriptions. |
| `docs/contracts/plan-coverage.md:560,578` | Manifest example and field table: one-line pass/fail. |
| `src/gobby/config/tasks.py:50-82,129-131`; `config/features.py:219` | Expansion-pattern templates/project commands guidance; these describe criteria generation configuration, not a close evaluator. |
| `web/src/components/tasks/TaskCreateForm.tsx`; `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx` | Criteria label, placeholder and field copy for entering/displaying the same task parameter. |

Historical descriptions also exist in `CHANGELOG.md`, `docs/archive/tasks_decomposition.md`, `docs/plans/completed/{TASKS,task-expansion-v2,task-expansion-v3,review-signal-learning,impeccable-ux-agents-draft}.md`, `docs/plans/abandoned/{UI,drawbridge-enhancements,ui/phase-0-tui}.md`, `docs/reviews/{mcp_proxy-tools,tasks,quality-subsystems,llm-prompts,TRIAGE}.md`, `docs/design/task-authority-audit.md` and `docs/reference-audit/tasks.json`. Treat these as archived designs/findings rather than live parameter contracts. Generated SQL/catalog snapshots under `docs/evidence/` describe the column/constraint rather than parameter wording. Exact occurrence lines remain in the inventories; historical narrative was not promoted into current behavior claims.

Recommended follow-up: use Josh's requested wording, “Outline the evaluation criteria an agent will use to ensure a high-quality deliverable. Be precise.” on the public create/update parameter descriptions; retain the separately true presence/reference rules and remove the unimplemented automatic-validator/exact-command claims. Check the HTTP/CLI/prompt/skill descriptions above for consistency. No new validator is proposed by this research.

#### Evidence index

Final source-byte and excerpt-hash checks passed at HEAD `78da223bc5a37e479af22934aa43ff083f7f8b15`: 211 complete records across 96 files, with no evidence warnings or source/excerpt hash mismatches.

Each identifying quote below comes from the cited numbered excerpt. The adjacent hash covers the entire range. Complete numbered excerpts and observation bindings are retained in `validation-criteria-25420-evidence.jsonl.gz` (read with `gzip -dc` or Python's `gzip.open`).

| Source range | Identifying quoted line | Excerpt hash |
| --- | --- | --- |
| `crates/gcore/assets/schema/baseline.sql:3884-3897` | 3889&#124;     validation_criteria text, | `e4c4c15753bef603469345db4705982394add09790bb6d302b58389ac39f9291` |
| `crates/gcore/assets/schema/baseline.sql:4664-4677` | 4669&#124;     ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID; | `9d93fc1c24d5a69ce150e7cb6837564357ba3242fef2b126549a07659f91233a` |
| `crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:40-55` | 45&#124; ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria; | `541dbbeccb236379fef610cf574510fff30d3d381ee8c635a11c9d0422a5581a` |
| `crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:21-34` | 26&#124; ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria; | `535399bb2c9b440adde17a738403939415bd2bfa780851b9bfb7cde3af3640f3` |
| `crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:53-66` | 58&#124; ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria | `a1406500f286695ae48460276d3b3406ee1066484a9bdb158f5e1183b88218ca` |
| `docs/contracts/plan-coverage.md:411-424` | 411&#124; gates above apply from expansion validation onward. | `469c6f167c3386e27e23d0bd04cc1880ecc7c5e822ca6cd650d29f3381d91e2a` |
| `docs/contracts/plan-coverage.md:427-440` | 428&#124; ## Planning Route and Draft Authority | `e543c99b0c0f3c13f15e167e61a10667936b84ea219c0c43bbb519f88c878b60` |
| `docs/contracts/plan-coverage.md:555-568` | 560&#124;   validation_criteria: <one-line pass/fail> | `56703f1651dab9d6696e9b44c434ddeb023d540ba4559e75d462bae89b5981ac` |
| `docs/contracts/plan-coverage.md:573-586` | 578&#124; &#124; &#96;validation_criteria&#96; &#124; str &#124; One-line pass/fail &#124; | `4dcc78618d955f6172bc6e93c9f7716a49ed6803d632cc94ec35a935c94d12a7` |
| `docs/contracts/plan-coverage.md:626-648` | 640&#124;   own acceptance criteria. | `98e511006dc169ec4406e492ffe26a45c5947e50cf9cfcd56c0228dae7d84093` |
| `docs/design/task-authority-audit.md:72-85` | 77&#124; &#124; update_task &#124; none &#124; rewrite foreign &#96;validation_criteria&#96;, &#96;affected_files&#96;, parent, priority &#124; G &#124; | `aeaee499c44f2b35f98f7ccea689f9b963a7158c3abc390746300b1e931e91ff` |
| `docs/guides/README.md:142-155` | 143&#124; ## Writing Specifications | `82784b18e6f5a6dd97cb1d23a0fdadcf2805d6683d298dfcdf1b13c74d9ddd41` |
| `docs/guides/cli-commands.md:555-569` | 560&#124; &#124; &#96;tasks create&#96; &#124; &#96;--description&#96;, &#96;--validation-criteria&#96; (required except epics), &#96;--priority&#96;, &#96;--type&#96;, &#96;--depends-on&#96;, &#96;--project&#96; &#124; | `b7685a33d0265e96e65cd5ac1f2c17fec91b84c741792f1fe92b01aa4e790f6f` |
| `docs/guides/configuration.md:769-782` | 769&#124; &#96;&#96;&#96; | `685665d4e6b36ac50d7058924bfda6b76008e1f56c3bfc631fbb96adef6b149b` |
| `docs/guides/http-endpoints.md:469-482` | 474&#124; and &#96;validation_criteria&#96;. | `9cf6e06cf8b49380481046e21d38cebe68d008c0e884df73beb012818be084d4` |
| `docs/guides/remote-docker-acceptance.md:175-188` | 180&#124; as required by the historical acceptance criteria. | `01ff64ed039212ce91e4ca2d78b45f8f0d81a24cfa558cb565f98d81b39e82fa` |
| `docs/guides/spec-writing.md:194-207` | 199&#124; target, acceptance criteria, and behavior-pinning or test-infrastructure scope. | `805a804c0426a911f2e8318afa9ecac76d3268d657326bd667a35d21a68d4355` |
| `docs/guides/spec-writing.md:228-241` | 233&#124; &#124; &#96;validation_criteria&#96; &#124; One-line pass/fail check &#124; | `5f541f3f99a52bf8874da2b0aab285cb47a7be01e8a9c746972efd5fb8c8a6b5` |
| `docs/guides/task-expansion.md:172-185` | 173&#124; Important fields: | `8631c7d739fd7ef76b12d7349e1d9742b6f88aa22992fea938b8c71e4773c4c7` |
| `docs/guides/task-expansion.md:322-346` | 327&#124;   validation_criteria: User model exists and tests cover core behavior. | `066ee6370b50d4d26fbd7f3b1d87bc6b66f34bca05414fb3fe509f969b8f0993` |
| `docs/guides/task-expansion.md:391-404` | 391&#124; Rules worth knowing: | `a845e60d5915979ab3cb4d3744640cf906ddaa1a2190e4ef49a0a195cc05d64f` |
| `docs/guides/tasks.md:18-31` | 18&#124;     Task --> Closed["closed metadata"] | `92a081f852a8de96258f4dc6ddbccda1bb21b72496e63e2c31965dfbbde739b3` |
| `docs/guides/tasks.md:63-86` | 68&#124;     "validation_criteria": "Task examples match registered schemas and all guide links resolve.", | `9c94aa6676da4e32d2a8046fcf58598c1226e635427d0c046f6c500dd3203833` |
| `docs/guides/tasks.md:349-362` | 354&#124; results for independent review. Acceptance criteria are authored task data; | `9f4d49ef04671779ada8ee63b63f55c5d833555909f0cbff16a97ceeef7e672a` |
| `docs/guides/tasks.md:391-405` | 396&#124; gobby tasks create "Title" --validation-criteria CRITERIA [-d DESCRIPTION] [-p PRIORITY] [-t TYPE] [-D BLOCKER] | `177235ede02d46148a5583d1be03e7025df2c375f676ada01503d7507e3d940f` |
| `docs/guides/tdd-enforcement.md:10-23` | 11&#124; For a manifest entry with &#96;tdd: true&#96;, the compiler adds: | `f025c883ab640f70a98f52e526c8241bb6544d4685e4818cfd22b5796d7b6e77` |
| `docs/guides/tdd-enforcement.md:25-38` | 26&#124; All developer agents load &#96;gobby:references/development/obligations.md&#96; before | `cad1dea4a06588be4a98bf7953ce68f38eae88245d7f1a5c03628d022dadf987` |
| `docs/guides/tdd-enforcement.md:61-74` | 66&#124; Plan authors describe behavior and acceptance criteria. They do not add filler | `a58bbf756a1a3db6b95e1ed59cdcec5875c8c7398ae3e62116a56c60f2c2178a` |
| `docs/guides/tdd-enforcement.md:90-103` | 90&#124; When auditing this guide, verify: | `5dc6947b8a0f70e0c98dc78abc38564419753f2bd703e6b50dbbc6270c81c2ab` |
| `docs/reference-audit/tasks.json:1426-1440` | 1431&#124;       "finding": "Docs creation example omitted validation_criteria and implied only code required criteria.", | `c72d237690a8e1daaf3ffe1371e12314cbd5814f49a900a70a1de5165ceace0e` |
| `src/gobby/agents/team_teardown.py:661-674` | 666&#124;             validation_criteria=( | `b6b0a4efdd3542a0723f8aee9ae762169c34de08763b977379e288dad650ff16` |
| `src/gobby/agents/team_teardown.py:705-718` | 710&#124;             validation_criteria=( | `1150fb150592536bd24eea1fc1a66f3ed2b8a61b25445126524ea19cd9e3c12b` |
| `src/gobby/cli/tasks/_crud_detail.py:10-58` | 10&#124; def show_task_impl(services: CrudServices, task_id: str) -> None: | `fddc7ddda5acb98573d5efff3f6b6a640857da6df24a5d6d960bc5273ea7d482` |
| `src/gobby/cli/tasks/_crud_mutations.py:5-40` | 10&#124; from gobby.tasks.criteria_text import TaskCriteriaError, require_validation_criteria | `6bfe6515ee0e793029ef6ce610a214a07fc719be0cf175477d422df150f5dab6` |
| `src/gobby/cli/tasks/_crud_mutations.py:50-63` | 55&#124;             validation_criteria=validation_criteria, | `a189b748c4934692e7ce1fffd797f6acef783871b34e71a92ea6985046f9ee84` |
| `src/gobby/cli/tasks/_crud_mutations.py:95-108` | 100&#124;     validation_criteria: str &#124; None, | `98d0b0eeeb4d8091ddea3a9357f41093a5c99222c3b286995dd74926fdfaa30e` |
| `src/gobby/cli/tasks/_crud_mutations.py:116-130` | 121&#124;     if validation_criteria is not None: | `fa8dfb19bde6c19d3b6a21e00c6e2619c71525fd975e67abf8545291ea50f6a5` |
| `src/gobby/cli/tasks/crud.py:161-184` | 166&#124;     "--validation-criteria", | `1f33a77fa3ad5baf1c2f598ddccdd917a1d086f9190a7ab4c073c7bcf7bf0164` |
| `src/gobby/cli/tasks/crud.py:189-202` | 194&#124;         validation_criteria=validation_criteria, | `cea6f8ed9f497f789223b5fe71ce44d8c0fa7e2e5bad7a851f3e0c76186304d8` |
| `src/gobby/cli/tasks/crud.py:210-223` | 215&#124; @click.option("--validation-criteria", help="New observable completion criteria") | `076e491a4ea85166e79f6f9d5ca91e431965d3e310c4c3bae76b368e31567491` |
| `src/gobby/cli/tasks/crud.py:234-247` | 239&#124;     validation_criteria: str &#124; None, | `ed8412e9be27138d671a2b319963812e6cd0d34ec48d9f321992730842ededc1` |
| `src/gobby/cli/tasks/crud.py:253-266` | 258&#124;         validation_criteria, | `13abb3999ad221a7ccd17663a01fd0db33358a9650e1205a66b3aa0077a94f08` |
| `src/gobby/config/features.py:214-227` | 215&#124; class ProjectVerificationConfig(BaseModel): | `e498374ed5ccc00dc8e400311c493680a2faf24a57c9564eb95d0af6529d1d67` |
| `src/gobby/config/tasks.py:1-13` | 1&#124; """ | `b5af8e71757317ba344dfcb2e8c07fc5d3390b9194a8ef9fd7eef495927f34c3` |
| `src/gobby/config/tasks.py:45-60` | 45&#124;     "merge_workflow_variables", | `8e448ff107ea50e4c00eb0870f2f1e5a2a50f4c8d19bad84e6841d051a3fe2ce` |
| `src/gobby/config/tasks.py:77-90` | 77&#124;                 "All existing tests pass: &#96;{unit_tests}&#96;", | `ccfa5b8644e7507b83f203d555923c29dec51236eebbde2aa2c47b6598aef70f` |
| `src/gobby/config/tasks.py:126-139` | 126&#124;         gt=0, | `a5a979739a42c61bfb0ab1272e975cca6da82beb615d9f1728d1ee3bb85796a7` |
| `src/gobby/feedback/actions.py:66-79` | 71&#124;         validation_criteria: str &#124; None = ..., | `e27ec2e9141ff438f97e339f6c45dc145ece70170369a8c3ca0a513ba10edbbf` |
| `src/gobby/feedback/actions.py:364-377` | 369&#124;             validation_criteria=( | `4b758606f7a141b06bd6edb260c2365ee9e2036ab13deb5ddb8734f8ca4e86fc` |
| `src/gobby/install/shared/prompts/expansion/system.md:30-85` | 30&#124;     { | `779d3c06ac936159dd20b5ee0f3089f36a734ab9b63a5ca42f4410d507156b87` |
| `src/gobby/install/shared/prompts/expansion/system.md:30-89` | 30&#124;     { | `8649d74406086587ab8fe0f4a6e96917a2a7473423ca53e6a59fc2da16f51f54` |
| `src/gobby/install/shared/prompts/external_validation/agent.md:5-21` | 10&#124;   criteria_section: | `a7111a4f7b81eed845e8d15578cfc1395076768decebfe5bbc051e285ecda555` |
| `src/gobby/install/shared/prompts/external_validation/agent.md:27-54` | 32&#124; Validate whether the code changes satisfy the acceptance criteria. You have access to tools to: | `4a5878d8e15a609f607e45f89c2aa94df3ae619e821f79a8b7e3fd632b7fb373` |
| `src/gobby/install/shared/prompts/external_validation/external.md:5-21` | 10&#124;   criteria_section: | `a7111a4f7b81eed845e8d15578cfc1395076768decebfe5bbc051e285ecda555` |
| `src/gobby/install/shared/prompts/external_validation/external.md:27-46` | 32&#124; {{ criteria_section }}{{ priority_section }}{{ symbol_section }} | `b7bf262ad810e9ded1c3cf3915178e2e542e2b0d8778927f08de0621a3e3ad4f` |
| `src/gobby/install/shared/prompts/external_validation/spawn.md:9-25` | 14&#124;   criteria_section: | `90a3a674a631ad333de8d531c82db4a91f222ceb750cd99b95bfd8463359adbb` |
| `src/gobby/install/shared/prompts/external_validation/spawn.md:46-59` | 51&#124; {{ criteria_section }}{{ category_section }}{{ priority_section }}{{ symbol_section }} | `789d3262a466a146219423079d355665e610c30434a539df32600fd9cadfd855` |
| `src/gobby/install/shared/prompts/external_validation/system.md:3-8` | 8&#124; You are an independent QA validator reviewing code changes. You have no prior context about this task — evaluate purely from the acceptance criteria and the changes provided. | `a1dbb1037824891b0d4bec9f9c2e8ac8bcce78248b67c2e4a4b135d09feb224e` |
| `src/gobby/install/shared/prompts/validation/criteria.md:1-47` | 2&#124; name: validation-criteria | `52719e65bcc3f221d3756d8b7a09e621008000f1f5e717a3f390049b64e43fde` |
| `src/gobby/install/shared/skills/annotate/SKILL.md:55-68` | 55&#124; or unavailable reason, and durable local capture location. Do not put screenshot | `1f968137be14ddb20c08de8db245936a16cb7eccb738cfe4bd65e8f6093d162d` |
| `src/gobby/install/shared/skills/architecture/SKILL.md:54-67` | 59&#124; Do not write implementation tasks or PRD acceptance criteria. Keep the brief | `eece731c2dd329f65db343b3fad02c84bb99a9341aef18c5b4d2a6f7276b14bf` |
| `src/gobby/install/shared/skills/architecture/SKILL.md:108-113` | 113&#124; Do NOT write &#96;### N.N&#96; task sections or PRD acceptance criteria. | `d337118d60e91a6db6fac9959a54175804a71cd0b6db81826bca0f8c089732b2` |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md:1-18` | 12&#124; - Test review. Before implementation, review the Test Writer's exact linked test SHA against the stored criteria and its authenticated RED evidence. Check that the failures demonstrate the required behavior and that assertions do n… | `0cfa5ed2cb333761955f1ee89d8ddd27b9322e3e9d57111235a033bfee2e175a` |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md:1-25` | 1&#124; # dev-manager procedures | `ba0f540618bb43c5690c2ba9bab786ea5e80af2710482d1fb47c6dc8ad51ab7b` |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/docs-reviewer.md:1-17` | 1&#124; # docs-reviewer procedures | `32361b408bc27d8ef5ecb872bce428fd2c754ceb4e54d44db7516c3cd1997b99` |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md:1-25` | 1&#124; # orchestrator procedures | `d289f8d81502fc2b98229665e1aef17143ea11ff3eb5c43a9a10e0057d8da2db` |
| `src/gobby/install/shared/skills/gobby/references/agents/procedures/research-reviewer.md:1-18` | 1&#124; # research-reviewer procedures | `529635162960be8be7d7ff06c1b186dd9f3cddaae4c9243be6f737cfd24abb0f` |
| `src/gobby/install/shared/skills/gobby/references/plan/approval.md:2-15` | 2&#124; Load before accepting review evidence, applying M1, or handing a plan to implementation. | `052d3393c8957c9e0acdd0b99b46b5210fbca43d114598cfa39ab64886a3c221` |
| `src/gobby/install/shared/skills/gobby/references/review/evidence.md:1-35` | 1&#124; # Review evidence | `4e29ce2cdef6b1138c4a62bd54a6c2a2c6dac34f89ad6f4da93b93ffe3dcb89a` |
| `src/gobby/install/shared/skills/gobby/references/tasks/artifacts.md:1-36` | 1&#124; # Task evidence and artifacts | `97546e38f3aced9f75f0e0de3d38975a77f78f7b251a54623872e101ec90a93c` |
| `src/gobby/install/shared/skills/gobby/references/tasks/closing.md:1-100` | 11&#124; 1. Finish the task, resolve owned findings, and check its acceptance criteria. | `d6621c66a21f113dfe9a5aa4643340a5561d0840edb76644a02179f6a1b1416d` |
| `src/gobby/install/shared/skills/gobby/references/tasks/creation.md:6-19` | 11&#124; Every task type except &#96;epic&#96; requires meaningful &#96;validation_criteria&#96;, including | `a24594d103cb1246c21cd8f07be9ee0e2c3a2e781ae8f4fb7870f42b2304cb48` |
| `src/gobby/install/shared/skills/gobby/references/tasks/creation.md:21-34` | 26&#124;     "validation_criteria": "Task examples match registered schemas and all links resolve.", | `5aa6a4226067075113552c7c0a8eadcc609da825b05cec22c16d999597e39ee5` |
| `src/gobby/install/shared/skills/gobby/references/tasks/live-work.md:1-30` | 1&#124; # Live interactive work | `3913d633614fa3094f1ffbd07c4c528ba52ffaecbfa56f01e443fef18857d0b0` |
| `src/gobby/install/shared/skills/gobby/references/tasks/overview.md:11-24` | 16&#124; acceptance criteria. A listing is bounded by its schema's limit; do not assume | `aba6af73b3e084d134437b1534f891e6384654f6502d1388570c44992e1e409d` |
| `src/gobby/install/shared/skills/prd/SKILL.md:2-38` | 7&#124; triggers: prd, product requirements, acceptance criteria | `1131ffca140df3c13ca0677fe26d9a74a29c626c17c9f132fea6d5c5250f0487` |
| `src/gobby/install/shared/skills/prd/SKILL.md:46-59` | 51&#124; ### Acceptance Criteria | `d53983f589b041a1223d3ac52204c4be9e71b56baac933b9f75088586946b9ae` |
| `src/gobby/install/shared/skills/tech-writer/SKILL.md:163-176` | 168&#124; against the task's acceptance criteria, and close. Closing a docs task | `6356d9c1459abd58663bf984e4bca28e6b542f7ec707f2c684d5c84c5e5f811f` |
| `src/gobby/install/shared/workflows/agents/consolidator.yaml:32-45` | 32&#124;     ## Role | `92bc10efea8af516dca4da967401ee237161b4e360f29238f80b4068be2f4e15` |
| `src/gobby/install/shared/workflows/agents/developer.yaml:1-50` | 34&#124;     - TDD. Before any behavior-bearing production write, resolve every named behavior test across the leaf's stored criteria and retain each exact node's authenticated RED after its relevant test edit. Skill completion permits test… | `1cc99f9ef1988aedb41cb8e97298f320e119034d9e8a9cdd3c51432c93f35974` |
| `src/gobby/install/shared/workflows/agents/docs-reviewer.yaml:1-15` | 1&#124; name: docs-reviewer | `bb5370b7f3bb41451f0d6c441dcab04c33d812394986bc3bda6244b77b615c1b` |
| `src/gobby/install/shared/workflows/agents/docs-reviewer.yaml:175-190` | 175&#124;     - name: review | `2cd5763d45b9bd9c993aded82900e3c2a73044a722306f279d435f1166dc232f` |
| `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml:51-67` | 51&#124;     - Load gobby:references/code-index/overview.md, gobby:references/review/epic.md, tech-writer, tasks, and | `abbac5c755610ac5aba1cd51098c494a34282cfec2c57abca476b37a34351268` |
| `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml:186-199` | 186&#124;     - name: review | `0ae5ca5d16d9bd4cddf4b3bac2d5931ca6a72fb0dc600b69513c58f6b90ac3f6` |
| `src/gobby/install/shared/workflows/agents/test-writer.yaml:1-40` | 23&#124;     - Start. Send your session ref to your Dev Manager with &#96;gobby-agents:send_message(target="session", target_id=<Dev Manager session ref from your launch prompt>)&#96;. Then read the task with &#96;gobby-tasks:get_task&#96; … | `4efdcd1c50ab1313a65adf790ad3ba5e5da2f8ed9ed9ffcd8f0510036fba04a8` |
| `src/gobby/install/shared/workflows/agents/test-writer.yaml:164-177` | 169&#124;         Write the smallest tests that pin the claimed task's acceptance criteria and run | `cad0517ed79a771264a89e558e281bc2f0b7e2aa18560c2e386f1c6e9d6b282f` |
| `src/gobby/install/shared/workflows/pipelines/team-planning.yaml:143-156` | 148&#124;         validation_criteria: "The plan file is decision-complete, passes gobby plans validate, and carries its consensus and manifest stamps." | `deddfcafaba34cbb3519e6b0bfbb28279fc07cfc9442ec0e294c643d153e491a` |
| `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml:46-59` | 46&#124;   claimed_task_requires_tdd: | `27217b2e8993ae1e1475d5e8a44d60928f32d0f5d2b18ae357c4fb14336e6868` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:31-44` | 36&#124; from gobby.tasks.criteria_text import TaskCriteriaError, require_validation_criteria | `153ccfde42cf587fe0c5d4aee5c4674c410d6acbfda01141ceaec72a2fb03ae7` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:46-70` | 51&#124;     validation_criteria: str &#124; None, | `683d2e964d74ae658dcd14e3ad99e774013b4a8eb21c7ed49d2ebe9a378263cd` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:89-102` | 94&#124;         validation_criteria: str &#124; None = None, | `87366fc55b2cb17a35e0f9c53f2646029f3b3fb5669466931a6c6b0cc0accd15` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:113-126` | 118&#124;             validation_criteria: Acceptance criteria for validating completion. | `14ddd015ef6cfee2ae93d177628b36f6057803c9eb19acd96818e7657579cc13` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:175-190` | 180&#124;             validation_criteria, | `cb0544b79e02e31e3d89b63b9e42e26c9d23bbb4499dd9f88c3c099a684d1288` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:226-239` | 231&#124;             "validation_criteria": validation_criteria, | `830606f9975b9fcb85d362776ef9f9f7e20e39c012f6006cb28e688e76dbc594` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:367-380` | 372&#124;         brief="Create a new task. Requires: title, session_id, category, validation_criteria (every task_type except 'epic')", | `db76633b5b63c10b211eced004d2e885948f5296a92c34c4ca69062008928a50` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:413-429` | 418&#124;                     "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing te… | `df93b87b864ae55344fdf538c96016170567956de299ddc98a4d4f6ce9fda213` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:506-529` | 511&#124;         description="Get task details including dependencies. Task ID can be #N (e.g., #1), path (e.g., 1.2.3), or UUID. Returns an actionable card by default, including description, validation_criteria, labels, state, and depende… | `fbcb4f83e3dac18eb1e13a4ff824af4656ffb6601c242b7e2460d0ef03e12476` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:532-545` | 537&#124;         validation_criteria: str &#124; None = None, | `ea87dbe187bebdb9a8012b7eeab255edc073ef613dc8fb09248609d7692ad7f7` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:572-600` | 577&#124;         effective_validation_criteria = ( | `2daf0cf424acc3820a1dac15ff200896d987802e13ac41cd72851de72824bc3a` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:602-616` | 607&#124;         if validation_criteria is not None: | `5e865c2c0f677e8822e221a69a63700809ccafe3e599ff716ad230bcd1489c38` |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py:689-714` | 694&#124;                 "validation_criteria": { | `a753658bb928a82a0aed41362a5639e35293089c989546c656f2923952879452` |
| `src/gobby/mcp_proxy/tools/tasks/_formatters.py:154-167` | 159&#124;         "validation_criteria": _task_value(task, "validation_criteria"), | `1da7c964ca0a5eb26179c54fa387ab310f43856828f2c8638deb9809a98f7eed` |
| `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:46-219` | 46&#124; async def _evaluate_close( | `0d7a8eee711e7f1dffa85d638d00fdde2b994f41072fa58be337f19e316f8733` |
| `src/gobby/mcp_proxy/tools/tasks/_task_scope.py:171-184` | 176&#124;         for reference in extract_artifact_references(task.validation_criteria or "", kind): | `2ba09758c382d8e79d90928ef8265b7b3c32e50c47cf68dbea0e83ecb162666e` |
| `src/gobby/plans/_artifact_refs.py:20-94` | 25&#124; def artifact_referenced(item: AcceptanceItem, validation_criteria: str) -> bool: | `e8cba2063a86c8d6421a827a832d053ffe4c9fe6efe1a91faa355539e19fb5ec` |
| `src/gobby/plans/_task_store.py:17-30` | 22&#124;     validation_criteria: str | `dc9e115eeb3840e82ce6f5e09856ef06e88dfe7eb7dc9295ab733bf29ee2bdf4` |
| `src/gobby/plans/_task_store.py:38-51` | 43&#124;             "validation_criteria": record.validation_criteria, | `465370ae7735ba6783dec296bb11f8816da6795dfaefd2983996aee4e86d9e26` |
| `src/gobby/plans/_task_store.py:105-118` | 110&#124;         validation_criteria=task.validation_criteria or task.description or "", | `e169704ed6d7b8b7064d8eac5c97b7c6ab8ddc8f5e0fb5b4c637e2a8928b799d` |
| `src/gobby/plans/_task_store.py:126-139` | 131&#124;         validation_criteria=_first_string(raw, "validation_criteria", "validation", "description"), | `2c3501c834fffdb07e16c6b3b4d76f80e4aa2652fe737850281f2fed7124c5b6` |
| `src/gobby/plans/coverage.py:79-92` | 84&#124;     validation_criteria_snippet: str | `a2553eb3f4cf70bc596429852c822c1ab565552f431e10706b06e2381e2ef40c` |
| `src/gobby/plans/coverage.py:140-153` | 145&#124;     leaf_validation_criteria: str, | `2eeb19eae8fedece2cae32349baf1815a8bba676bc724198f18476eb820b322b` |
| `src/gobby/plans/coverage.py:162-175` | 167&#124;     if not artifact_referenced(item, leaf_validation_criteria): | `a926b772b34f3a34a4cc5fa4306d5defb8abd2e5fdb6374da096f509f1587fa5` |
| `src/gobby/plans/coverage.py:360-376` | 365&#124;         result = validate_covers(record, task.validation_criteria, task.ref, plan_doc) | `ed15d216d3d3de4d3cbf65a039d53b3c61be1498fe7a46f850455a336e06305c` |
| `src/gobby/plans/coverage.py:553-606` | 558&#124;                                 validation_criteria_snippet=str(exc), | `f24e73f7a4631c350ac0fd343301ea86ecadc7c8a713501eaf7a03ed9e70f9b9` |
| `src/gobby/plans/coverage.py:759-772` | 764&#124;                 "validation_criteria": record.validation_criteria, | `21e05553d219e102d263ec0e51dcbb6e56a926bd26b9932213de3be3b470e59e` |
| `src/gobby/plans/coverage_manifest.py:419-432` | 424&#124;         "validation_criteria_snippet": _attr(leaf, "validation_criteria_snippet"), | `ad305bb3d6ee1f6167a0aad3b28037039be4d3bc8f8fd402161be28865efbdde` |
| `src/gobby/plans/deferral.py:13-26` | 18&#124;     "validation_criteria_does_not_duplicate", | `ab324b8a44a929acc503ca306fd492f0aa9beba3eac3bfb1043829c54a5c528a` |
| `src/gobby/plans/deferral.py:95-116` | 100&#124;     validation_criteria = _task_validation_criteria(task) | `ef604c8c9e81e45d89a8a6a213b3af1cddd5c0d7e6408a3ca8215e4f0cfe4597` |
| `src/gobby/plans/deferral.py:167-181` | 172&#124; def _task_validation_criteria(task: dict[str, Any]) -> str: | `b524bf82e9b89efc199e1477fe2680823f4dda6445969a2274b33e06443ac7e3` |
| `src/gobby/plans/manifest_emitter.py:145-158` | 150&#124;         entry["validation_criteria"] = "\n".join( | `8d77ab496da66362be272033da9c425e27c638547ad3fc3aaff02df56b4cb256` |
| `src/gobby/plans/manifest_emitter.py:166-179` | 171&#124;                     str(entry["validation_criteria"]), | `17fd07d63457c64b7cf529e4894798f83e64ef1fa17efdbd524cbc7a3e3cf5f2` |
| `src/gobby/plans/manifest_emitter.py:393-406` | 398&#124;         "validation_criteria": validation, | `6daa2e952a18f674e99e3fe16f55939669c0c82a812fcfca9282d964f448b6f4` |
| `src/gobby/plans/manifest_parser.py:18-42` | 23&#124;     "validation_criteria", | `6d9b933614b6788ef2b20846aa606a42da15d2c2007ebb94eeaf4a63f95d956d` |
| `src/gobby/plans/manifest_parser.py:228-241` | 233&#124;         validation_criteria=str(raw["validation_criteria"]), | `bd90c341d79d9a5bea6088d7af9d270ba21739d594387b9a68f9d1425647b4f7` |
| `src/gobby/projects/purge.py:103-122` | 108&#124; # tasks_require_validation_criteria is NOT VALID: legacy rows without criteria | `442e4cd2e5cc5a0672328d58875fbfab0cf4b6dea8544dd8331021030b6f72f4` |
| `src/gobby/servers/routes/agent_spawn.py:144-158` | 149&#124;     if task.validation_criteria: | `163236c905e29a9313a476e1a47acc20eee78dd55b83ddc5041ccb03e12f79c0` |
| `src/gobby/servers/routes/tasks.py:60-73` | 65&#124;     validation_criteria: str &#124; None = Field(default=None, description="Acceptance criteria") | `4d7f4c0871574e1da70eabe07f9a5f8375880d79c486cc24b729f79ac1030793` |
| `src/gobby/servers/routes/tasks.py:91-104` | 96&#124;     validation_criteria: str &#124; None = Field(default=None, description="New validation criteria") | `7f26445c98d073326cbc22ca2a92c5c9fe4a9630300e20b17b1d8753987b7713` |
| `src/gobby/servers/routes/tasks.py:311-324` | 316&#124;                 validation_criteria=request_data.validation_criteria, | `680531f04dda9fd47dfe1e9c97b139d4dbf4fa256a662a826fe3a432cf0670e1` |
| `src/gobby/servers/routes/tasks.py:339-380` | 339&#124;     @router.patch("/{task_id}") | `3b2c502200e9f2cd7800cb7edab33d272f2f3a317cc33da3b49fe0645879f63a` |
| `src/gobby/storage/tasks/_close.py:12-25` | 17&#124; from gobby.tasks.criteria_text import normalized_validation_criteria | `1de211e21b9b0bd9b0fe68a1d9c67d6dc6c01a17c6b1c59be1714b984cee645c` |
| `src/gobby/storage/tasks/_close.py:95-111` | 100&#124;                validation_criteria = CASE | `54450d75529e942354d345bc1ca9009fe73a4fe87c9ef0f91422b37b5ac4c416` |
| `src/gobby/storage/tasks/_close.py:219-232` | 224&#124;                 and normalized_validation_criteria(parent["validation_criteria"]) is not None | `2891bcd400bd591f8a8e33b6f5487bceb03774b37e1e4c941c4f1021ed06cdf8` |
| `src/gobby/storage/tasks/_creation.py:19-32` | 24&#124; from gobby.tasks.criteria_text import require_validation_criteria | `7eeb5cd6b65bad57837acdcfde304acccc9c34c35b73601c3cb93e42788ef371` |
| `src/gobby/storage/tasks/_creation.py:36-49` | 41&#124;     validation_criteria: str &#124; None = None, | `b2dd944ff46ba215870e0e96da7b159d5ed2002575ce22d64467e35cab56eb4a` |
| `src/gobby/storage/tasks/_creation.py:59-72` | 64&#124;             validation_criteria=validation_criteria, | `293540458bd5c21ddfd3bfc550190eaa629e938c741b3c0cacfb13d3fb736869` |
| `src/gobby/storage/tasks/_creation.py:78-91` | 83&#124;     validation_criteria: str &#124; None = None, | `06552e0fe16fbe48a34927a30014c0f14e21c2941c3221eae7c6997e6e517915` |
| `src/gobby/storage/tasks/_creation.py:99-112` | 104&#124;             validation_criteria=validation_criteria, | `378d3e54e397b81f714866431f4da801b78f18768073db50d52be99e8adca8b5` |
| `src/gobby/storage/tasks/_creation.py:145-158` | 150&#124;     validation_criteria: str &#124; None = None, | `dcd617950128c510001e0158ca97ae36ea77fbf740e4bd353362405f082b470c` |
| `src/gobby/storage/tasks/_creation.py:161-174` | 166&#124;     validation_criteria = require_validation_criteria(task_type, validation_criteria) | `9c53ed7133b3f1e589765eb8d59e1b0c2eb0a36e101e28c6880f63eab095c0bd` |
| `src/gobby/storage/tasks/_creation.py:187-200` | 192&#124;                     validation_criteria, review_fail_count, | `0f290a223b32e4899e24b341fced6255d67702db38131fbba9595e24dfeef5c8` |
| `src/gobby/storage/tasks/_creation.py:205-218` | 210&#124;                     validation_criteria, | `cc4f96a9517f6b033be00ba57496b9b554ae6bb285782fd7eb86a7d411edf40e` |
| `src/gobby/storage/tasks/_decomposition.py:17-30` | 22&#124;         validation_criteria: str &#124; None = None, | `9544e6f7a5c0e55e7da3c8a5d83c5be818eb2a0a5dff7d3f29f141b303016616` |
| `src/gobby/storage/tasks/_decomposition.py:35-48` | 40&#124;             validation_criteria: Observable acceptance criteria for every non-epic task. | `c32f36eb538538bb1621a896aea06dd2ae22b390afc4ac323b77a2142d464615` |
| `src/gobby/storage/tasks/_decomposition.py:66-76` | 71&#124;             validation_criteria=validation_criteria, | `72079feb10ce072a9b60a6a5b8f59964ec5661a651f925ea7b25e37566a17b58` |
| `src/gobby/storage/tasks/_manager.py:174-187` | 179&#124;         validation_criteria: str &#124; None = None, | `828a05d00a9d9dd125e53d072c90c3c1987bc9b51e39f344c53e4b2485c84fa3` |
| `src/gobby/storage/tasks/_manager.py:193-206` | 198&#124;             validation_criteria=validation_criteria, | `59ce5b5476696349483c051ed9c4f4809a26b8784776987aa346a1a77ece696b` |
| `src/gobby/storage/tasks/_manager.py:213-226` | 218&#124;         validation_criteria: str &#124; None = None, | `6dfd281db2b4fb1fd52a40fe4b5b93e838f5864494f5b5b3c1b67d4b44f6008f` |
| `src/gobby/storage/tasks/_manager.py:231-244` | 236&#124;             validation_criteria=validation_criteria, | `a38e2e2836c3e54362d254847000875a156b2b45433b43f64f3c4425b8e901be` |
| `src/gobby/storage/tasks/_models.py:276-289` | 281&#124;     validation_criteria: str &#124; None = None | `4aa77d88985d12385f787c45f3e004877749631a6b7825fab0c9c48ee7e89314` |
| `src/gobby/storage/tasks/_models.py:360-374` | 365&#124;             validation_criteria=( | `3a562e231b15e78d1afed698f259e548651bf965882ee367d955f978db348147` |
| `src/gobby/storage/tasks/_models.py:421-434` | 426&#124;             "validation_criteria": self.validation_criteria, | `0fc56a325338839fc474f0887697d9bab5ea56b1a9589b6b1e024a5f96892f04` |
| `src/gobby/storage/tasks/_update_facade.py:6-19` | 11&#124; from gobby.tasks.criteria_text import require_validation_criteria | `61e73ab5e4d7c8344c433223d8834798a5391be2cb57de6b469bc843d3658136` |
| `src/gobby/storage/tasks/_update_facade.py:39-52` | 44&#124;         validation_criteria: MaybeUnset[str &#124; None] = UNSET, | `e3c9b0977c7a7ca161874fc713c271b887ae40f778c7775491795fde7e4c6919` |
| `src/gobby/storage/tasks/_update_facade.py:59-88` | 64&#124;         if task_type is not UNSET or validation_criteria is not UNSET: | `18a1b4815b27811b7afc3972b7db8aea7993347dc75c898a71fa5dec2745b86b` |
| `src/gobby/storage/tasks/_update_facade.py:104-117` | 109&#124;                 validation_criteria=validation_criteria, | `ad5849b6aaa7b1cdfb27e2acfedbf62172957d7d91ade87ca6e1a08ab6afe4bc` |
| `src/gobby/storage/tasks/_updates.py:88-101` | 93&#124;     validation_criteria: MaybeUnset[str &#124; None] = UNSET, | `e68e54af1bda85e9bed720176455fb2b4b93d46fb1d8b78ccb7cb7392d004ab2` |
| `src/gobby/storage/tasks/_updates.py:156-171` | 161&#124;     if validation_criteria is not UNSET: | `74a8fa0f90e41b5b83b11abf46db96b236ad5e4aed8136e53c7f4eca314a27f6` |
| `src/gobby/storage/tasks/_updates.py:287-300` | 292&#124;     validation_criteria: MaybeUnset[str &#124; None] = UNSET, | `e68e54af1bda85e9bed720176455fb2b4b93d46fb1d8b78ccb7cb7392d004ab2` |
| `src/gobby/storage/tasks/_updates.py:354-367` | 359&#124;         validation_criteria=validation_criteria, | `acba1efc3092848b0bf006f228c130e0247c55ef50008634e20874e9fb0f30aa` |
| `src/gobby/sync/task_review_serialization.py:13-37` | 18&#124;     validation_criteria: str &#124; None | `39b6520edd913f3271679f85ceb1425cb74c125b4d30870fbf687dcbc6905869` |
| `src/gobby/sync/task_review_serialization.py:85-92` | 90&#124;         "validation_criteria": value.get("criteria"), | `8a918d471641121abfc46fc4de6dc701486e9da8fffd44c7d62862deed0f932e` |
| `src/gobby/sync/tasks.py:10-23` | 12&#124;     deserialize_task_step_records, | `3fbc618d5d61c6d897b9606a2bf704482bc283705e6baf0cffddc6e8f0df1426` |
| `src/gobby/sync/tasks.py:545-560` | 550&#124;                         validation_criteria = require_validation_criteria( | `2d2065e1b917d55cb490d71620001f4fe99bf61828ae748bb437f774b0ce0df9` |
| `src/gobby/sync/tasks.py:605-618` | 610&#124;                             "validation_criteria": validation_criteria, | `070fcb5acb962a09e1df5ac6d1b2b2ce50d2af791c83b43b71ce4661f55ec243` |
| `src/gobby/tasks/close_invariants.py:1-100` | 17&#124;     (5, "step_records"), | `9946075125f304a2bee8153612607b283f41e42b771df8da98a6c56a3848afde` |
| `src/gobby/tasks/commits.py:443-463` | 448&#124;     """Extract file paths mentioned in task title, description, and validation_criteria. | `ad09892e7fae66b12ca094d00f33ac3d47e7098409f5e1cd91823dbcc31dd095` |
| `src/gobby/tasks/commits.py:467-481` | 472&#124;     if task.get("validation_criteria"): | `6c3d66e3a03a88b3e8292e6aec9ae0bd6315fa55a2f1b7a6d3cdd8e862c2b4f6` |
| `src/gobby/tasks/commits.py:512-537` | 517&#124;         task: Task dictionary with title, description, and optionally validation_criteria. | `fd97d961541e3e60e4a1f71f0b430304988e68bac5ec83eb705a248b7b235f9a` |
| `src/gobby/tasks/criteria_text.py:5-26` | 10&#124; def normalized_validation_criteria(value: str &#124; None) -> str &#124; None: | `06f04134451a221157702e97074c163beaf04b615b2e3132d10fd4b8e1c17cd4` |
| `src/gobby/tasks/expansion/_apply.py:173-186` | 178&#124;                     validation_criteria=epic_validation, | `2378e64ebce9c42ca10df6233e7f4d9feb53e593cce9b803e80b8ca2e830e10e` |
| `src/gobby/tasks/expansion/_apply.py:229-242` | 234&#124;                     validation_criteria=task_item.get("validation"), | `0f7f0be27ce2ed8ee1e486f6762e9865861ae37978fa27af6fdb16cc9e600e20` |
| `src/gobby/tasks/expansion/_compile.py:392-405` | 397&#124;             "validation": task_item.get("validation") or task_item.get("validation_criteria"), | `74c3d9010bb5353573af968f32efbd166d280dec5dd1ef6cec577c86ff06c93e` |
| `src/gobby/tasks/expansion/_compile.py:497-510` | 502&#124;         "validation_criteria": task.validation_criteria, | `28580858ede9bee6bf0749cea5bbc866c01e92836ca4bb7550f84d6bc12c1971` |
| `src/gobby/tasks/expansion/_compile.py:527-540` | 532&#124;             task.validation_criteria, | `8ab1878ab5cc72f37e2bcd7d5dd8131222b63c3486c9089bbc5c7de5764f26d2` |
| `src/gobby/tasks/expansion/_contract.py:242-255` | 247&#124;     validation = _contract_validation_criteria(entry, section) | `2eefd80f0bc6eee157ea9c950c16b24a3567376444649c313ac3d941e0a2ab67` |
| `src/gobby/tasks/expansion/_contract.py:242-310` | 247&#124;     validation = _contract_validation_criteria(entry, section) | `d614dbdfe4599e8feb18ebc7507b10c29e8fe7f6993e3d9bc473ddb594d37b50` |
| `src/gobby/tasks/expansion/_contract.py:273-287` | 278&#124; def _contract_validation_criteria(entry: ManifestEntry, section: PlanSection) -> str: | `46d79ac65229ef8127a72f8497cea1e84487091a946a20a43d0ad4135b0f959b` |
| `src/gobby/tasks/expansion/_deferrals.py:109-122` | 114&#124;             validation_criteria=validation, | `a8d313cea9e7a04707f6701bf8cc4e6ea9f53cd822513a07d4abb637af1f0cf9` |
| `src/gobby/tasks/expansion/_reconcile.py:260-273` | 265&#124;         validation_criteria=task_item.get("validation"), | `11af737ef12e4723e3daceffd4dd9251549830d33a81aaee76d06629930c3535` |
| `src/gobby/tasks/generation_schemas.py:40-75` | 40&#124;             "type": "array", | `78a49b6ba26e9c85a808ce4709c9f6efb233ff33556e03d0a95b7111fe413d7d` |
| `src/gobby/tasks/generation_schemas.py:104-168` | 104&#124; TASK_CLOSE_VALIDATION_SCHEMA: dict[str, Any] = { | `af1991d04e0b375de91de0858f650ae8af115a4819306e7e30457fd8f93d8829` |
| `src/gobby/tasks/import_criteria.py:1-11` | 6&#124; def external_issue_validation_criteria(source: str, reference: str) -> str: | `7e19f88a7af4aacb33bc68dad8ab4b768b35ac8272110567129257d9923af282` |
| `src/gobby/tasks/land_commit.py:374-402` | 385&#124;     if independent_review_approval(task, records, sha, caller) is None: | `764564474a94429b1edc21a2a938056c82d276cc6d8290ff53ad360abeda7a24` |
| `src/gobby/tasks/land_commit.py:510-555` | 530&#124;                 records = list_task_step_records(db, task.id) | `0d442d4d626a533836bdd30b566b912fd05132c9afc8e86f1485a685275110bd` |
| `src/gobby/tasks/prompts/expand-task.md:12-25` | 17&#124; &#124; validation_criteria &#124; string &#124; No &#124; How to verify this subtask is complete &#124; | `58654c8a7c1b0224523805f134706d3a5f13fc67d3284a7087aed23b66df4176` |
| `src/gobby/tasks/prompts/expand-task.md:28-59` | 33&#124;       "validation_criteria": "Run migrations and verify tables exist" | `0c4cef106a1ee25359a9bb6d80f72d9ae0183f39ee87350f4197af8a9f62d4f4` |
| `src/gobby/tasks/prompts/expand-task.md:64-86` | 69&#124; 4. **Testing**: Every coding subtask should have a category and validation_criteria. | `e8bd46d4cbaaed56e33707ea3300baf8e4b71ecf8d2bdacda2faf07c48ef9d14` |
| `src/gobby/tasks/related_tests.py:55-74` | 60&#124;     validation_criteria: str &#124; None = None, | `bc3bf48bf74190e81887f42c038e5deb906d94f3f377ed2c390be9418c959799` |
| `src/gobby/tasks/step_records.py:168-283` | 186&#124; def independent_review_approval( | `6996e5dd17e375e4ee799bbb44f087dda12c9baeb3b552b40d52e49fdfd830e1` |
| `src/gobby/tasks/tree_builder.py:172-199` | 177&#124;         validation_criteria = node.get("validation_criteria") | `ebc446caecbfe030b4a93e701c676f9d4b7258653945da94110aa4863d40c9cc` |
| `src/gobby/workflows/claimed_task_extra_skills.py:37-55` | 42&#124;     validation_criteria: str &#124; None, | `f3e38dc66b7768729658f06b3391f01a1b19998f0d1cafe5eecfaaf457ee7d2e` |
| `src/gobby/workflows/claimed_task_extra_skills.py:70-100` | 75&#124;             validation_criteria = _string_field(task, "validation_criteria") | `d8a7470cfc18a9e339fa457f71c0a1fc6903696f7a9d339708dac3ace94bd9fc` |
| `web/src/components/activity/TasksTabActions.ts:15-28` | 20&#124;   validation_criteria?: string; | `bc08173c4b50288f1a896dc247983152d39060cf7326cd00fadd6abc30d36fc6` |
| `web/src/components/activity/taskFieldRouting.ts:13-26` | 18&#124;   "validation_criteria", | `960bbd9ba9b0a954bd7262b86f5b90a2b7b5a2094035206bf8dfc7022b5f1388` |
| `web/src/components/activity/taskFieldRouting.ts:41-54` | 46&#124;  *                category/labels/validation_criteria) | `b0abe0b8d59c89a270bc93e8b63d3b1f964470e44129467af715c2327a3291e1` |
| `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:47-64` | 52&#124;   const validationCriteria = task.validation_criteria ?? ""; | `ae4f014155060988aff6f8cd18a9d0553c8462d12a6ab2abc38690ce121dd233` |
| `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:47-95` | 52&#124;   const validationCriteria = task.validation_criteria ?? ""; | `6ed0d9ac9501f0f34090eaa7b33e2b53f1515d646e9f308148981b72269af945` |
| `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:79-93` | 84&#124;         {validationCriteria && ( | `422aea253e4844c6b32a0c26268a9190894ed43ddca24035bc9b9ede371e262b` |
| `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:174-195` | 179&#124;           value={validationCriteria} | `d0221d4860e2ce9d6816c51d6c8911da1f599e8846a791f4c7790d2badfca47c` |
| `web/src/components/tasks/TaskCreateForm.tsx:19-32` | 24&#124;   validationCriteria?: string; | `66c8f7dbf2d4acc037167cba42112907979ffc582513fa62d87b0dbe2d5a409d` |
| `web/src/components/tasks/TaskCreateForm.tsx:40-53` | 45&#124;   validation_criteria?: string; | `c7533b388b7f4f7514d4ee3a2529b55e43ad3b94d183834e5b8f20e365fc9238` |
| `web/src/components/tasks/TaskCreateForm.tsx:60-73` | 65&#124;   const [validationCriteria, setValidationCriteria] = useState(""); | `769f92f81e2f78b886ff2918e43eb9830bd7749bc335c0661b2a9e9e6693228b` |
| `web/src/components/tasks/TaskCreateForm.tsx:85-98` | 90&#124;     validationCriteria !== (defaults?.validationCriteria &#124;&#124; ""); | `609c242e6793470e9c4e1ddd53ad1bf2f15a87941a1e67ba4446b04636e0015f` |
| `web/src/components/tasks/TaskCreateForm.tsx:111-124` | 116&#124;       setValidationCriteria(defaults?.validationCriteria &#124;&#124; ""); | `552b7a16af09f37d53e5848581a7560dd7bfc59d8222e44ad44be5fcab70323f` |
| `web/src/components/tasks/TaskCreateForm.tsx:138-152` | 143&#124;       if (validationCriteria.trim()) | `2d94579b1fae4863d35a0c55ff87733d16581122fdff680c7f1afd7d818131c6` |
| `web/src/components/tasks/TaskCreateForm.tsx:159-172` | 164&#124;       validationCriteria, | `d7dbca87ad52f8e0c67d26fae3096038df10409e4a4da1d0a568ddcb21a98776` |
| `web/src/components/tasks/TaskCreateForm.tsx:326-339` | 331&#124;                   value={validationCriteria} | `5a65a6e161bf180d3ca2d83ed833b3488aa5ffe0e2b4fe629ae1bbe2534febd4` |
| `web/src/lib/taskNormalization.ts:27-40` | 32&#124;   validation_criteria?: string &#124; null; | `b88c02ccbc062e8af7c2153f494e3da524a9db3b8549d0738d809f1aae066c9e` |
| `web/src/lib/taskNormalization.ts:113-126` | 118&#124;   validation_criteria: isOptionalString, | `d335765f5b96ca50db40c55477d8778769725b548c3a7e167becfed7788465b7` |
| `web/src/types/tasks.ts:73-86` | 78&#124;   validation_criteria: string &#124; null; | `63aae75850b8acb8302e379999e574f5d99c511f26a22768c0f9ff256c3148d3` |

#### Evidence artifact retention

The complete inventories follow in this Markdown commit. The lossless evidence archive remains at `.gobby/plans/research/validation-criteria-25420-evidence.jsonl.gz` (144,113 bytes; SHA-256 `df917be11cd53036362d62de24e79dc001274d67ed369af167f713956478f2e7`). Its uncompressed JSONL SHA-256 is `cbbf74f93189de3f5b5ea45ab6434810ad26135fd17ac66cc757019e88d421e6`. The range/excerpt-hash index above remains committed; the archive and separate inventory files stay uncommitted under the Orchestrator's Markdown-only ruling and cleanup ownership.

#### Appendix: validation-criteria-25420-inventory.txt

```text
crates/gcode/src/communities/refresh_tests.rs:481:                "INSERT INTO tasks(id, project_id, title, claimed_by_session_id, validation_criteria)
crates/gcore/assets/schema/baseline.sql:3889:    validation_criteria text,
crates/gcore/assets/schema/baseline.sql:4669:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
crates/gcore/assets/schema/catalog.manifest.json:4800:      "name": "tasks.validation_criteria",
crates/gcore/assets/schema/catalog.manifest.json:7194:      "name": "tasks.tasks_require_validation_criteria",
crates/gcore/assets/schema/catalog.manifest.json:7195:      "definition": "CHECK (task_type = 'epic'::text OR NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL) NOT VALID"
crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:45:ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria;
crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:50:ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria
crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:51:    CHECK (task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL) NOT VALID;
crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:26:ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria;
crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:58:ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria
crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:59:    CHECK (task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL) NOT VALID;
crates/gcore/src/schema/runner_tests.rs:1436:            "INSERT INTO tasks(id, project_id, title, validation_criteria, closed_at, closed_reason) \
crates/gcore/src/schema/runner_tests.rs:1448:            "INSERT INTO tasks(id, project_id, title, validation_criteria, labels) \
crates/gcore/src/schema/runner_tests.rs:1518:            "INSERT INTO tasks(id, project_id, title, validation_criteria, validation_status, \
crates/gcore/src/schema/runner_tests.rs:1634:    client.batch_execute("ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria")?;
crates/gcore/src/schema/runner_tests.rs:1636:        "INSERT INTO tasks(id, project_id, title, task_type, validation_criteria) \
crates/gcore/src/schema/runner_tests.rs:1641:        "INSERT INTO tasks(id, project_id, title, task_type, validation_criteria, closed_at, \
crates/gcore/src/schema/runner_tests.rs:1646:        "ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria CHECK \
crates/gcore/src/schema/runner_tests.rs:1647:         (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) \
crates/gcore/src/schema/runner_tests.rs:1657:            "SELECT validation_criteria, review_status, closed_reason, state_bucket \
crates/gcore/src/schema/runner_tests.rs:1668:         WHERE conrelid = 'tasks'::regclass AND conname = 'tasks_require_validation_criteria'",
crates/gcore/src/schema/runner_tests.rs:1674:        "CHECK (task_type = 'epic'::text OR NULLIF(btrim(validation_criteria), ''::text) \
crates/gcore/src/schema/runner_tests.rs:1811:        "INSERT INTO tasks(id, project_id, title, validation_criteria) \
crates/gcore/src/schema/runner_tests.rs:1949:            "INSERT INTO tasks(id, project_id, title, isolation, validation_criteria) \
docs/archive/tasks_decomposition.md:63:| `generate_validation_criteria` | 632-694 | Generate criteria via AI |
docs/contracts/plan-coverage.md:560:  validation_criteria: <one-line pass/fail>
docs/contracts/plan-coverage.md:578:| `validation_criteria` | str | One-line pass/fail |
docs/design/task-authority-audit.md:77:| update_task | none | rewrite foreign `validation_criteria`, `affected_files`, parent, priority | G |
docs/evidence/flatten-baseline-419/new.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-419/new.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-419/old.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-419/old.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-420/canonical.normalized.sql:382:ALTER TABLE __schema__.tasks ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-420/canonical.normalized.sql:856:CREATE TABLE __schema__.tasks (CONSTRAINT tasks_allow_automation_check CHECK ((allow_automation = ANY (ARRAY[false, true]))), CONSTRAINT tasks_blocked_by_merge_check CHECK ((blocked_by_merge = ANY (ARRAY[false, true]))), CONSTRAINT tasks_implementation_domain_check CHECK (((implementation_domain IS NULL) OR (implementation_domain = ANY (ARRAY['backend'::text, 'frontend'::text, 'fullstack'::text])))), CONSTRAINT tasks_is_escalated_check CHECK ((is_escalated = ANY (ARRAY[false, true]))), CONSTRAINT tasks_isolation_check CHECK ((isolation = ANY (ARRAY['none'::text, 'worktree'::text, 'clone'::text]))), CONSTRAINT tasks_merge_in_progress_check CHECK ((merge_in_progress = ANY (ARRAY[false, true]))), CONSTRAINT tasks_state_bucket_check CHECK ((state_bucket = ANY (ARRAY['ready'::text, 'in_progress'::text, 'needs_review'::text, 'review_approved'::text, 'closed'::text, 'escalated'::text]))), CONSTRAINT tasks_unattended_check CHECK ((unattended = ANY (ARRAY[false, true]))), CONSTRAINT tasks_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'invalid'::text, 'error'::text]))), additional_skills jsonb, allow_automation boolean DEFAULT false NOT NULL, assigned_agent text, blocked_by_merge boolean DEFAULT false NOT NULL, category text, claimed_by_session_id uuid, closed_at timestamp with time zone, closed_commit_sha text, closed_in_session_id uuid, closed_reason text, commits jsonb, compacted_at timestamp with time zone, created_at timestamp with time zone DEFAULT now() NOT NULL, created_in_session_id uuid, description text, dispatch_failure_count integer DEFAULT 0, due_date date, escalated_at timestamp with time zone, escalation_reason text, github_issue_number integer, github_pr_number integer, github_repo text, id uuid NOT NULL, implementation_domain text, is_escalated boolean DEFAULT false NOT NULL, isolation text DEFAULT 'worktree'::text NOT NULL, labels jsonb, linear_issue_id text, linear_team_id text, merge_in_progress boolean DEFAULT false NOT NULL, parent_task_id uuid, path_cache text, priority integer DEFAULT 2, project_id uuid NOT NULL, seq_num integer, start_date date, state_bucket text DEFAULT 'ready'::text NOT NULL, task_type text DEFAULT 'task'::text, title text NOT NULL, unattended boolean DEFAULT false NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL, validation_criteria text, validation_fail_count integer DEFAULT 0, validation_feedback text, validation_override_reason text, validation_status text);
docs/evidence/flatten-baseline-420/new.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-420/new.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-420/old.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-420/old.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/pre-flatten/migrated-fresh.normalized.sql:314:ALTER TABLE __schema__.tasks ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/pre-flatten/migrated-fresh.normalized.sql:693:CREATE TABLE __schema__.tasks (CONSTRAINT tasks_allow_automation_check CHECK ((allow_automation = ANY (ARRAY[false, true]))), CONSTRAINT tasks_blocked_by_merge_check CHECK ((blocked_by_merge = ANY (ARRAY[false, true]))), CONSTRAINT tasks_implementation_domain_check CHECK (((implementation_domain IS NULL) OR (implementation_domain = ANY (ARRAY['backend'::text, 'frontend'::text, 'fullstack'::text])))), CONSTRAINT tasks_is_escalated_check CHECK ((is_escalated = ANY (ARRAY[false, true]))), CONSTRAINT tasks_isolation_check CHECK ((isolation = ANY (ARRAY['none'::text, 'worktree'::text, 'clone'::text]))), CONSTRAINT tasks_merge_in_progress_check CHECK ((merge_in_progress = ANY (ARRAY[false, true]))), CONSTRAINT tasks_state_bucket_check CHECK ((state_bucket = ANY (ARRAY['ready'::text, 'in_progress'::text, 'needs_review'::text, 'review_approved'::text, 'closed'::text, 'escalated'::text]))), CONSTRAINT tasks_unattended_check CHECK ((unattended = ANY (ARRAY[false, true]))), CONSTRAINT tasks_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'invalid'::text, 'error'::text]))), additional_skills jsonb, allow_automation boolean DEFAULT false NOT NULL, assigned_agent text, blocked_by_merge boolean DEFAULT false NOT NULL, category text, claimed_by_session_id uuid, closed_at timestamp with time zone, closed_commit_sha text, closed_in_session_id uuid, closed_reason text, commits jsonb, compacted_at timestamp with time zone, created_at timestamp with time zone NOT NULL, created_in_session_id uuid, description text, dispatch_failure_count integer DEFAULT 0, due_date date, escalated_at timestamp with time zone, escalation_reason text, github_issue_number integer, github_pr_number integer, github_repo text, id uuid NOT NULL, implementation_domain text, is_escalated boolean DEFAULT false NOT NULL, isolation text DEFAULT 'worktree'::text NOT NULL, labels jsonb, linear_issue_id text, linear_team_id text, merge_in_progress boolean DEFAULT false NOT NULL, parent_task_id uuid, path_cache text, priority integer DEFAULT 2, project_id uuid NOT NULL, seq_num integer, start_date date, state_bucket text DEFAULT 'ready'::text NOT NULL, task_type text DEFAULT 'task'::text, title text NOT NULL, unattended boolean DEFAULT false NOT NULL, updated_at timestamp with time zone NOT NULL, validation_criteria text, validation_fail_count integer DEFAULT 0, validation_feedback text, validation_override_reason text, validation_status text);
docs/guides/cli-commands.md:560:| `tasks create` | `--description`, `--validation-criteria` (required except epics), `--priority`, `--type`, `--depends-on`, `--project` |
docs/guides/cli-commands.md:561:| `tasks update` | `--title`, `--validation-criteria`, `--priority`, `--parent`, `--task-type` |
docs/guides/http-endpoints.md:474:and `validation_criteria`.
docs/guides/spec-writing.md:233:| `validation_criteria` | One-line pass/fail check |
docs/guides/task-expansion.md:327:  validation_criteria: User model exists and tests cover core behavior.
docs/guides/task-expansion.md:338:  validation_criteria: Auth handler exists.
docs/guides/tasks.md:68:    "validation_criteria": "Task examples match registered schemas and all guide links resolve.",
docs/guides/tasks.md:73:`create_task` requires `title`, `category`, and nonempty `validation_criteria`
docs/guides/tasks.md:78:| `code` | Implementation work. Requires `validation_criteria` and `implementation_domain` (`backend`, `frontend`, or `fullstack`). |
docs/guides/tasks.md:396:gobby tasks create "Title" --validation-criteria CRITERIA [-d DESCRIPTION] [-p PRIORITY] [-t TYPE] [-D BLOCKER]
docs/guides/tasks.md:397:gobby tasks update TASK [--title TITLE] [--validation-criteria CRITERIA] [--priority N] [--parent TASK] [--task-type TYPE]
docs/plans/abandoned/UI.md:742:  validation_criteria: string | null;
docs/plans/abandoned/cli-test-battery.md:179:| MCP equivalent | `generate_validation_criteria` | Compare output |
docs/plans/abandoned/drawbridge-enhancements.md:69:| — | `validation_criteria` | `"Visual change matches the annotation screenshot and comment description"` |
docs/plans/abandoned/drawbridge-enhancements.md:150:        validation_criteria="Visual change matches the annotation screenshot and comment description",
docs/plans/abandoned/drawbridge-enhancements.md:301:- `create_task` — supports `task_type: "epic"`, `parent_task_id`, `labels`, `category`, `validation_criteria`
docs/plans/abandoned/ui/phase-0-tui.md:59:- [ ] Display full task details (title, description, validation_criteria)
docs/plans/completed/TASKS.md:58:    validation_criteria TEXT,         -- Natural language prompt for validating completion
docs/plans/completed/TASKS.md:461:    Validate task completion against its validation_criteria.
docs/plans/completed/TASKS.md:481:    - validation_criteria (if set)
docs/plans/completed/TASKS.md:585:  infer_validation: true                # Auto-generate validation_criteria for subtasks
docs/plans/completed/TASKS.md:681:    **Validation Criteria:** {validation_criteria}
docs/plans/completed/TASKS.md:714:1. When a task has `validation_criteria` set, validation can be triggered:
docs/plans/completed/TASKS.md:1441:> Uses configured `task_validation` provider. Validates task completion against `validation_criteria`.
docs/plans/completed/TASKS.md:1446:- [x] Add `validation_criteria` column to tasks table
docs/plans/completed/TASKS.md:1487:- [x] Add `generate_validation_criteria` tool with `--all` support for bulk generation
docs/plans/completed/adversary-convergence.md:88:  section contracts`), 13 (`manifest-validation-criteria-completeness`), 15
docs/plans/completed/adversary-convergence.md:89:  (`manifest-criteria-save-retention`), 16 (`manifest-validation-criteria-fidelity`) —
docs/plans/completed/gobby-tasks-separation-of-concerns.md:22:| 6 | **Validation** | `task_validation.py` | `validate_task`, `get_validation_status`, `reset_validation_count`, `get_validation_history`, `get_recurring_issues`, `clear_validation_history`, `de_escalate_task`, `generate_validation_criteria`, `run_fix_attempt`, `validate_and_fix` | 10 |
docs/plans/completed/gobby-tasks-separation-of-concerns.md:81:| `generate_validation_criteria` | AI-generate criteria |
docs/plans/completed/gobby-tasks-separation-of-concerns.md:85:**Verdict: Mixed concern.** The first 7 tools are task-centric (query/update validation state on the task entity). But `run_fix_attempt` and `validate_and_fix` spawn agents — they're orchestration in disguise. `generate_validation_criteria` calls LLM. This registry mixes data access with workflow execution.
docs/plans/completed/impeccable-ux-agents-draft.md:45:- `src/gobby/install/shared/skills/plan-draft-ux/SKILL.md` — UX authoring methodology. Uses the shared grammar; adds UX-specific deliverable-section archetypes (information architecture, component inventory, interaction model, motion plan, content strategy, visual system). Defines validation_criteria patterns for the new acceptance kinds. Documents the `impeccable shape` steering command as the recommended brief-structuring tool.
docs/plans/completed/impeccable-ux-agents-draft.md:62:- Manifest emission rules updated for UX-flavored entries (e.g., `category: design`, validation_criteria patterns specific to design kinds).
docs/plans/completed/review-signal-learning.md:207:exists and the tier/target escalates, **`update_task`** its `description`, `validation_criteria`,
docs/plans/completed/review-signal-learning.md:213:IDs**; `validation_criteria` = guardrail acceptance; labels `guardrail`, `review-learning`,
docs/plans/completed/review-signal-learning.md:313:  validation_criteria/labels) on an existing open `pattern:<key>` task; non-promotable never counts.
docs/plans/completed/task-expansion-v2.md:413:    validation_criteria: str  # Generated here, not in create_task
docs/plans/completed/task-expansion-v2.md:452:        Generate validation_criteria field (moved from create_task)
docs/plans/completed/task-expansion-v2.md:471:          "validation_criteria": "## Deliverable\n- [ ] ...",
docs/plans/completed/task-expansion-v2.md:499:6. Parent's `validation_criteria` updated to "All child tasks completed" (parent becomes container)
docs/plans/completed/task-expansion-v2.md:510:    Parent's validation_criteria updated to "All child tasks completed".
docs/plans/completed/task-expansion-v2.md:639:    original_criteria = task.validation_criteria
docs/plans/completed/task-expansion-v2.md:644:        validation_criteria=TDD_CRITERIA_RED,  # Templated
docs/plans/completed/task-expansion-v2.md:651:        validation_criteria=original_criteria,  # Inherited from parent
docs/plans/completed/task-expansion-v2.md:659:        validation_criteria=TDD_CRITERIA_BLUE,  # Templated
docs/plans/completed/task-expansion-v2.md:665:    task.validation_criteria = PARENT_CRITERIA
docs/plans/completed/task-expansion-v2.md:692:    - Green (implement): Inherits parent's original validation_criteria
docs/plans/completed/task-expansion-v3.md:229:            validation_criteria=subtask.get("validation"),
docs/reference-audit/tasks.json:1431:      "finding": "Docs creation example omitted validation_criteria and implied only code required criteria.",
docs/reference-audit/tasks.json:1432:      "evidence": "create_task registered schema; _crud._task_invariant_error; criteria_contract.require_validation_criteria",
docs/reviews/TRIAGE.md:742:- #16202 (U496, `docs/reviews/mcp_proxy-tools.md`) — Enforce code-task validation_criteria invariant in update_task
docs/reviews/TRIAGE.md:1160:- #16873 (U855, `docs/reviews/tasks.md`) — Treat missing validation_criteria on leaf code tasks as a hard validation gate
docs/reviews/llm-prompts.md:99:- **Where:** `src/gobby/tasks/expansion/_compile.py:457` (`normalized = file_path.lstrip("./")`), `:459,472` (`repo_path / normalized`); paths from `extract_mentioned_files` (`src/gobby/tasks/commits.py:391`) over `task.title`/`description`/`validation_criteria`
docs/reviews/mcp_proxy-tools.md:268:### [IMPORTANT] `update_task` bypasses the category='code' ⇒ validation_criteria invariant
docs/reviews/mcp_proxy-tools.md:270:- **Failure mode:** `create_task` rejects code tasks without criteria/domain; `update_task` sets `category="code"` with no check and accepts `validation_criteria=""`. A code task without criteria skips LLM validation on close (`_lifecycle_close.py:270`).
docs/reviews/quality-subsystems.md:165:- **Where:** `src/gobby/review_learning/promotion.py:242-257` (`_find_existing_task` matches only `pattern:{key}` + group labels — decision/target ignored), `:213-226` (update rewrites title/category/target/validation_criteria), `:225` (clears `implementation_domain` via explicit `None` → SET NULL at `src/gobby/storage/tasks/_updates.py:158`)
docs/reviews/tasks.md:29:### [IMPORTANT] LLM validation gate is silently skipped whenever `task.validation_criteria` is empty
docs/reviews/tasks.md:30:- **Where:** `mcp_proxy/tools/tasks/_lifecycle_close.py:270` (`if is_leaf and ctx.task_validator and task.validation_criteria:`); criteria is required only at `_crud.py:115` for `category=="code"`; expansion sets it from an optional manifest field (`expansion/_apply.py:201`, may be None); storage `create_task` doesn't enforce it.
docs/reviews/tasks.md:31:- **Failure mode:** A leaf task reaching close with falsy `validation_criteria` bypasses the entire LLM validation block. Expansion-created code tasks (manifest omits `validation`), dispatch/build-created tasks, non-`code` categories, and storage-direct creations can all close with zero LLM validation — the commit/edit checks still run, so broken code can be committed and the task closed without the validator ever evaluating the diff. The TaskValidator-side analogue of the upstream close-gate poisoning.
docs/reviews/tasks.md:55:- **Where:** `expansion/_compile.py:443-475` (`_build_file_context`) — paths from `extract_mentioned_files(task_payload)` over title/description/validation_criteria; normalized only with `lstrip("./")` (`:455`) then `repo_path / normalized` (`:458`), read up to 3500 chars (`_common.py:349`) with **no containment check**.
docs/reviews/tasks.md:106:1. **Gates keyed on session-global flags or optional data, not task-scoped truth.** `task_has_commits` is a per-session boolean any of three tools can set (the Blocker), never checked against the closing task. The LLM validation gate hinges on a free-text `validation_criteria` enforced at one boundary for one category. Both are "absence/optional treated as permission," echoing the upstream "absence-of-signal = success" findings.
src/gobby/agents/team_teardown.py:666:            validation_criteria=(
src/gobby/agents/team_teardown.py:710:            validation_criteria=(
src/gobby/cli/tasks/_crud_mutations.py:10:from gobby.tasks.criteria_text import TaskCriteriaError, require_validation_criteria
src/gobby/cli/tasks/_crud_mutations.py:18:    validation_criteria: str | None,
src/gobby/cli/tasks/_crud_mutations.py:32:        validation_criteria = require_validation_criteria(task_type, validation_criteria)
src/gobby/cli/tasks/_crud_mutations.py:55:            validation_criteria=validation_criteria,
src/gobby/cli/tasks/_crud_mutations.py:100:    validation_criteria: str | None,
src/gobby/cli/tasks/_crud_mutations.py:121:    if validation_criteria is not None:
src/gobby/cli/tasks/_crud_mutations.py:122:        kwargs["validation_criteria"] = validation_criteria
src/gobby/cli/tasks/crud.py:166:    "--validation-criteria",
src/gobby/cli/tasks/crud.py:176:    validation_criteria: str | None,
src/gobby/cli/tasks/crud.py:194:        validation_criteria=validation_criteria,
src/gobby/cli/tasks/crud.py:215:@click.option("--validation-criteria", help="New observable completion criteria")
src/gobby/cli/tasks/crud.py:239:    validation_criteria: str | None,
src/gobby/cli/tasks/crud.py:258:        validation_criteria,
src/gobby/feedback/actions.py:71:        validation_criteria: str | None = ...,
src/gobby/feedback/actions.py:369:            validation_criteria=(
src/gobby/install/shared/prompts/validation/criteria.md:2:name: validation-criteria
src/gobby/install/shared/skills/gobby/references/tasks/creation.md:11:Every task type except `epic` requires meaningful `validation_criteria`, including
src/gobby/install/shared/skills/gobby/references/tasks/creation.md:26:    "validation_criteria": "Task examples match registered schemas and all links resolve.",
src/gobby/install/shared/workflows/pipelines/team-planning.yaml:148:        validation_criteria: "The plan file is decision-complete, passes gobby plans validate, and carries its consensus and manifest stamps."
src/gobby/mcp_proxy/tools/tasks/_crud.py:36:from gobby.tasks.criteria_text import TaskCriteriaError, require_validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:51:    validation_criteria: str | None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:54:    supplied_validation_criteria: str | None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:58:        require_validation_criteria(task_type, validation_criteria)
src/gobby/mcp_proxy/tools/tasks/_crud.py:61:    if supplied_validation_criteria is not None:
src/gobby/mcp_proxy/tools/tasks/_crud.py:62:        findings = (*malformed_test_reference_findings(supplied_validation_criteria),)
src/gobby/mcp_proxy/tools/tasks/_crud.py:94:        validation_criteria: str | None = None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:118:            validation_criteria: Acceptance criteria for validating completion.
src/gobby/mcp_proxy/tools/tasks/_crud.py:180:            validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:182:            supplied_validation_criteria=validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:231:            "validation_criteria": validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:372:        brief="Create a new task. Requires: title, session_id, category, validation_criteria (every task_type except 'epic')",
src/gobby/mcp_proxy/tools/tasks/_crud.py:418:                    "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing tests), 'test' (test-writing), 'research' (investigation), 'planning' (design/architecture), or 'manual' (manual verification). Category does not affect the validation_criteria requirement, which applies to every task_type except 'epic'.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:421:                "validation_criteria": {
src/gobby/mcp_proxy/tools/tasks/_crud.py:511:        description="Get task details including dependencies. Task ID can be #N (e.g., #1), path (e.g., 1.2.3), or UUID. Returns an actionable card by default, including description, validation_criteria, labels, state, and dependency summaries; set brief=false for the full task record.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:521:                    "description": "If true (default), return the actionable card: identity, description, validation_criteria, labels, state, execution settings, and dependency summaries. If false, also return project and session ids, commits, closure, validation, merge and dispatch details, escalation fields, GitHub/Linear links, dates, and full dependency rows.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:537:        validation_criteria: str | None = None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:577:        effective_validation_criteria = (
src/gobby/mcp_proxy/tools/tasks/_crud.py:578:            validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:579:            if validation_criteria is not None
src/gobby/mcp_proxy/tools/tasks/_crud.py:580:            else current_task.validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:590:            effective_validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:592:            supplied_validation_criteria=validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:607:        if validation_criteria is not None:
src/gobby/mcp_proxy/tools/tasks/_crud.py:608:            kwargs["validation_criteria"] = validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:694:                "validation_criteria": {
src/gobby/mcp_proxy/tools/tasks/_crud.py:706:                    "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing tests), 'test' (test-writing), 'research' (investigation), 'planning' (design/architecture), or 'manual' (manual verification). Category does not affect the validation_criteria requirement, which applies to every task_type except 'epic'.",
src/gobby/mcp_proxy/tools/tasks/_formatters.py:159:        "validation_criteria": _task_value(task, "validation_criteria"),
src/gobby/mcp_proxy/tools/tasks/_task_scope.py:176:        for reference in extract_artifact_references(task.validation_criteria or "", kind):
src/gobby/plans/_artifact_refs.py:25:def artifact_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:27:        return _file_referenced(item.artifact_ref, validation_criteria)
src/gobby/plans/_artifact_refs.py:29:        return _symbol_referenced(item.artifact_ref, validation_criteria)
src/gobby/plans/_artifact_refs.py:31:        return _test_referenced(item.artifact_ref, validation_criteria)
src/gobby/plans/_artifact_refs.py:33:        return _behavior_referenced(item, validation_criteria)
src/gobby/plans/_artifact_refs.py:37:def _file_referenced(artifact_ref: str, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:39:    if any(_contains_path_ref(validation_criteria, candidate) for candidate in candidates):
src/gobby/plans/_artifact_refs.py:41:    if any(_path_parts_referenced(validation_criteria, candidate) for candidate in candidates):
src/gobby/plans/_artifact_refs.py:44:    return bool(markers) and all(marker in validation_criteria for marker in markers)
src/gobby/plans/_artifact_refs.py:47:def _symbol_referenced(artifact_ref: str, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:52:        _contains_ref(validation_criteria, candidate) for candidate in candidates if candidate
src/gobby/plans/_artifact_refs.py:56:def _test_referenced(artifact_ref: str, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:57:    if _contains_ref(validation_criteria, artifact_ref):
src/gobby/plans/_artifact_refs.py:61:            _contains_path_ref(validation_criteria, path)
src/gobby/plans/_artifact_refs.py:62:            or _path_parts_referenced(validation_criteria, path)
src/gobby/plans/_artifact_refs.py:64:        and _contains_ref(validation_criteria, test_name)
src/gobby/plans/_artifact_refs.py:69:def _behavior_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:71:        _contains_ref(validation_criteria, ref, case_sensitive=False)
src/gobby/plans/_artifact_refs.py:80:        _contains_path_ref(validation_criteria, candidate)
src/gobby/plans/_artifact_refs.py:81:        or _path_parts_referenced(validation_criteria, candidate)
src/gobby/plans/_artifact_refs.py:86:        _contains_ref(validation_criteria, task_ref, case_sensitive=False)
src/gobby/plans/_task_store.py:22:    validation_criteria: str
src/gobby/plans/_task_store.py:43:            "validation_criteria": record.validation_criteria,
src/gobby/plans/_task_store.py:110:        validation_criteria=task.validation_criteria or task.description or "",
src/gobby/plans/_task_store.py:131:        validation_criteria=_first_string(raw, "validation_criteria", "validation", "description"),
src/gobby/plans/coverage.py:84:    validation_criteria_snippet: str
src/gobby/plans/coverage.py:145:    leaf_validation_criteria: str,
src/gobby/plans/coverage.py:167:    if not artifact_referenced(item, leaf_validation_criteria):
src/gobby/plans/coverage.py:365:        result = validate_covers(record, task.validation_criteria, task.ref, plan_doc)
src/gobby/plans/coverage.py:368:            validation_criteria_snippet=_snippet(task.validation_criteria),
src/gobby/plans/coverage.py:558:                                validation_criteria_snippet=str(exc),
src/gobby/plans/coverage.py:568:            result = validate_covers(record, task.validation_criteria, task.ref, plan_doc)
src/gobby/plans/coverage.py:579:                            validation_criteria_snippet=result.detail,
src/gobby/plans/coverage.py:593:            validation_criteria_snippet="",
src/gobby/plans/coverage.py:598:        validation_criteria_snippet=str(raw.get("validation_criteria_snippet", "")),
src/gobby/plans/coverage.py:764:                "validation_criteria": record.validation_criteria,
src/gobby/plans/coverage_manifest.py:424:        "validation_criteria_snippet": _attr(leaf, "validation_criteria_snippet"),
src/gobby/plans/deferral.py:18:    "validation_criteria_does_not_duplicate",
src/gobby/plans/deferral.py:100:    validation_criteria = _task_validation_criteria(task)
src/gobby/plans/deferral.py:102:        if not artifact_referenced(item, validation_criteria):
src/gobby/plans/deferral.py:108:                "validation_criteria_does_not_duplicate",
src/gobby/plans/deferral.py:172:def _task_validation_criteria(task: dict[str, Any]) -> str:
src/gobby/plans/deferral.py:173:    value = task.get("validation_criteria", "")
src/gobby/plans/manifest_emitter.py:150:        entry["validation_criteria"] = "\n".join(
src/gobby/plans/manifest_emitter.py:171:                    str(entry["validation_criteria"]),
src/gobby/plans/manifest_emitter.py:398:        "validation_criteria": validation,
src/gobby/plans/manifest_parser.py:23:    "validation_criteria",
src/gobby/plans/manifest_parser.py:34:    validation_criteria: str
src/gobby/plans/manifest_parser.py:233:        validation_criteria=str(raw["validation_criteria"]),
src/gobby/projects/purge.py:108:# tasks_require_validation_criteria is NOT VALID: legacy rows without criteria
src/gobby/projects/purge.py:112:    "validation_criteria = CASE"
src/gobby/projects/purge.py:113:    " WHEN task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL"
src/gobby/projects/purge.py:114:    " THEN validation_criteria"
src/gobby/servers/routes/agent_spawn.py:149:    if task.validation_criteria:
src/gobby/servers/routes/agent_spawn.py:150:        parts.append(f"\n### Validation Criteria\n{task.validation_criteria}")
src/gobby/servers/routes/tasks.py:65:    validation_criteria: str | None = Field(default=None, description="Acceptance criteria")
src/gobby/servers/routes/tasks.py:96:    validation_criteria: str | None = Field(default=None, description="New validation criteria")
src/gobby/servers/routes/tasks.py:316:                validation_criteria=request_data.validation_criteria,
src/gobby/storage/tasks/_close.py:17:from gobby.tasks.criteria_text import normalized_validation_criteria
src/gobby/storage/tasks/_close.py:100:               validation_criteria = CASE
src/gobby/storage/tasks/_close.py:102:                        OR NULLIF(btrim(validation_criteria), '') IS NOT NULL
src/gobby/storage/tasks/_close.py:103:                   THEN validation_criteria
src/gobby/storage/tasks/_close.py:224:                and normalized_validation_criteria(parent["validation_criteria"]) is not None
src/gobby/storage/tasks/_creation.py:24:from gobby.tasks.criteria_text import require_validation_criteria
src/gobby/storage/tasks/_creation.py:41:    validation_criteria: str | None = None,
src/gobby/storage/tasks/_creation.py:64:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_creation.py:83:    validation_criteria: str | None = None,
src/gobby/storage/tasks/_creation.py:104:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_creation.py:150:    validation_criteria: str | None = None,
src/gobby/storage/tasks/_creation.py:166:    validation_criteria = require_validation_criteria(task_type, validation_criteria)
src/gobby/storage/tasks/_creation.py:192:                    validation_criteria, review_fail_count,
src/gobby/storage/tasks/_creation.py:210:                    validation_criteria,
src/gobby/storage/tasks/_decomposition.py:22:        validation_criteria: str | None = None,
src/gobby/storage/tasks/_decomposition.py:40:            validation_criteria: Observable acceptance criteria for every non-epic task.
src/gobby/storage/tasks/_decomposition.py:71:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_manager.py:179:        validation_criteria: str | None = None,
src/gobby/storage/tasks/_manager.py:198:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_manager.py:218:        validation_criteria: str | None = None,
src/gobby/storage/tasks/_manager.py:236:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_models.py:281:    validation_criteria: str | None = None
src/gobby/storage/tasks/_models.py:365:            validation_criteria=(
src/gobby/storage/tasks/_models.py:366:                row["validation_criteria"] if "validation_criteria" in keys else None
src/gobby/storage/tasks/_models.py:426:            "validation_criteria": self.validation_criteria,
src/gobby/storage/tasks/_update_facade.py:11:from gobby.tasks.criteria_text import require_validation_criteria
src/gobby/storage/tasks/_update_facade.py:44:        validation_criteria: MaybeUnset[str | None] = UNSET,
src/gobby/storage/tasks/_update_facade.py:64:        if task_type is not UNSET or validation_criteria is not UNSET:
src/gobby/storage/tasks/_update_facade.py:69:            if validation_criteria is UNSET:
src/gobby/storage/tasks/_update_facade.py:70:                effective_criteria = current_task.validation_criteria
src/gobby/storage/tasks/_update_facade.py:73:                    validation_criteria if isinstance(validation_criteria, str) else None
src/gobby/storage/tasks/_update_facade.py:75:            effective_criteria = require_validation_criteria(
src/gobby/storage/tasks/_update_facade.py:79:            if validation_criteria is not UNSET:
src/gobby/storage/tasks/_update_facade.py:80:                validation_criteria = effective_criteria
src/gobby/storage/tasks/_update_facade.py:109:                validation_criteria=validation_criteria,
src/gobby/storage/tasks/_updates.py:93:    validation_criteria: MaybeUnset[str | None] = UNSET,
src/gobby/storage/tasks/_updates.py:161:    if validation_criteria is not UNSET:
src/gobby/storage/tasks/_updates.py:162:        updates.append("validation_criteria = %s")
src/gobby/storage/tasks/_updates.py:163:        params.append(validation_criteria)
src/gobby/storage/tasks/_updates.py:292:    validation_criteria: MaybeUnset[str | None] = UNSET,
src/gobby/storage/tasks/_updates.py:359:        validation_criteria=validation_criteria,
src/gobby/sync/task_review_serialization.py:18:    validation_criteria: str | None
src/gobby/sync/task_review_serialization.py:29:        "criteria": task.validation_criteria,
src/gobby/sync/task_review_serialization.py:90:        "validation_criteria": value.get("criteria"),
src/gobby/sync/tasks.py:15:from gobby.tasks.criteria_text import require_validation_criteria
src/gobby/sync/tasks.py:550:                        validation_criteria = require_validation_criteria(
src/gobby/sync/tasks.py:552:                            review["validation_criteria"],
src/gobby/sync/tasks.py:610:                            "validation_criteria": validation_criteria,
src/gobby/tasks/commits.py:448:    """Extract file paths mentioned in task title, description, and validation_criteria.
src/gobby/tasks/commits.py:455:        task: Task dictionary with title, description, and optionally validation_criteria.
src/gobby/tasks/commits.py:472:    if task.get("validation_criteria"):
src/gobby/tasks/commits.py:473:        text_parts.append(task["validation_criteria"])
src/gobby/tasks/commits.py:517:        task: Task dictionary with title, description, and optionally validation_criteria.
src/gobby/tasks/commits.py:528:    if task.get("validation_criteria"):
src/gobby/tasks/commits.py:529:        text_parts.append(task["validation_criteria"])
src/gobby/tasks/criteria_text.py:10:def normalized_validation_criteria(value: str | None) -> str | None:
src/gobby/tasks/criteria_text.py:18:def require_validation_criteria(task_type: str, value: str | None) -> str | None:
src/gobby/tasks/criteria_text.py:20:    normalized = normalized_validation_criteria(value)
src/gobby/tasks/criteria_text.py:23:            "Every non-epic task requires nonempty validation_criteria. "
src/gobby/tasks/expansion/_apply.py:178:                    validation_criteria=epic_validation,
src/gobby/tasks/expansion/_apply.py:234:                    validation_criteria=task_item.get("validation"),
src/gobby/tasks/expansion/_compile.py:397:            "validation": task_item.get("validation") or task_item.get("validation_criteria"),
src/gobby/tasks/expansion/_compile.py:502:        "validation_criteria": task.validation_criteria,
src/gobby/tasks/expansion/_compile.py:532:            task.validation_criteria,
src/gobby/tasks/expansion/_contract.py:247:    validation = _contract_validation_criteria(entry, section)
src/gobby/tasks/expansion/_contract.py:278:def _contract_validation_criteria(entry: ManifestEntry, section: PlanSection) -> str:
src/gobby/tasks/expansion/_contract.py:279:    lines = [entry.validation_criteria.strip()]
src/gobby/tasks/expansion/_deferrals.py:114:            validation_criteria=validation,
src/gobby/tasks/expansion/_reconcile.py:265:        validation_criteria=task_item.get("validation"),
src/gobby/tasks/import_criteria.py:6:def external_issue_validation_criteria(source: str, reference: str) -> str:
src/gobby/tasks/prompts/expand-task.md:17:| validation_criteria | string | No | How to verify this subtask is complete |
src/gobby/tasks/prompts/expand-task.md:33:      "validation_criteria": "Run migrations and verify tables exist"
src/gobby/tasks/prompts/expand-task.md:42:      "validation_criteria": "Unit tests for all repository methods pass"
src/gobby/tasks/prompts/expand-task.md:51:      "validation_criteria": "Integration tests for all endpoints pass"
src/gobby/tasks/prompts/expand-task.md:69:4. **Testing**: Every coding subtask should have a category and validation_criteria.
src/gobby/tasks/prompts/expand-task.md:78:For each subtask, generate precise validation criteria in the `validation_criteria` field.
src/gobby/tasks/related_tests.py:60:    validation_criteria: str | None = None,
src/gobby/tasks/related_tests.py:66:    search_text = f"{task_title} {validation_criteria or ''} {task_description or ''}"
src/gobby/tasks/tree_builder.py:177:        validation_criteria = node.get("validation_criteria")
src/gobby/tasks/tree_builder.py:191:                validation_criteria=validation_criteria,
src/gobby/workflows/claimed_task_extra_skills.py:42:    validation_criteria: str | None,
src/gobby/workflows/claimed_task_extra_skills.py:47:    text = (validation_criteria or "").lower()
src/gobby/workflows/claimed_task_extra_skills.py:75:            validation_criteria = _string_field(task, "validation_criteria")
src/gobby/workflows/claimed_task_extra_skills.py:85:                validation_criteria=validation_criteria,
src/gobby/workflows/claimed_task_extra_skills.py:91:            if validation_criteria:
src/gobby/workflows/claimed_task_extra_skills.py:92:                for reference in extract_artifact_references(validation_criteria, "test"):
web/src/components/activity/TasksTabActions.ts:20:  validation_criteria?: string;
web/src/components/activity/__tests__/TasksTab.events.test.tsx:488:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.setup.ts:98:      validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:368:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:460:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:556:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:564:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:662:        validation_criteria: "Verify the lower pane layout",
web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx:164:          validation_criteria: null,
web/src/components/activity/__tests__/fixtures.ts:31:    validation_criteria: null,
web/src/components/activity/taskFieldRouting.ts:18:  "validation_criteria",
web/src/components/activity/taskFieldRouting.ts:46: *                category/labels/validation_criteria)
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:52:  const validationCriteria = task.validation_criteria ?? "";
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:56:    const hasBody = Boolean(description) || Boolean(validationCriteria);
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:84:        {validationCriteria && (
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:85:          <StaticBlock label="Validation criteria" value={validationCriteria} />
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:179:          value={validationCriteria}
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:183:          disabled={edit.isFieldPending(task.id, "validation_criteria")}
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:187:              field: "validation_criteria",
web/src/components/tasks/TaskCreateForm.tsx:24:  validationCriteria?: string;
web/src/components/tasks/TaskCreateForm.tsx:45:  validation_criteria?: string;
web/src/components/tasks/TaskCreateForm.tsx:65:  const [validationCriteria, setValidationCriteria] = useState("");
web/src/components/tasks/TaskCreateForm.tsx:90:    validationCriteria !== (defaults?.validationCriteria || "");
web/src/components/tasks/TaskCreateForm.tsx:116:      setValidationCriteria(defaults?.validationCriteria || "");
web/src/components/tasks/TaskCreateForm.tsx:143:      if (validationCriteria.trim())
web/src/components/tasks/TaskCreateForm.tsx:144:        params.validation_criteria = validationCriteria.trim();
web/src/components/tasks/TaskCreateForm.tsx:164:      validationCriteria,
web/src/components/tasks/TaskCreateForm.tsx:331:                  value={validationCriteria}
web/src/lib/__tests__/taskNormalization.test.ts:14:        validation_criteria: null,
web/src/lib/__tests__/taskNormalization.test.ts:24:      { id: "task-1", validation_criteria: ["run tests"] },
web/src/lib/taskNormalization.ts:32:  validation_criteria?: string | null;
web/src/lib/taskNormalization.ts:118:  validation_criteria: isOptionalString,
web/src/types/tasks.ts:78:  validation_criteria: string | null;
web/tests/fixtures/webChatRestorePlan.ts:44:  validation_criteria: "Refresh keeps claimed task state visible.",
```

#### Appendix: validation-criteria-25420-descriptions.txt

```text
crates/gcode/src/communities/refresh_tests.rs:481:                "INSERT INTO tasks(id, project_id, title, claimed_by_session_id, validation_criteria)
crates/gcore/assets/config/runtime_config_contract.json:6286:      "description": "Pattern name to list of validation criteria templates. Templates can use placeholders like {unit_tests}, {type_check}, {lint}.",
crates/gcore/assets/schema/baseline.sql:3889:    validation_criteria text,
crates/gcore/assets/schema/baseline.sql:4669:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
crates/gcore/assets/schema/catalog.manifest.json:4800:      "name": "tasks.validation_criteria",
crates/gcore/assets/schema/catalog.manifest.json:7194:      "name": "tasks.tasks_require_validation_criteria",
crates/gcore/assets/schema/catalog.manifest.json:7195:      "definition": "CHECK (task_type = 'epic'::text OR NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL) NOT VALID"
crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:45:ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria;
crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:50:ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria
crates/gcore/assets/schema/migrations/476_runbook_review_state.sql:51:    CHECK (task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL) NOT VALID;
crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:26:ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria;
crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:58:ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria
crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:59:    CHECK (task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL) NOT VALID;
crates/gcore/src/schema/runner_tests.rs:1436:            "INSERT INTO tasks(id, project_id, title, validation_criteria, closed_at, closed_reason) \
crates/gcore/src/schema/runner_tests.rs:1448:            "INSERT INTO tasks(id, project_id, title, validation_criteria, labels) \
crates/gcore/src/schema/runner_tests.rs:1518:            "INSERT INTO tasks(id, project_id, title, validation_criteria, validation_status, \
crates/gcore/src/schema/runner_tests.rs:1634:    client.batch_execute("ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria")?;
crates/gcore/src/schema/runner_tests.rs:1636:        "INSERT INTO tasks(id, project_id, title, task_type, validation_criteria) \
crates/gcore/src/schema/runner_tests.rs:1641:        "INSERT INTO tasks(id, project_id, title, task_type, validation_criteria, closed_at, \
crates/gcore/src/schema/runner_tests.rs:1646:        "ALTER TABLE tasks ADD CONSTRAINT tasks_require_validation_criteria CHECK \
crates/gcore/src/schema/runner_tests.rs:1647:         (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) \
crates/gcore/src/schema/runner_tests.rs:1657:            "SELECT validation_criteria, review_status, closed_reason, state_bucket \
crates/gcore/src/schema/runner_tests.rs:1668:         WHERE conrelid = 'tasks'::regclass AND conname = 'tasks_require_validation_criteria'",
crates/gcore/src/schema/runner_tests.rs:1674:        "CHECK (task_type = 'epic'::text OR NULLIF(btrim(validation_criteria), ''::text) \
crates/gcore/src/schema/runner_tests.rs:1811:        "INSERT INTO tasks(id, project_id, title, validation_criteria) \
crates/gcore/src/schema/runner_tests.rs:1949:            "INSERT INTO tasks(id, project_id, title, isolation, validation_criteria) \
crates/gterminal/vendor/libghostty-vt/HACKING.md:415:#### VM Acceptance Criteria
docs/architecture/gobby-v1.0.0-roadmap.md:77:| Ref | Type | Title | Validation criteria (summary) |
docs/architecture/gobby-v1.0.0-roadmap.md:198:- `list_tasks parent_task_id=#21363 label=found-work` returns FW.1–FW.3 with validation criteria set.
docs/architecture/interactive-persona-scope.md:38:Task `#21088` owns the runtime and focused-test changes. Its acceptance criteria cover
docs/archive/tasks_decomposition.md:63:| `generate_validation_criteria` | 632-694 | Generate criteria via AI |
docs/contracts/plan-coverage.md:416:with validation criteria that list the original acceptance items, a `blocked-by`
docs/contracts/plan-coverage.md:432:has concrete scope and validation criteria, and is reasonably expected to fit
docs/contracts/plan-coverage.md:560:  validation_criteria: <one-line pass/fail>
docs/contracts/plan-coverage.md:578:| `validation_criteria` | str | One-line pass/fail |
docs/contracts/plan-coverage.md:631:  validation criteria requiring red, green, refactor/final-green, exact command,
docs/contracts/plan-coverage.md:640:  own acceptance criteria.
docs/design/task-authority-audit.md:77:| update_task | none | rewrite foreign `validation_criteria`, `affected_files`, parent, priority | G |
docs/evidence/flatten-baseline-419/new.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-419/new.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-419/old.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-419/old.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-420/canonical.normalized.sql:382:ALTER TABLE __schema__.tasks ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-420/canonical.normalized.sql:856:CREATE TABLE __schema__.tasks (CONSTRAINT tasks_allow_automation_check CHECK ((allow_automation = ANY (ARRAY[false, true]))), CONSTRAINT tasks_blocked_by_merge_check CHECK ((blocked_by_merge = ANY (ARRAY[false, true]))), CONSTRAINT tasks_implementation_domain_check CHECK (((implementation_domain IS NULL) OR (implementation_domain = ANY (ARRAY['backend'::text, 'frontend'::text, 'fullstack'::text])))), CONSTRAINT tasks_is_escalated_check CHECK ((is_escalated = ANY (ARRAY[false, true]))), CONSTRAINT tasks_isolation_check CHECK ((isolation = ANY (ARRAY['none'::text, 'worktree'::text, 'clone'::text]))), CONSTRAINT tasks_merge_in_progress_check CHECK ((merge_in_progress = ANY (ARRAY[false, true]))), CONSTRAINT tasks_state_bucket_check CHECK ((state_bucket = ANY (ARRAY['ready'::text, 'in_progress'::text, 'needs_review'::text, 'review_approved'::text, 'closed'::text, 'escalated'::text]))), CONSTRAINT tasks_unattended_check CHECK ((unattended = ANY (ARRAY[false, true]))), CONSTRAINT tasks_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'invalid'::text, 'error'::text]))), additional_skills jsonb, allow_automation boolean DEFAULT false NOT NULL, assigned_agent text, blocked_by_merge boolean DEFAULT false NOT NULL, category text, claimed_by_session_id uuid, closed_at timestamp with time zone, closed_commit_sha text, closed_in_session_id uuid, closed_reason text, commits jsonb, compacted_at timestamp with time zone, created_at timestamp with time zone DEFAULT now() NOT NULL, created_in_session_id uuid, description text, dispatch_failure_count integer DEFAULT 0, due_date date, escalated_at timestamp with time zone, escalation_reason text, github_issue_number integer, github_pr_number integer, github_repo text, id uuid NOT NULL, implementation_domain text, is_escalated boolean DEFAULT false NOT NULL, isolation text DEFAULT 'worktree'::text NOT NULL, labels jsonb, linear_issue_id text, linear_team_id text, merge_in_progress boolean DEFAULT false NOT NULL, parent_task_id uuid, path_cache text, priority integer DEFAULT 2, project_id uuid NOT NULL, seq_num integer, start_date date, state_bucket text DEFAULT 'ready'::text NOT NULL, task_type text DEFAULT 'task'::text, title text NOT NULL, unattended boolean DEFAULT false NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL, validation_criteria text, validation_fail_count integer DEFAULT 0, validation_feedback text, validation_override_reason text, validation_status text);
docs/evidence/flatten-baseline-420/new.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-420/new.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/flatten-baseline-420/old.dump.sql:3772:    validation_criteria text,
docs/evidence/flatten-baseline-420/old.dump.sql:4382:    ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/pre-flatten/migrated-fresh.normalized.sql:314:ALTER TABLE __schema__.tasks ADD CONSTRAINT tasks_require_validation_criteria CHECK (((task_type = 'epic'::text) OR (NULLIF(btrim(validation_criteria), ''::text) IS NOT NULL))) NOT VALID;
docs/evidence/pre-flatten/migrated-fresh.normalized.sql:693:CREATE TABLE __schema__.tasks (CONSTRAINT tasks_allow_automation_check CHECK ((allow_automation = ANY (ARRAY[false, true]))), CONSTRAINT tasks_blocked_by_merge_check CHECK ((blocked_by_merge = ANY (ARRAY[false, true]))), CONSTRAINT tasks_implementation_domain_check CHECK (((implementation_domain IS NULL) OR (implementation_domain = ANY (ARRAY['backend'::text, 'frontend'::text, 'fullstack'::text])))), CONSTRAINT tasks_is_escalated_check CHECK ((is_escalated = ANY (ARRAY[false, true]))), CONSTRAINT tasks_isolation_check CHECK ((isolation = ANY (ARRAY['none'::text, 'worktree'::text, 'clone'::text]))), CONSTRAINT tasks_merge_in_progress_check CHECK ((merge_in_progress = ANY (ARRAY[false, true]))), CONSTRAINT tasks_state_bucket_check CHECK ((state_bucket = ANY (ARRAY['ready'::text, 'in_progress'::text, 'needs_review'::text, 'review_approved'::text, 'closed'::text, 'escalated'::text]))), CONSTRAINT tasks_unattended_check CHECK ((unattended = ANY (ARRAY[false, true]))), CONSTRAINT tasks_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'invalid'::text, 'error'::text]))), additional_skills jsonb, allow_automation boolean DEFAULT false NOT NULL, assigned_agent text, blocked_by_merge boolean DEFAULT false NOT NULL, category text, claimed_by_session_id uuid, closed_at timestamp with time zone, closed_commit_sha text, closed_in_session_id uuid, closed_reason text, commits jsonb, compacted_at timestamp with time zone, created_at timestamp with time zone NOT NULL, created_in_session_id uuid, description text, dispatch_failure_count integer DEFAULT 0, due_date date, escalated_at timestamp with time zone, escalation_reason text, github_issue_number integer, github_pr_number integer, github_repo text, id uuid NOT NULL, implementation_domain text, is_escalated boolean DEFAULT false NOT NULL, isolation text DEFAULT 'worktree'::text NOT NULL, labels jsonb, linear_issue_id text, linear_team_id text, merge_in_progress boolean DEFAULT false NOT NULL, parent_task_id uuid, path_cache text, priority integer DEFAULT 2, project_id uuid NOT NULL, seq_num integer, start_date date, state_bucket text DEFAULT 'ready'::text NOT NULL, task_type text DEFAULT 'task'::text, title text NOT NULL, unattended boolean DEFAULT false NOT NULL, updated_at timestamp with time zone NOT NULL, validation_criteria text, validation_fail_count integer DEFAULT 0, validation_feedback text, validation_override_reason text, validation_status text);
docs/guides/README.md:147:| [spec-writing.md](spec-writing.md) | Writing task specifications and validation criteria |
docs/guides/cli-commands.md:560:| `tasks create` | `--description`, `--validation-criteria` (required except epics), `--priority`, `--type`, `--depends-on`, `--project` |
docs/guides/cli-commands.md:561:| `tasks update` | `--title`, `--validation-criteria`, `--priority`, `--parent`, `--task-type` |
docs/guides/configuration.md:774:the verification section when generating validation criteria.
docs/guides/http-endpoints.md:474:and `validation_criteria`.
docs/guides/remote-docker-acceptance.md:180:as required by the historical acceptance criteria.
docs/guides/spec-writing.md:199:target, acceptance criteria, and behavior-pinning or test-infrastructure scope.
docs/guides/spec-writing.md:233:| `validation_criteria` | One-line pass/fail check |
docs/guides/task-expansion.md:177:- `tasks[].validation`: validation criteria copied onto the created task.
docs/guides/task-expansion.md:327:  validation_criteria: User model exists and tests cover core behavior.
docs/guides/task-expansion.md:338:  validation_criteria: Auth handler exists.
docs/guides/task-expansion.md:396:  `tdd:required`, and validation criteria for red, green,
docs/guides/tasks.md:23:type, category, parent, labels, and validation criteria. The workflow-facing
docs/guides/tasks.md:68:    "validation_criteria": "Task examples match registered schemas and all guide links resolve.",
docs/guides/tasks.md:73:`create_task` requires `title`, `category`, and nonempty `validation_criteria`
docs/guides/tasks.md:78:| `code` | Implementation work. Requires `validation_criteria` and `implementation_domain` (`backend`, `frontend`, or `fullstack`). |
docs/guides/tasks.md:354:results for independent review. Acceptance criteria are authored task data;
docs/guides/tasks.md:396:gobby tasks create "Title" --validation-criteria CRITERIA [-d DESCRIPTION] [-p PRIORITY] [-t TYPE] [-D BLOCKER]
docs/guides/tasks.md:397:gobby tasks update TASK [--title TITLE] [--validation-criteria CRITERIA] [--priority N] [--parent TASK] [--task-type TYPE]
docs/guides/tdd-enforcement.md:15:- validation criteria requiring red evidence, minimal green evidence,
docs/guides/tdd-enforcement.md:30:additional skill, or has validation criteria requiring TDD.
docs/guides/tdd-enforcement.md:66:Plan authors describe behavior and acceptance criteria. They do not add filler
docs/guides/tdd-enforcement.md:95:- TDD validation criteria require red, green, refactor/final-green, exact
docs/plans/abandoned/UI.md:742:  validation_criteria: string | null;
docs/plans/abandoned/cli-test-battery.md:179:| MCP equivalent | `generate_validation_criteria` | Compare output |
docs/plans/abandoned/drawbridge-enhancements.md:69:| — | `validation_criteria` | `"Visual change matches the annotation screenshot and comment description"` |
docs/plans/abandoned/drawbridge-enhancements.md:150:        validation_criteria="Visual change matches the annotation screenshot and comment description",
docs/plans/abandoned/drawbridge-enhancements.md:301:- `create_task` — supports `task_type: "epic"`, `parent_task_id`, `labels`, `category`, `validation_criteria`
docs/plans/abandoned/drawbridge-enhancements.md:305:- `validate_task` — validation criteria set at import time
docs/plans/abandoned/implementation-v2.md:105:- `spec-review.md` — heuristics for checking spec-vs-work: does the diff implement what the plan described, are there scope creeps, missing acceptance criteria. Explicitly NOT code-quality review (that's qa-reviewer's job).
docs/plans/abandoned/planning-agents.md:170:  clear user personas, prioritized user stories with acceptance criteria,
docs/plans/abandoned/planning-agents.md:199:  - Write acceptance criteria as Given/When/Then
docs/plans/abandoned/planning-agents.md:529:**gobby-prd**: Full PRD workflow — ingest brief, extract and question requirements, create user stories (As a/I want/So that + Given/When/Then acceptance criteria), MoSCoW prioritization, NFRs with measurable targets, success metrics. Ends with self-review against validation checklist. Output: `prd.md`.
docs/plans/abandoned/postgresql-migration.md:361:## Acceptance Criteria
docs/plans/abandoned/ui/phase-0-tui.md:59:- [ ] Display full task details (title, description, validation_criteria)
docs/plans/completed/TASKS.md:58:    validation_criteria TEXT,         -- Natural language prompt for validating completion
docs/plans/completed/TASKS.md:432:    For user stories: Creates acceptance criteria as subtasks
docs/plans/completed/TASKS.md:436:    Returns root task ID and full task tree with validation criteria.
docs/plans/completed/TASKS.md:461:    Validate task completion against its validation_criteria.
docs/plans/completed/TASKS.md:481:    - validation_criteria (if set)
docs/plans/completed/TASKS.md:585:  infer_validation: true                # Auto-generate validation_criteria for subtasks
docs/plans/completed/TASKS.md:681:    **Validation Criteria:** {validation_criteria}
docs/plans/completed/TASKS.md:691:    Evaluate whether the task has been completed according to the validation criteria.
docs/plans/completed/TASKS.md:714:1. When a task has `validation_criteria` set, validation can be triggered:
docs/plans/completed/TASKS.md:1150:    For user stories: Creates acceptance criteria as subtasks
docs/plans/completed/TASKS.md:1441:> Uses configured `task_validation` provider. Validates task completion against `validation_criteria`.
docs/plans/completed/TASKS.md:1446:- [x] Add `validation_criteria` column to tasks table
docs/plans/completed/TASKS.md:1459:- [x] Implement `generate_criteria()` method - LLM-generates validation criteria from task title/description
docs/plans/completed/TASKS.md:1487:- [x] Add `generate_validation_criteria` tool with `--all` support for bulk generation
docs/plans/completed/TASKS.md:1546:| 8 | **Validation criteria format** | Natural language prompt | Simple, flexible. LLM evaluates against current state. No rigid schema needed. |
docs/plans/completed/adversary-convergence.md:88:  section contracts`), 13 (`manifest-validation-criteria-completeness`), 15
docs/plans/completed/adversary-convergence.md:89:  (`manifest-criteria-save-retention`), 16 (`manifest-validation-criteria-fidelity`) —
docs/plans/completed/adversary-convergence.md:151:"Section X's validation criteria omit obligation Y" can be emitted against any plan of any
docs/plans/completed/cc-worktrees.md:67:| **Validation** | None | Diff-based validation criteria checked on close |
docs/plans/completed/enhancements.md:350:│  - Acceptance criteria from task                            │
docs/plans/completed/enhancements.md:357:│  - Checks acceptance criteria                               │
docs/plans/completed/enhancements.md:459:      Fix the issues and ensure all validation criteria pass.
docs/plans/completed/gobby-skills.md:375:**Acceptance Criteria:**
docs/plans/completed/gobby-skills.md:436:**Acceptance Criteria:**
docs/plans/completed/gobby-skills.md:526:**Acceptance Criteria:**
docs/plans/completed/gobby-skills.md:658:**Acceptance Criteria:**
docs/plans/completed/gobby-skills.md:781:**Acceptance Criteria:**
docs/plans/completed/gobby-skills.md:901:**Acceptance Criteria:**
docs/plans/completed/gobby-skills.md:1102:**Acceptance Criteria:**
docs/plans/completed/gobby-tasks-separation-of-concerns.md:22:| 6 | **Validation** | `task_validation.py` | `validate_task`, `get_validation_status`, `reset_validation_count`, `get_validation_history`, `get_recurring_issues`, `clear_validation_history`, `de_escalate_task`, `generate_validation_criteria`, `run_fix_attempt`, `validate_and_fix` | 10 |
docs/plans/completed/gobby-tasks-separation-of-concerns.md:81:| `generate_validation_criteria` | AI-generate criteria |
docs/plans/completed/gobby-tasks-separation-of-concerns.md:85:**Verdict: Mixed concern.** The first 7 tools are task-centric (query/update validation state on the task entity). But `run_fix_attempt` and `validate_and_fix` spawn agents — they're orchestration in disguise. `generate_validation_criteria` calls LLM. This registry mixes data access with workflow execution.
docs/plans/completed/hook-normalization-v2.md:420:## Acceptance Criteria
docs/plans/completed/impeccable-ux-agents-draft.md:45:- `src/gobby/install/shared/skills/plan-draft-ux/SKILL.md` — UX authoring methodology. Uses the shared grammar; adds UX-specific deliverable-section archetypes (information architecture, component inventory, interaction model, motion plan, content strategy, visual system). Defines validation_criteria patterns for the new acceptance kinds. Documents the `impeccable shape` steering command as the recommended brief-structuring tool.
docs/plans/completed/impeccable-ux-agents-draft.md:62:- Manifest emission rules updated for UX-flavored entries (e.g., `category: design`, validation_criteria patterns specific to design kinds).
docs/plans/completed/migration-flattening-0.4.0-launch.md:108:## Acceptance Criteria
docs/plans/completed/review-signal-learning.md:207:exists and the tier/target escalates, **`update_task`** its `description`, `validation_criteria`,
docs/plans/completed/review-signal-learning.md:213:IDs**; `validation_criteria` = guardrail acceptance; labels `guardrail`, `review-learning`,
docs/plans/completed/review-signal-learning.md:313:  validation_criteria/labels) on an existing open `pattern:<key>` task; non-promotable never counts.
docs/plans/completed/strangler-fig-decomposition.md:55:**Acceptance Criteria:**
docs/plans/completed/strangler-fig-decomposition.md:118:**Acceptance Criteria:**
docs/plans/completed/strangler-fig-decomposition.md:166:**Acceptance Criteria:**
docs/plans/completed/task-expansion-v2.md:12:| Validation criteria | Move from `create_task` to `enrich_task` (was 6+ sec) |
docs/plans/completed/task-expansion-v2.md:108:| `validate-criteria.md` | Validation criteria generation | `enrich_task` tool |
docs/plans/completed/task-expansion-v2.md:199:3. Remove validation criteria auto-generation (lines 427-447)
docs/plans/completed/task-expansion-v2.md:413:    validation_criteria: str  # Generated here, not in create_task
docs/plans/completed/task-expansion-v2.md:428:    generates validation criteria, and stores findings in expansion_context.
docs/plans/completed/task-expansion-v2.md:452:        Generate validation_criteria field (moved from create_task)
docs/plans/completed/task-expansion-v2.md:471:          "validation_criteria": "## Deliverable\n- [ ] ...",
docs/plans/completed/task-expansion-v2.md:499:6. Parent's `validation_criteria` updated to "All child tasks completed" (parent becomes container)
docs/plans/completed/task-expansion-v2.md:510:    Parent's validation_criteria updated to "All child tasks completed".
docs/plans/completed/task-expansion-v2.md:580:# Templated validation criteria for TDD phases
docs/plans/completed/task-expansion-v2.md:584:- [ ] Test coverage addresses acceptance criteria from parent task
docs/plans/completed/task-expansion-v2.md:639:    original_criteria = task.validation_criteria
docs/plans/completed/task-expansion-v2.md:644:        validation_criteria=TDD_CRITERIA_RED,  # Templated
docs/plans/completed/task-expansion-v2.md:651:        validation_criteria=original_criteria,  # Inherited from parent
docs/plans/completed/task-expansion-v2.md:659:        validation_criteria=TDD_CRITERIA_BLUE,  # Templated
docs/plans/completed/task-expansion-v2.md:665:    task.validation_criteria = PARENT_CRITERIA
docs/plans/completed/task-expansion-v2.md:689:    Validation criteria:
docs/plans/completed/task-expansion-v2.md:692:    - Green (implement): Inherits parent's original validation_criteria
docs/plans/completed/task-expansion-v2.md:817:  generates validation criteria, and stores findings in expansion_context.
docs/plans/completed/task-expansion-v2.md:828:  --no-validation         Skip validation criteria generation
docs/plans/completed/task-expansion-v2.md:1277:| `~/.gobby/prompts/validate-criteria.md` | Validation criteria prompt |
docs/plans/completed/task-expansion-v2.md:1413:#   Generated validation criteria
docs/plans/completed/task-expansion-v3.md:97:| TDD sandwich (3x tasks) | TDD as validation criteria |
docs/plans/completed/task-expansion-v3.md:229:            validation_criteria=subtask.get("validation"),
docs/plans/completed/task-expansion-v3.md:344:# - Validation criteria include "tests pass"
docs/plans/completed/task-expansion-v3.md:379:3. TDD requirement is explicit in task validation criteria
docs/plans/completed/task-lifecycle-v2.md:163:### Acceptance criteria
docs/plans/completed/task-lifecycle-v2.md:204:### Acceptance criteria
docs/plans/completed/task-lifecycle-v2.md:242:### Acceptance criteria
docs/plans/completed/task-lifecycle-v2.md:270:### Acceptance criteria
docs/plans/completed/task-lifecycle-v2.md:296:### Acceptance criteria
docs/plans/completed/task-lifecycle-v2.md:324:### Acceptance criteria
docs/plans/completed/task-lifecycle-v2.md:342:### Acceptance criteria
docs/plans/completed/web-chat-fixes.md:9:Task execution truth now lives in the expanded Gobby tasks under `#11509`. Use the task records for current scope, dependencies, and validation criteria; treat this document as planning context only.
docs/plans/completed/web-chat-fixes.md:128:All visual verification uses the `playwright-cli` skill via MCP. Each task's validation criteria must include Playwright steps.
docs/plans/cross-repo-submit-profile.md:95:## Acceptance Criteria
docs/plans/gcode-ask-fix.md:1010:  evidence met the unchanged acceptance criteria. Post-close memory review is complete.
docs/reference-audit/tasks.json:1431:      "finding": "Docs creation example omitted validation_criteria and implied only code required criteria.",
docs/reference-audit/tasks.json:1432:      "evidence": "create_task registered schema; _crud._task_invariant_error; criteria_contract.require_validation_criteria",
docs/reference-audit/tasks.json:1441:      "finding": "Shell examples used unquoted #N references and CLI option tables omitted validation criteria.",
docs/research/activity-panel-live-terminal-live-review-2026-07-29.md:11:The review compared the plan's acceptance criteria with the current terminal
docs/research/fieldy-voice-loop-review-queue-2026-09-29.md:115:#22719's validation criteria describe the old "turn recordings into agent work" design. If Josh picks Plan, the criteria need rewriting for the review-only flow before any implementation.
docs/research/grok-adversary-prompt-example.md:10:- PLAN_FILE: path to the Markdown plan (numbered acceptance criteria), or `(unavailable)`.
docs/research/grok-adversary-prompt-example.md:98:   `## Acceptance criteria` as a derived checklist: judge each criterion MET or
docs/reviews/TRIAGE.md:6:- **Task anatomy:** each fix task carries the source review file and commit (`covers:<file>` label), the merged unit id (`Unit UNNN`), `Where:` file:line pointers, a single committed fix approach, concrete validation criteria, and `implementation_domain`.
docs/reviews/TRIAGE.md:742:- #16202 (U496, `docs/reviews/mcp_proxy-tools.md`) — Enforce code-task validation_criteria invariant in update_task
docs/reviews/TRIAGE.md:1160:- #16873 (U855, `docs/reviews/tasks.md`) — Treat missing validation_criteria on leaf code tasks as a hard validation gate
docs/reviews/llm-prompts.md:99:- **Where:** `src/gobby/tasks/expansion/_compile.py:457` (`normalized = file_path.lstrip("./")`), `:459,472` (`repo_path / normalized`); paths from `extract_mentioned_files` (`src/gobby/tasks/commits.py:391`) over `task.title`/`description`/`validation_criteria`
docs/reviews/mcp_proxy-tools.md:268:### [IMPORTANT] `update_task` bypasses the category='code' ⇒ validation_criteria invariant
docs/reviews/mcp_proxy-tools.md:270:- **Failure mode:** `create_task` rejects code tasks without criteria/domain; `update_task` sets `category="code"` with no check and accepts `validation_criteria=""`. A code task without criteria skips LLM validation on close (`_lifecycle_close.py:270`).
docs/reviews/plans-sync.md:152:- **Why it matters:** the gate reports `valid` for leaves whose validation criteria never reference the artifact; symbols ending in `run`/`main`/`parse` match almost anything.
docs/reviews/quality-subsystems.md:165:- **Where:** `src/gobby/review_learning/promotion.py:242-257` (`_find_existing_task` matches only `pattern:{key}` + group labels — decision/target ignored), `:213-226` (update rewrites title/category/target/validation_criteria), `:225` (clears `implementation_domain` via explicit `None` → SET NULL at `src/gobby/storage/tasks/_updates.py:158`)
docs/reviews/support-infra.md:29:- **Failure mode:** Setting `telemetry.llm_tracing.enabled: true` does nothing in a running daemon. Repo-wide proof: the only production caller of any `init_telemetry` is `runner_init/storage.py:43`, importing from `gobby.telemetry.logging`; nothing in src imports the package-level one (`git grep "from gobby.telemetry import"` → only `inject_into_env` and `shutdown_telemetry`). The `LoggingInstrumentor` bridge at `__init__.py:100` is equally unreachable. The LangWatch epic's validation criteria ("init_telemetry calls setup_llm_instrumentors when llm_tracing.enabled") were validated against the wrong function.
docs/reviews/tasks.md:29:### [IMPORTANT] LLM validation gate is silently skipped whenever `task.validation_criteria` is empty
docs/reviews/tasks.md:30:- **Where:** `mcp_proxy/tools/tasks/_lifecycle_close.py:270` (`if is_leaf and ctx.task_validator and task.validation_criteria:`); criteria is required only at `_crud.py:115` for `category=="code"`; expansion sets it from an optional manifest field (`expansion/_apply.py:201`, may be None); storage `create_task` doesn't enforce it.
docs/reviews/tasks.md:31:- **Failure mode:** A leaf task reaching close with falsy `validation_criteria` bypasses the entire LLM validation block. Expansion-created code tasks (manifest omits `validation`), dispatch/build-created tasks, non-`code` categories, and storage-direct creations can all close with zero LLM validation — the commit/edit checks still run, so broken code can be committed and the task closed without the validator ever evaluating the diff. The TaskValidator-side analogue of the upstream close-gate poisoning.
docs/reviews/tasks.md:55:- **Where:** `expansion/_compile.py:443-475` (`_build_file_context`) — paths from `extract_mentioned_files(task_payload)` over title/description/validation_criteria; normalized only with `lstrip("./")` (`:455`) then `repo_path / normalized` (`:458`), read up to 3500 chars (`_common.py:349`) with **no containment check**.
docs/reviews/tasks.md:106:1. **Gates keyed on session-global flags or optional data, not task-scoped truth.** `task_has_commits` is a per-session boolean any of three tools can set (the Blocker), never checked against the closing task. The LLM validation gate hinges on a free-text `validation_criteria` enforced at one boundary for one category. Both are "absence/optional treated as permission," echoing the upstream "absence-of-signal = success" findings.
docs/reviews/web/components-a.md:49:- **Why it matters:** In an active system (agents/dispatcher constantly updating tasks) the tab flashes "Loading tasks…" repeatedly and typing into Description/Validation criteria is lossy — direct user-edit data loss, the exact class the optimistic-edit machinery was built to prevent.
src/gobby/agents/team_teardown.py:666:            validation_criteria=(
src/gobby/agents/team_teardown.py:710:            validation_criteria=(
src/gobby/cli/tasks/_crud_mutations.py:10:from gobby.tasks.criteria_text import TaskCriteriaError, require_validation_criteria
src/gobby/cli/tasks/_crud_mutations.py:18:    validation_criteria: str | None,
src/gobby/cli/tasks/_crud_mutations.py:32:        validation_criteria = require_validation_criteria(task_type, validation_criteria)
src/gobby/cli/tasks/_crud_mutations.py:55:            validation_criteria=validation_criteria,
src/gobby/cli/tasks/_crud_mutations.py:100:    validation_criteria: str | None,
src/gobby/cli/tasks/_crud_mutations.py:121:    if validation_criteria is not None:
src/gobby/cli/tasks/_crud_mutations.py:122:        kwargs["validation_criteria"] = validation_criteria
src/gobby/cli/tasks/crud.py:166:    "--validation-criteria",
src/gobby/cli/tasks/crud.py:176:    validation_criteria: str | None,
src/gobby/cli/tasks/crud.py:194:        validation_criteria=validation_criteria,
src/gobby/cli/tasks/crud.py:215:@click.option("--validation-criteria", help="New observable completion criteria")
src/gobby/cli/tasks/crud.py:239:    validation_criteria: str | None,
src/gobby/cli/tasks/crud.py:258:        validation_criteria,
src/gobby/config/features.py:219:    Used by task expansion to generate precise validation criteria with actual commands.
src/gobby/config/tasks.py:5:- PatternCriteriaConfig: Pattern-specific validation criteria templates
src/gobby/config/tasks.py:50:    """Configuration for pattern-specific validation criteria templates.
src/gobby/config/tasks.py:52:    Defines validation criteria templates for common development patterns like
src/gobby/config/tasks.py:82:        description="Pattern name to list of validation criteria templates. "
src/gobby/config/tasks.py:131:        description="Pattern-specific validation criteria templates",
src/gobby/feedback/actions.py:71:        validation_criteria: str | None = ...,
src/gobby/feedback/actions.py:369:            validation_criteria=(
src/gobby/install/shared/prompts/expansion/system.md:38:      "validation": "Specific validation criteria using project commands when possible",
src/gobby/install/shared/prompts/expansion/system.md:73:9. For TDD-required code/config work, emit one implementation task with `additional_skills: ["test-driven-development"]`, a `tdd:required` label if labels are present, and validation criteria requiring red, green, refactor/final-green, exact test command, test-quality audit evidence, and test-types audit (mypy ratchet) evidence for Python test changes.
src/gobby/install/shared/prompts/expansion/system.md:84:    identify that file as new in the test intent and validation criteria.
src/gobby/install/shared/prompts/external_validation/agent.md:10:  criteria_section:
src/gobby/install/shared/prompts/external_validation/agent.md:13:    description: Acceptance criteria or task description section
src/gobby/install/shared/prompts/external_validation/agent.md:32:Validate whether the code changes satisfy the acceptance criteria. You have access to tools to:
src/gobby/install/shared/prompts/external_validation/agent.md:40:{{ criteria_section }}{{ priority_section }}{{ symbol_section }}
src/gobby/install/shared/prompts/external_validation/agent.md:46:1. Review the changes against the acceptance criteria
src/gobby/install/shared/prompts/external_validation/external.md:10:  criteria_section:
src/gobby/install/shared/prompts/external_validation/external.md:13:    description: Acceptance criteria or task description section
src/gobby/install/shared/prompts/external_validation/external.md:32:{{ criteria_section }}{{ priority_section }}{{ symbol_section }}
src/gobby/install/shared/prompts/external_validation/external.md:38:1. Review each change against the acceptance criteria
src/gobby/install/shared/prompts/external_validation/spawn.md:14:  criteria_section:
src/gobby/install/shared/prompts/external_validation/spawn.md:17:    description: Acceptance criteria or task description section
src/gobby/install/shared/prompts/external_validation/spawn.md:51:{{ criteria_section }}{{ category_section }}{{ priority_section }}{{ symbol_section }}
src/gobby/install/shared/prompts/external_validation/system.md:8:You are an independent QA validator reviewing code changes. You have no prior context about this task — evaluate purely from the acceptance criteria and the changes provided.
src/gobby/install/shared/prompts/validation/criteria.md:2:name: validation-criteria
src/gobby/install/shared/prompts/validation/criteria.md:3:description: Prompt for generating validation criteria from task description
src/gobby/install/shared/prompts/validation/criteria.md:15:Generate validation criteria for this task.
src/gobby/install/shared/skills/annotate/SKILL.md:60:and implementation domain, with observable validation criteria. Infer dependencies
src/gobby/install/shared/skills/architecture/SKILL.md:59:Do not write implementation tasks or PRD acceptance criteria. Keep the brief
src/gobby/install/shared/skills/architecture/SKILL.md:113:Do NOT write `### N.N` task sections or PRD acceptance criteria.
src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md:12:- Test review. Before implementation, review the Test Writer's exact linked test SHA against the stored criteria and its authenticated RED evidence. Check that the failures demonstrate the required behavior and that assertions do not merely mirror the implementation. Record the verdict with the existing review step and epoch rejection counter, then send it in the verdict order above. An approved test SHA moves the claim to the Developer automatically; it does not authorize landing unfinished implementation. Review the later implementation SHA independently.
src/gobby/install/shared/skills/gobby/references/plan/approval.md:7:Before manifest handoff, verify a real nonblank `**Plan ID:** <id>` outside fences and reject any `covers:unknown:` label. Preserve every acceptance item in source order in server-derived validation criteria; never summarize away obligations.
src/gobby/install/shared/skills/gobby/references/tasks/closing.md:11:1. Finish the task, resolve owned findings, and check its acceptance criteria.
src/gobby/install/shared/skills/gobby/references/tasks/closing.md:70:Developers and independent reviewers remain responsible for acceptance criteria,
src/gobby/install/shared/skills/gobby/references/tasks/creation.md:11:Every task type except `epic` requires meaningful `validation_criteria`, including
src/gobby/install/shared/skills/gobby/references/tasks/creation.md:26:    "validation_criteria": "Task examples match registered schemas and all links resolve.",
src/gobby/install/shared/skills/gobby/references/tasks/overview.md:16:acceptance criteria. A listing is bounded by its schema's limit; do not assume
src/gobby/install/shared/skills/prd/SKILL.md:7:triggers: prd, product requirements, acceptance criteria
src/gobby/install/shared/skills/prd/SKILL.md:20:PRD should define goal, users, scope, explicit non-scope, acceptance criteria,
src/gobby/install/shared/skills/prd/SKILL.md:30:5. Write acceptance criteria that can become plan deliverables.
src/gobby/install/shared/skills/prd/SKILL.md:51:### Acceptance Criteria
src/gobby/install/shared/skills/tech-writer/SKILL.md:168:against the task's acceptance criteria, and close. Closing a docs task
src/gobby/install/shared/workflows/agents/consolidator.yaml:37:    - Apply directed folds and retirements only. For each approved proposal: merge the union of the folded task's validation criteria into the survivor with update_task; record the fold in a dated section of the survivor's description; close the folded task with close_task(reason=superseded, superseded_by=<survivor>), or reason=moot for a retirement; then report each applied fold to Triage. Never close a task as done or wont_do.
src/gobby/install/shared/workflows/agents/developer.yaml:34:    - TDD. Before any behavior-bearing production write, resolve every named behavior test across the leaf's stored criteria and retain each exact node's authenticated RED after its relevant test edit. Skill completion permits test/stub preparation; writing one acceptance file never opens this barrier. Only explicitly stored regression-only: criteria proven unchanged against the claim baseline may use GREEN alone. Test-only writes and mechanically verified behavior-free API stubs may prepare RED; shell and mixed implementation writes share the leaf-wide guard. Close needs the same per-test chronology and later GREEN for every named test. Preserve original RED through compaction; never replay it.
src/gobby/install/shared/workflows/agents/epic-reviewer.yaml:56:      docs/build epic, use the task Discovery Brief, validation criteria, and full
src/gobby/install/shared/workflows/agents/epic-reviewer.yaml:59:      validation criteria. Filesystem reads are expected in the review step.
src/gobby/install/shared/workflows/agents/epic-reviewer.yaml:191:        use the Discovery Brief, validation criteria, and descendant task set as
src/gobby/install/shared/workflows/agents/test-writer.yaml:23:    - Start. Send your session ref to your Dev Manager with `gobby-agents:send_message(target="session", target_id=<Dev Manager session ref from your launch prompt>)`. Then read the task with `gobby-tasks:get_task` and load `test-driven-development` plus the skills its paths need. Claim a single task with `gobby-tasks:claim_task` before you edit. An epic (`task_type` `epic`) has no acceptance criteria and is never claimed by a seat: wait with `gobby-agents:wait_for_coordination` on your Dev Manager until it names a leaf, then claim only that leaf.
src/gobby/install/shared/workflows/agents/test-writer.yaml:24:    - Tests. Write the smallest tests that pin the task's acceptance criteria and run them. Keep each expected assertion failure as the RED evidence; fix collection, environment or compile failures before treating a run as RED. Write no production code beyond a behavior-free stub a test needs to compile.
src/gobby/install/shared/workflows/agents/test-writer.yaml:26:    - Backstop. If the task's record shows an out-of-order implementation link (an `out_of_order_link` incident recorded before its test step was approved), map the existing tests at the candidate SHA to the task's validation criteria, add tests only for the gaps, commit them, link the commit, record the test step with that map in its feedback, and send the combined SHA to your Dev Manager as `EVENT=CANDIDATE`. The tests-first path above is unchanged.
src/gobby/install/shared/workflows/agents/test-writer.yaml:169:        Write the smallest tests that pin the claimed task's acceptance criteria and run
src/gobby/install/shared/workflows/pipelines/team-planning.yaml:148:        validation_criteria: "The plan file is decision-complete, passes gobby plans validate, and carries its consensus and manifest stamps."
src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml:51:    description: "Acceptance test paths derived from claimed task validation criteria"
src/gobby/mcp_proxy/tools/tasks/_crud.py:36:from gobby.tasks.criteria_text import TaskCriteriaError, require_validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:51:    validation_criteria: str | None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:54:    supplied_validation_criteria: str | None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:58:        require_validation_criteria(task_type, validation_criteria)
src/gobby/mcp_proxy/tools/tasks/_crud.py:61:    if supplied_validation_criteria is not None:
src/gobby/mcp_proxy/tools/tasks/_crud.py:62:        findings = (*malformed_test_reference_findings(supplied_validation_criteria),)
src/gobby/mcp_proxy/tools/tasks/_crud.py:94:        validation_criteria: str | None = None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:118:            validation_criteria: Acceptance criteria for validating completion.
src/gobby/mcp_proxy/tools/tasks/_crud.py:180:            validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:182:            supplied_validation_criteria=validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:231:            "validation_criteria": validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:372:        brief="Create a new task. Requires: title, session_id, category, validation_criteria (every task_type except 'epic')",
src/gobby/mcp_proxy/tools/tasks/_crud.py:418:                    "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing tests), 'test' (test-writing), 'research' (investigation), 'planning' (design/architecture), or 'manual' (manual verification). Category does not affect the validation_criteria requirement, which applies to every task_type except 'epic'.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:421:                "validation_criteria": {
src/gobby/mcp_proxy/tools/tasks/_crud.py:423:                    "description": "Acceptance criteria for task completion. REQUIRED for every task_type except 'epic' — creation fails without it, whatever the category. Describe what 'done' looks like — validate_task checks the diff against this. Reference named tests as `test: path::test_symbol` and evidence files as `file: path`. A backticked command span is a mandatory exact close command; pipelines, placeholders, redirections, and trailing punctuation are rejected.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:511:        description="Get task details including dependencies. Task ID can be #N (e.g., #1), path (e.g., 1.2.3), or UUID. Returns an actionable card by default, including description, validation_criteria, labels, state, and dependency summaries; set brief=false for the full task record.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:521:                    "description": "If true (default), return the actionable card: identity, description, validation_criteria, labels, state, execution settings, and dependency summaries. If false, also return project and session ids, commits, closure, validation, merge and dispatch details, escalation fields, GitHub/Linear links, dates, and full dependency rows.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:537:        validation_criteria: str | None = None,
src/gobby/mcp_proxy/tools/tasks/_crud.py:577:        effective_validation_criteria = (
src/gobby/mcp_proxy/tools/tasks/_crud.py:578:            validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:579:            if validation_criteria is not None
src/gobby/mcp_proxy/tools/tasks/_crud.py:580:            else current_task.validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:590:            effective_validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:592:            supplied_validation_criteria=validation_criteria,
src/gobby/mcp_proxy/tools/tasks/_crud.py:607:        if validation_criteria is not None:
src/gobby/mcp_proxy/tools/tasks/_crud.py:608:            kwargs["validation_criteria"] = validation_criteria
src/gobby/mcp_proxy/tools/tasks/_crud.py:694:                "validation_criteria": {
src/gobby/mcp_proxy/tools/tasks/_crud.py:696:                    "description": "Acceptance criteria for validating task completion. Cannot be cleared on any task_type except 'epic'. Reference named tests as `test: path::test_symbol` and evidence files as `file: path`. A backticked command span is a mandatory exact close command; pipelines, placeholders, redirections, and trailing punctuation are rejected.",
src/gobby/mcp_proxy/tools/tasks/_crud.py:706:                    "description": "Task domain: 'code' (implementation — also requires implementation_domain), 'config' (configuration files), 'docs' (documentation), 'refactor' (code restructuring, including updating existing tests), 'test' (test-writing), 'research' (investigation), 'planning' (design/architecture), or 'manual' (manual verification). Category does not affect the validation_criteria requirement, which applies to every task_type except 'epic'.",
src/gobby/mcp_proxy/tools/tasks/_formatters.py:159:        "validation_criteria": _task_value(task, "validation_criteria"),
src/gobby/mcp_proxy/tools/tasks/_task_scope.py:176:        for reference in extract_artifact_references(task.validation_criteria or "", kind):
src/gobby/plans/_artifact_refs.py:25:def artifact_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:27:        return _file_referenced(item.artifact_ref, validation_criteria)
src/gobby/plans/_artifact_refs.py:29:        return _symbol_referenced(item.artifact_ref, validation_criteria)
src/gobby/plans/_artifact_refs.py:31:        return _test_referenced(item.artifact_ref, validation_criteria)
src/gobby/plans/_artifact_refs.py:33:        return _behavior_referenced(item, validation_criteria)
src/gobby/plans/_artifact_refs.py:37:def _file_referenced(artifact_ref: str, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:39:    if any(_contains_path_ref(validation_criteria, candidate) for candidate in candidates):
src/gobby/plans/_artifact_refs.py:41:    if any(_path_parts_referenced(validation_criteria, candidate) for candidate in candidates):
src/gobby/plans/_artifact_refs.py:44:    return bool(markers) and all(marker in validation_criteria for marker in markers)
src/gobby/plans/_artifact_refs.py:47:def _symbol_referenced(artifact_ref: str, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:52:        _contains_ref(validation_criteria, candidate) for candidate in candidates if candidate
src/gobby/plans/_artifact_refs.py:56:def _test_referenced(artifact_ref: str, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:57:    if _contains_ref(validation_criteria, artifact_ref):
src/gobby/plans/_artifact_refs.py:61:            _contains_path_ref(validation_criteria, path)
src/gobby/plans/_artifact_refs.py:62:            or _path_parts_referenced(validation_criteria, path)
src/gobby/plans/_artifact_refs.py:64:        and _contains_ref(validation_criteria, test_name)
src/gobby/plans/_artifact_refs.py:69:def _behavior_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
src/gobby/plans/_artifact_refs.py:71:        _contains_ref(validation_criteria, ref, case_sensitive=False)
src/gobby/plans/_artifact_refs.py:80:        _contains_path_ref(validation_criteria, candidate)
src/gobby/plans/_artifact_refs.py:81:        or _path_parts_referenced(validation_criteria, candidate)
src/gobby/plans/_artifact_refs.py:86:        _contains_ref(validation_criteria, task_ref, case_sensitive=False)
src/gobby/plans/_task_store.py:22:    validation_criteria: str
src/gobby/plans/_task_store.py:43:            "validation_criteria": record.validation_criteria,
src/gobby/plans/_task_store.py:110:        validation_criteria=task.validation_criteria or task.description or "",
src/gobby/plans/_task_store.py:131:        validation_criteria=_first_string(raw, "validation_criteria", "validation", "description"),
src/gobby/plans/coverage.py:84:    validation_criteria_snippet: str
src/gobby/plans/coverage.py:145:    leaf_validation_criteria: str,
src/gobby/plans/coverage.py:167:    if not artifact_referenced(item, leaf_validation_criteria):
src/gobby/plans/coverage.py:365:        result = validate_covers(record, task.validation_criteria, task.ref, plan_doc)
src/gobby/plans/coverage.py:368:            validation_criteria_snippet=_snippet(task.validation_criteria),
src/gobby/plans/coverage.py:558:                                validation_criteria_snippet=str(exc),
src/gobby/plans/coverage.py:568:            result = validate_covers(record, task.validation_criteria, task.ref, plan_doc)
src/gobby/plans/coverage.py:579:                            validation_criteria_snippet=result.detail,
src/gobby/plans/coverage.py:593:            validation_criteria_snippet="",
src/gobby/plans/coverage.py:598:        validation_criteria_snippet=str(raw.get("validation_criteria_snippet", "")),
src/gobby/plans/coverage.py:764:                "validation_criteria": record.validation_criteria,
src/gobby/plans/coverage_manifest.py:424:        "validation_criteria_snippet": _attr(leaf, "validation_criteria_snippet"),
src/gobby/plans/deferral.py:18:    "validation_criteria_does_not_duplicate",
src/gobby/plans/deferral.py:100:    validation_criteria = _task_validation_criteria(task)
src/gobby/plans/deferral.py:102:        if not artifact_referenced(item, validation_criteria):
src/gobby/plans/deferral.py:108:                "validation_criteria_does_not_duplicate",
src/gobby/plans/deferral.py:109:                f"task validation criteria do not duplicate artifact {item.artifact_ref!r}",
src/gobby/plans/deferral.py:172:def _task_validation_criteria(task: dict[str, Any]) -> str:
src/gobby/plans/deferral.py:173:    value = task.get("validation_criteria", "")
src/gobby/plans/manifest_emitter.py:150:        entry["validation_criteria"] = "\n".join(
src/gobby/plans/manifest_emitter.py:171:                    str(entry["validation_criteria"]),
src/gobby/plans/manifest_emitter.py:398:        "validation_criteria": validation,
src/gobby/plans/manifest_parser.py:23:    "validation_criteria",
src/gobby/plans/manifest_parser.py:34:    validation_criteria: str
src/gobby/plans/manifest_parser.py:233:        validation_criteria=str(raw["validation_criteria"]),
src/gobby/projects/purge.py:108:# tasks_require_validation_criteria is NOT VALID: legacy rows without criteria
src/gobby/projects/purge.py:112:    "validation_criteria = CASE"
src/gobby/projects/purge.py:113:    " WHEN task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL"
src/gobby/projects/purge.py:114:    " THEN validation_criteria"
src/gobby/projects/purge.py:115:    " ELSE 'Legacy task: validation criteria were not recorded before they became required.'"
src/gobby/servers/routes/agent_spawn.py:148:    # Validation criteria
src/gobby/servers/routes/agent_spawn.py:149:    if task.validation_criteria:
src/gobby/servers/routes/agent_spawn.py:150:        parts.append(f"\n### Validation Criteria\n{task.validation_criteria}")
src/gobby/servers/routes/tasks.py:65:    validation_criteria: str | None = Field(default=None, description="Acceptance criteria")
src/gobby/servers/routes/tasks.py:96:    validation_criteria: str | None = Field(default=None, description="New validation criteria")
src/gobby/servers/routes/tasks.py:316:                validation_criteria=request_data.validation_criteria,
src/gobby/storage/tasks/_close.py:17:from gobby.tasks.criteria_text import normalized_validation_criteria
src/gobby/storage/tasks/_close.py:21:_LEGACY_VALIDATION_CRITERIA = (
src/gobby/storage/tasks/_close.py:22:    "Legacy task: validation criteria were not recorded before they became required."
src/gobby/storage/tasks/_close.py:100:               validation_criteria = CASE
src/gobby/storage/tasks/_close.py:102:                        OR NULLIF(btrim(validation_criteria), '') IS NOT NULL
src/gobby/storage/tasks/_close.py:103:                   THEN validation_criteria
src/gobby/storage/tasks/_close.py:116:            _LEGACY_VALIDATION_CRITERIA,
src/gobby/storage/tasks/_close.py:224:                and normalized_validation_criteria(parent["validation_criteria"]) is not None
src/gobby/storage/tasks/_creation.py:24:from gobby.tasks.criteria_text import require_validation_criteria
src/gobby/storage/tasks/_creation.py:41:    validation_criteria: str | None = None,
src/gobby/storage/tasks/_creation.py:64:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_creation.py:83:    validation_criteria: str | None = None,
src/gobby/storage/tasks/_creation.py:104:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_creation.py:150:    validation_criteria: str | None = None,
src/gobby/storage/tasks/_creation.py:166:    validation_criteria = require_validation_criteria(task_type, validation_criteria)
src/gobby/storage/tasks/_creation.py:192:                    validation_criteria, review_fail_count,
src/gobby/storage/tasks/_creation.py:210:                    validation_criteria,
src/gobby/storage/tasks/_decomposition.py:22:        validation_criteria: str | None = None,
src/gobby/storage/tasks/_decomposition.py:40:            validation_criteria: Observable acceptance criteria for every non-epic task.
src/gobby/storage/tasks/_decomposition.py:71:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_manager.py:179:        validation_criteria: str | None = None,
src/gobby/storage/tasks/_manager.py:198:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_manager.py:218:        validation_criteria: str | None = None,
src/gobby/storage/tasks/_manager.py:236:            validation_criteria=validation_criteria,
src/gobby/storage/tasks/_models.py:281:    validation_criteria: str | None = None
src/gobby/storage/tasks/_models.py:365:            validation_criteria=(
src/gobby/storage/tasks/_models.py:366:                row["validation_criteria"] if "validation_criteria" in keys else None
src/gobby/storage/tasks/_models.py:426:            "validation_criteria": self.validation_criteria,
src/gobby/storage/tasks/_update_facade.py:11:from gobby.tasks.criteria_text import require_validation_criteria
src/gobby/storage/tasks/_update_facade.py:44:        validation_criteria: MaybeUnset[str | None] = UNSET,
src/gobby/storage/tasks/_update_facade.py:64:        if task_type is not UNSET or validation_criteria is not UNSET:
src/gobby/storage/tasks/_update_facade.py:69:            if validation_criteria is UNSET:
src/gobby/storage/tasks/_update_facade.py:70:                effective_criteria = current_task.validation_criteria
src/gobby/storage/tasks/_update_facade.py:73:                    validation_criteria if isinstance(validation_criteria, str) else None
src/gobby/storage/tasks/_update_facade.py:75:            effective_criteria = require_validation_criteria(
src/gobby/storage/tasks/_update_facade.py:79:            if validation_criteria is not UNSET:
src/gobby/storage/tasks/_update_facade.py:80:                validation_criteria = effective_criteria
src/gobby/storage/tasks/_update_facade.py:109:                validation_criteria=validation_criteria,
src/gobby/storage/tasks/_updates.py:93:    validation_criteria: MaybeUnset[str | None] = UNSET,
src/gobby/storage/tasks/_updates.py:161:    if validation_criteria is not UNSET:
src/gobby/storage/tasks/_updates.py:162:        updates.append("validation_criteria = %s")
src/gobby/storage/tasks/_updates.py:163:        params.append(validation_criteria)
src/gobby/storage/tasks/_updates.py:292:    validation_criteria: MaybeUnset[str | None] = UNSET,
src/gobby/storage/tasks/_updates.py:359:        validation_criteria=validation_criteria,
src/gobby/sync/task_review_serialization.py:18:    validation_criteria: str | None
src/gobby/sync/task_review_serialization.py:29:        "criteria": task.validation_criteria,
src/gobby/sync/task_review_serialization.py:90:        "validation_criteria": value.get("criteria"),
src/gobby/sync/tasks.py:15:from gobby.tasks.criteria_text import require_validation_criteria
src/gobby/sync/tasks.py:550:                        validation_criteria = require_validation_criteria(
src/gobby/sync/tasks.py:552:                            review["validation_criteria"],
src/gobby/sync/tasks.py:610:                            "validation_criteria": validation_criteria,
src/gobby/tasks/acceptance_references.py:1:"""Parse test and file references in task acceptance criteria."""
src/gobby/tasks/commits.py:448:    """Extract file paths mentioned in task title, description, and validation_criteria.
src/gobby/tasks/commits.py:455:        task: Task dictionary with title, description, and optionally validation_criteria.
src/gobby/tasks/commits.py:472:    if task.get("validation_criteria"):
src/gobby/tasks/commits.py:473:        text_parts.append(task["validation_criteria"])
src/gobby/tasks/commits.py:517:        task: Task dictionary with title, description, and optionally validation_criteria.
src/gobby/tasks/commits.py:528:    if task.get("validation_criteria"):
src/gobby/tasks/commits.py:529:        text_parts.append(task["validation_criteria"])
src/gobby/tasks/criteria_text.py:10:def normalized_validation_criteria(value: str | None) -> str | None:
src/gobby/tasks/criteria_text.py:18:def require_validation_criteria(task_type: str, value: str | None) -> str | None:
src/gobby/tasks/criteria_text.py:20:    normalized = normalized_validation_criteria(value)
src/gobby/tasks/criteria_text.py:23:            "Every non-epic task requires nonempty validation_criteria. "
src/gobby/tasks/expansion/_apply.py:178:                    validation_criteria=epic_validation,
src/gobby/tasks/expansion/_apply.py:234:                    validation_criteria=task_item.get("validation"),
src/gobby/tasks/expansion/_compile.py:397:            "validation": task_item.get("validation") or task_item.get("validation_criteria"),
src/gobby/tasks/expansion/_compile.py:502:        "validation_criteria": task.validation_criteria,
src/gobby/tasks/expansion/_compile.py:532:            task.validation_criteria,
src/gobby/tasks/expansion/_contract.py:247:    validation = _contract_validation_criteria(entry, section)
src/gobby/tasks/expansion/_contract.py:278:def _contract_validation_criteria(entry: ManifestEntry, section: PlanSection) -> str:
src/gobby/tasks/expansion/_contract.py:279:    lines = [entry.validation_criteria.strip()]
src/gobby/tasks/expansion/_deferrals.py:36:    ``needs-planning`` hold label, validation criteria that duplicate its original
src/gobby/tasks/expansion/_deferrals.py:114:            validation_criteria=validation,
src/gobby/tasks/expansion/_reconcile.py:265:        validation_criteria=task_item.get("validation"),
src/gobby/tasks/import_criteria.py:1:"""Observable validation criteria for externally imported tasks."""
src/gobby/tasks/import_criteria.py:6:def external_issue_validation_criteria(source: str, reference: str) -> str:
src/gobby/tasks/prompts/expand-task.md:17:| validation_criteria | string | No | How to verify this subtask is complete |
src/gobby/tasks/prompts/expand-task.md:33:      "validation_criteria": "Run migrations and verify tables exist"
src/gobby/tasks/prompts/expand-task.md:42:      "validation_criteria": "Unit tests for all repository methods pass"
src/gobby/tasks/prompts/expand-task.md:51:      "validation_criteria": "Integration tests for all endpoints pass"
src/gobby/tasks/prompts/expand-task.md:69:4. **Testing**: Every coding subtask should have a category and validation_criteria.
src/gobby/tasks/prompts/expand-task.md:76:## Validation Criteria Rules
src/gobby/tasks/prompts/expand-task.md:78:For each subtask, generate precise validation criteria in the `validation_criteria` field.
src/gobby/tasks/related_tests.py:60:    validation_criteria: str | None = None,
src/gobby/tasks/related_tests.py:66:    search_text = f"{task_title} {validation_criteria or ''} {task_description or ''}"
src/gobby/tasks/tree_builder.py:177:        validation_criteria = node.get("validation_criteria")
src/gobby/tasks/tree_builder.py:191:                validation_criteria=validation_criteria,
src/gobby/workflows/claimed_task_extra_skills.py:42:    validation_criteria: str | None,
src/gobby/workflows/claimed_task_extra_skills.py:47:    text = (validation_criteria or "").lower()
src/gobby/workflows/claimed_task_extra_skills.py:75:            validation_criteria = _string_field(task, "validation_criteria")
src/gobby/workflows/claimed_task_extra_skills.py:85:                validation_criteria=validation_criteria,
src/gobby/workflows/claimed_task_extra_skills.py:91:            if validation_criteria:
src/gobby/workflows/claimed_task_extra_skills.py:92:                for reference in extract_artifact_references(validation_criteria, "test"):
web/src/components/activity/TasksTabActions.ts:20:  validation_criteria?: string;
web/src/components/activity/__tests__/TasksTab.events.test.tsx:488:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.setup.ts:98:      validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:368:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:460:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:556:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:564:        validation_criteria: null,
web/src/components/activity/__tests__/TasksTab.test.tsx:662:        validation_criteria: "Verify the lower pane layout",
web/src/components/activity/__tests__/TasksTab.test.tsx:690:      (screen.getByLabelText("Validation criteria") as HTMLTextAreaElement)
web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx:134:    expect(getByLabelText("Validation criteria")).toBeTruthy();
web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx:164:          validation_criteria: null,
web/src/components/activity/__tests__/fixtures.ts:31:    validation_criteria: null,
web/src/components/activity/taskFieldRouting.ts:18:  "validation_criteria",
web/src/components/activity/taskFieldRouting.ts:46: *                category/labels/validation_criteria)
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:18: * labels, description, validation criteria); every commit routes through the
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:52:  const validationCriteria = task.validation_criteria ?? "";
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:85:          <StaticBlock label="Validation criteria" value={validationCriteria} />
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:176:          Validation criteria
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:180:          ariaLabel="Validation criteria"
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:181:          placeholder="No validation criteria"
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:183:          disabled={edit.isFieldPending(task.id, "validation_criteria")}
web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx:187:              field: "validation_criteria",
web/src/components/tasks/TaskCreateForm.tsx:45:  validation_criteria?: string;
web/src/components/tasks/TaskCreateForm.tsx:144:        params.validation_criteria = validationCriteria.trim();
web/src/components/tasks/TaskCreateForm.tsx:326:            <FormField label="Validation Criteria">
web/src/lib/__tests__/taskNormalization.test.ts:14:        validation_criteria: null,
web/src/lib/__tests__/taskNormalization.test.ts:24:      { id: "task-1", validation_criteria: ["run tests"] },
web/src/lib/taskNormalization.ts:32:  validation_criteria?: string | null;
web/src/lib/taskNormalization.ts:118:  validation_criteria: isOptionalString,
web/src/types/tasks.ts:78:  validation_criteria: string | null;
web/tests/fixtures/webChatRestorePlan.ts:44:  validation_criteria: "Refresh keeps claimed task state visible.",
```

### Risks

- Automatic close/landing gates prove required records and repository invariants, not that the text's obligations were actually satisfied. Incorrect reviewer judgment or weak criteria can still produce approved records.
- Schema and Developer instructions overstate current automatic enforcement. A wording-only follow-up must not leave users relying on the retired automatic diff/command check.
- Presence enforcement only proves nonblank text. Legacy placeholder criteria can satisfy it without conveying a useful acceptance contract.
- Source templates do not prove installed policy or per-run compliance. The live DB population count was not independently verified.
- The main checkout advances during fleet work. The attached excerpts retain their observation bindings; final checks must confirm their bytes still match the cited source before review.
