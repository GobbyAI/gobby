# Direct native Ask verification

Final result: all fourteen questions have source-supported answers after the
fixes and separately recorded retries below. Q14 uses an explicit C3 commit-hash
clarification. This is a direct functional check, not a first-attempt success
rate or a comparison against the historical frozen benchmark.

User direction, 2026-09-13: run the fourteen questions against the existing Game
Goblins checkout/index and ordinary daemon; fix failures until Ask answers them.
No evaluation worktrees, separate services, or cohort index builds. This supersedes
the unexecuted frozen-cohort apparatus preserved in Git history.

User follow-up after Q11: stop limiting runs to 600 seconds; tune speed later.
Remaining questions and timeout retries use `--timeout-seconds 3600`. Previously
started runs retain their recorded deadlines and results.

Checkout: `/Users/josh/Projects/game-goblins` at
`6f43eba0ab07f3921749705e63bdb9f175b5f45c`. Existing project:
`1d7a5bc4-6530-4642-aec8-ff41025caaa2`. `gcode status` reports a healthy index,
173 files and 2,914 symbols. These current-checkout results do not reproduce the
historical frozen-commit benchmark.

## Direct attempts

- Q01 attempt 1, installed CLI before coordinated update:
  `gcode ask "What is the shared platform, and which systems remain standalone?"
  --project /Users/josh/Projects/game-goblins --timeout-seconds 600
  --retrieval deterministic --format json`, with coordinator session identity.
  Run `9388f171-a45b-43b5-afe1-c42836955a0c` failed at `bind_prepare`:
  `authenticated session project path mismatch`. Binding selected the correct
  existing project and commit. Raw output: `/tmp/ask-direct-13038-q01-attempt1.json`;
  stderr: `/tmp/ask-direct-13038-q01-attempt1.stderr`.
- Q01 attempt 2, invoked from the Game Goblins checkout without a session header:
  HTTP 400, `X-Gobby-Session-Id is required`; no Ask run created.
  Raw stderr: `/tmp/ask-direct-13038-q01-attempt2.stderr`.
- Fix `9a03cf2` reuses the existing project-launcher helper for operator-authenticated
  Ask calls, binding grants to the selected project and supporting ordinary CLI
  calls without agent-session headers. Signed agent requests retain their verified
  session and project restrictions. All 80 HTTP tests pass, including real
  authentication, cross-project operator binding, and launcher reuse. Ruff,
  scoped mypy, test type/quality audits and suppression checks pass.
- Latest `gdaemon`, `ghook`, and `gterm` release builds passed. Session #12910 built
  latest `gcode`; the coordinated installation/restart completed before attempt 3.

## Updated runtime and index repair

The coordinated restart loaded Ask fixes `9a03cf2` and `75624b2`.
Installed gcode SHA-256:
`4174abf7f6f65b88cb91bf3a1617edd23f5f78479450f2852cdd61dfa8c9824d`.
Schema v435 was verified by sessions #12910 and #12967; gterm was unchanged.

Q01 attempts 3–9 passed binding and failed on stale end-line values left in the
existing index from before the already-committed chunker fix `e447449f5e`.
Six targeted file refreshes exposed the next stale entry each time. One repair
of the same existing 173-file index with `gcode index --full` then completed in
10.549 seconds. No project, worktree, separate service or evaluation index was
created. This was repair of observed stale data, not recurring Ask preparation.
Raw repair result: `/tmp/ask-direct-13038-existing-index-repair.json`.

| Attempt | Run | Failure |
|---|---|---|
| Q01/3 | `40db45d9-7d93-4eb9-8140-8d4d5232a409` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for README.md: requested lines 1..75, blob has 74 line(s) |
| Q01/4 | `0fc84b66-ddbf-47c6-890f-17169e7a61f4` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for docs/research/game-goblins-company-discovery.md: requested lines 181..257, blob has 256 line(s) |
| Q01/5 | `d42acb12-8ad7-4d87-94b3-8b708cb1809a` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for docs/architecture.md: requested lines 1..96, blob has 95 line(s) |
| Q01/6 | `e2e98706-c210-474a-97ff-ce75162c8c1c` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for docs/plans/network-replenishment-v1-original.md: requested lines 451..493, blob has 492 line(s) |
| Q01/7 | `3a3b79ca-c6e8-439a-b29f-e68fff2e6165` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for .gobby/plans/network-replenishment-v1.md: requested lines 631..727, blob has 726 line(s) |
| Q01/8 | `c7a9ab27-ff4e-4fe9-8391-9073050d6bd8` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for docs/plans/daily-weekly-restock-staff-guide.md: requested lines 1..83, blob has 82 line(s) |
| Q01/9 | `4631001c-a64e-4f11-96f4-5a1966a59aba` | MCP step seed failed: gobby-ask:seed returned error: stale_range: stale range for src/game_goblins/__init__.py: requested lines 1..4, blob has 3 line(s) |

Raw attempts use `/tmp/ask-direct-13038-q01-attempt<N>.json` and `.stderr`.

During direct execution, investigator and reviewer self-transitions repeatedly
hit the workflow engine's chain limit after each tool call. Fix `4e66282` ends a
self-transition after one re-entry per event. All 93 step-enforcement tests pass,
with Ruff, mypy and test type/quality/suppression checks clean. This fix is committed
and became live in the coordinated restart described below.

Installed-binary HTTP/MCP checks also passed (3 tests in 2.18 seconds):
`test_shared_run_contract_and_event_driven_wait`,
`test_installed_cli_lifecycle_against_authenticated_service`, and
`test_ask_authorization_and_discovery`, using the isolated test hub and
`GOBBY_GCODE_BIN=/Users/josh/.gobby/bin/gcode`.

## Initial answers and failures

| Question | Attempt/run | Result and source review |
|---|---|---|
| Q01 | Attempt 10, `f7f87349-d7ef-4921-b075-184dec027b21` | Completed, outcome complete. Correctly identifies the local-first Python `game_goblins` operations platform and standalone Restocks/Buylist applications. Cites README lines 1–60 and architecture lines 1–40 and 87. |
| Q02 | Attempt 1, `10bff487-8554-4d26-a609-ca98952e482e` | Completed in 151.5 seconds, outcome complete. Lightspeed is the source of record; PostgreSQL stores normalized mirrors and reproducible operational decisions. Cites architecture lines 1–10. |
| Q03 | Attempt 1, `d79d656c-4c18-4103-add2-b96106379ecc` | Completed in 513.3 seconds, outcome complete. Page records and watermark commit atomically; interrupted incremental pages replay from the stored watermark. Cites `sync_store.py:123–167`, `sync.py:140–215` and the retry test. The cited implementation confirms the shared transaction. |
| Q04 | Attempt 1, `3d6a5fc8-d3e4-4086-8d0b-4fa3ab84fdbc` | Completed in 567.1 seconds after one bounded repair, outcome complete. Explains daily forecasts, Warehouse-to-Little-Rock planning, both CSV artifacts, shadow/publish behavior, dated OPEN-transfer reuse and Slack publication. `daily.py:310–483` confirms shadow returns before mutations and transfer failures skip both uploads. |
| Q05 | Attempt 1, `6a756864-3538-4192-940d-5c6ae307a6e8` | Completed in 515.0 seconds, outcome complete. Orders Conway→Little Rock, Conway→Warehouse, Warehouse→Conway, then Little Rock→Conway seed. Explains running stock updates, available versus projected stock, and why earlier returns cannot immediately be re-shipped. Cites the planner and replenishment guide. |
| Q06 | Attempt 1, `91d036d3-b770-47dd-994b-2bb95cc9cfe5` | Failed in 543.7 seconds during repair: `agent_failed`, `MCP step repair failed: gobby-ask:spawn returned error: Tool call timed out`. No answer was published. Original stdout/stderr retained as `/tmp/ask-direct-13038-q06-attempt1.*`. |
| Q07 | Attempt 1, `c6e16ebf-5849-49c4-8d7e-5d0a07f30ba8` | Completed in 555.1 seconds, outcome complete. Lists all six workbook sheets and order columns; publish uploads the 90-band workbook to Receiving after transfer verification. Cited guide and `weekly_writes.py:380–392` confirm the upload gate. |
| Q08 | Attempt 1, `a5a9883c-eadc-4607-bf87-6f03cf9cc5f5` | Completed in 549.4 seconds, outcome complete. Explains page transaction atomicity, incremental/reconcile/ID cursor restart behavior, duplicate-free page replay, and failure records. All cited excerpts inspected, including `sync.py:146–202,250–284`, `sync_store.py:123–175,230–265` and interruption tests. |
| Q09 | Attempt 1, `e5cd3f74-9ccf-41bc-b8e3-074f9d944228` | Failed after about 546 seconds during repair with the same `agent_failed`/tool timeout as Q06. No publication. Accepted claims cover all three question parts, with no missing parts or global diagnostics; rejected additional clauses nevertheless forced repair in the old daemon. |
| Q10 | Attempt 1, `07efab01-6a66-4a85-8e94-bc993b283894` | Completed in 178.5 seconds, outcome complete. One operational CSV and one unpriced audit per game; internal/public Google Sheets and an aggregate Slack summary. Source review confirms the CSV writer and documented Sheet columns/channel. |
| Q11 | Attempt 1, `c0050efd-c417-4af4-9ad9-e6b891dcba34` | Failed at the 600-second deadline during independent review. Investigation submitted eight claims; review read all thirteen distinct cited excerpts but had not submitted a verdict by the deadline. No publication. Retry will use the user-authorized longer budget. |
| Q12 | Attempt 1, `ee529b13-9e0f-4b0f-8491-3b4236551bc9` | Completed in about 503.7 seconds, outcome complete. Current code already includes the shared Buylist schema and optional PostgreSQL backend; SQLite remains the default. The answer explicitly leaves deployed cutover status unknown. Citations and hashes checked against the schema, backend factory and cutover guide. This appropriately differs from the historical baseline. |

Q01 binding took about 1.2 seconds (grant 31 ms, config 651 ms, search 433 ms,
publish 58 ms). Investigation and independent review both ran successfully.
Publication SHA-256:
`132405b0892c9e919b5322b5f11a5fe2f70f2621d5c1a937661726708172a3e8`.
Publication is under the run's ordinary `~/.gobby/ask/<project>/<run>/publication`.
Q10–Q14 resumed after the coordinated daemon/gterm restart. Each command uses the original
question, the same existing checkout/index and deterministic retrieval. Q01–Q12
started with 600 seconds; remaining questions and retries use 3,600 seconds per
the subsequent user direction.
The original unexecuted frozen-cohort preparation is preserved in Git history.

## Q06 repair finding

The initial independent review accepted claims c3–c6 and marked no question part
missing. Those claims explain the stored-run/status gates and the distinction
between documented review procedure and publish-mode execution. Three additional
claims were rejected for overbroad scope or incomplete negative-search evidence.
Repair was nevertheless mandatory whenever any claim was rejected. The repair
agent launched at 10:57:38 CDT and exhausted its deadline, which reserves the final
60 seconds for review, at approximately 10:59:02. The proxy flattened the timeout
to a generic tool error, and the entire run failed despite the accepted answer.

Fix `b12ca97` admits repair when validated accepted claims leave a question part
unresolved, when the reviewer explicitly reports missing parts, or when validation
has global diagnostics. Publication continues to exclude rejected claims. A
read-only replay of Q06's original draft/reports through the corrected admission
and existing publication selection returns `repair_needed=False`, outcome
`complete`, claims c3–c6. This diagnostic does not change the failed primary run or
substitute for a live retry. The fix became live in the restart below.

The related adapter evidence fix `8ed0080` extends the existing shared-run test to
compare actual installed CLI, MCP registry and authenticated HTTP records while
running and after completion/cancellation, including disconnect survival.
Final focused validation: 88 pipeline/HTTP tests passed in 26.63 seconds with the
installed binary; Ruff, mypy, test-quality and suppression checks passed. The
14-file test-type ratchet has zero new errors (78 existing baseline entries).

Commit `15003aa` additionally exercises installed CLI resume and export through
the real authenticated service. An executor interruption produces a durable
failed run; CLI resume preserves its original binding and completes it. Export
writes the requested tar archive from a reviewed publication, then checks its
manifest and every file byte after extraction. The executor and source fixture
are controlled test boundaries; HTTP, signed authentication, service, durable
storage, resume and export verification are production code. Final HTTP/MCP run:
90 tests passed in 17.92 seconds (`/tmp/ask-cli-resume-export-green.log`), with
Ruff, test-type/quality and suppression ratchets clean. Installed
`gcode contract` exposes start/status/resume/cancel/export and no `--commit`;
captured at `/tmp/ask-installed-contract-13038.json`.

## Coordinated restart and continued execution

Session #12967 completed the coordinated daemon/gterm cycle: daemon PID 32845
became 49189, and old gterm host 49820 became 57123. The new host runs installed
SHA-256 `3316ad9f566f406f8d0d67c6de178d44ecea4364a806f1bfb5087ca254869e9b`.
Its authenticated hello/ping/list control roundtrips correlate request IDs and
complete in under 10 ms; a wrong-token hello returns `invalid_token`. This loads
`4e66282` and `b12ca97` without changing the installed gcode binary or target index.

The serial Python driver was suspended while Q09's child finished, so Q10 could
not launch during restart. No question was interrupted. Its printed Q09 elapsed
value (1980.6 seconds) includes this operator coordination pause; the Ask deadline
and output timestamp show approximately 546 seconds of execution. Q09's read-only
artifact replay through the fix returns `repair_needed=False`, accepted claims
c1/c5/c6/c7/c8/c10. Q06 and Q09 still require separately labeled live retries.

The installed CLI exported the actual completed Q01 run from the restarted daemon
to `/tmp/ask-live-export-13038/ask-f7f87349-d7ef-4921-b075-184dec027b21.tar`.
`replay_publication` verified the manifest and all ten extracted files matched
the original publication byte for byte. Metadata and stderr are preserved at
`/tmp/ask-live-export-13038.json` and `.stderr`. This checks a real published answer
in addition to the isolated API integration test. Native checks also passed:
`cargo test -p gobby-code --test contract --test ask` (3 Ask and 15 contract tests).

#22019 closed after background validator `76326165-bfe9-4721-b339-f4e25acd84a9`
accepted this evidence; post-close memory review completed without changes.
Q10's live bind phases total approximately 666 ms: grant 16.7, config 318.3,
search 308.7, publication 22.7. The post-restart log check found no root-path
index-lock errors or Ask self-transition chain-limit warnings.

Plan commit `0fa0726` applies the user's longer-budget instruction. Q12 finished
before the old suspended wrapper was retired; only that obsolete wrapper exited
137. Its Q12 stdout/stderr are preserved, with no question interrupted. Q13/Q14
primaries and Q06/Q09/Q11 attempt-2 retries run serially with 3,600 seconds, retaining
the same checkout, index and deterministic retrieval.

## Restart interruption during the longer-budget continuation

Q13 attempt 1 created run `0cbbc1cf-0b7e-4f04-bcc7-05e34b4a10a6` with deadline
18:34:33 UTC. Session #13128 restarted the daemon at 12:35:37 CDT for its feedback
fix; its announcement reached this coordinator after shutdown. Q13's foreground
wait disconnected after 65 seconds. The immediately queued Q14 attempt 1 and
Q06/Q09/Q11 attempt 2 starts received HTTP 503 and created no runs. All raw outputs
remain under the original `/tmp/ask-direct-13038-qNN-attemptN.*` names.

After restart to daemon PID 37312, Q13 was failed. A separately recorded
`q13-attempt1-resume1` call returned HTTP 409 because the active agent had become
cancelled. Shutdown also logged `_notify_waiters` accessing the already-closed
PostgreSQL pool. That lifecycle defect is owned by #22021; no fix is claimed yet.
Session #13128 confirmed it will perform no further restarts in its task.

A fresh serial continuation now runs Q13 attempt 2, Q14 attempt 2, and
Q06/Q09/Q11 attempt 3 with 3,600 seconds. It stops on any new start/transport
failure instead of spending the remaining queued invocations during downtime.

### Expired launcher blocked the longer-budget retries

The next five starts used 3600 seconds but failed during `bind_prepare` with
`authenticated session is unavailable`: Q13 attempt 2
(`b640bde1-1c8f-4a54-8208-6c50deb43a8b`), Q14 attempt 2
(`af0cbd3a-3e51-473b-a66d-8156f3a3eb87`), Q06 attempt 3
(`c9c69716-cef9-4570-a9ed-56cca0988542`), Q09 attempt 3
(`e5399f1a-9cf6-4485-b2e3-c13bc42db62c`), and Q11 attempt 3
(`ee749f3d-eed5-4d4c-b426-b232f646b04b`). All raw files remain preserved.

The project launcher `game-goblins#321` had expired at
2026-09-13T17:37:28Z, but the launcher helper selected it without checking
status. Commit `dd164cc` selects active launchers on the local machine, touches
a reused launcher, and registers a new identity when no active launcher exists.
Expired terminal identities remain expired. Validation: all 25 agent spawn route
tests passed against the isolated test hub; focused Ruff, mypy, test quality, test
types and the suppression ratchet passed. A coordinated daemon restart loads it
before further direct Ask attempts.

### Q13 completed after launcher fix

Q13 attempt 3, run `557c2ad6-7126-4170-a3a3-61c39432cea0`, completed
in 286.2 seconds with a 3600-second budget. It distinguishes working company
discovery notes, the superseded Restock MCP draft, the preserved network
replenishment source plan, and the later store-floor/transfer owner direction.
All four published excerpts support their claims; full-file content hashes and
exact byte-range excerpt hashes match the existing checkout. Publication
manifest: `58e9227e0246fb23504ab6b1f0176223793bcf6ecfe3973d9afb3edfb4083c65`.
This is the tenth completed, source-reviewed question. Q14 and the labeled
Q06/Q09/Q11 retries continue serially on the same index.

### Q14 unqualified label was answered incorrectly

Q14 attempt 3, run `1bba701f-1280-4cf4-8138-4757737f89ed`, returned
`complete` in 110.5 seconds, but it called current HEAD `6f43eba0...` “C3” and
reported its one-file `.gitignore` change. This is **incorrect for the intended
C3**: the prompt supplies no mapping from the label to HEAD, and C3 means
`8b24ac26699aac8b24254a647aa70b208287b492`. An independent `git show --stat`
confirms that commit changes nine paths versus its first parent. Runtime
completion is not answer correctness. The raw response and publication
`90decbf7a9dcaa478fff21c312e1d90a2b5e7aa0707d6d09737042b09a11f7cc` remain
preserved. A separately labeled explicit-hash clarification will be evaluated
after the remaining serial questions.

### Q06 completed

Q06 attempt 4, run `277f8193-86d8-4ec1-bb17-6e1ef64373f2`, completed
in 521.5 seconds. It distinguishes the publish-mode code gate, the baseline
command that forces publish, the limited existence/cadence check in
`begin_apply`, and the runbook's explicit operator-approval requirement. The
answer does not invent a universal persisted review approval gate. All five
source excerpts support the claims and match current full-file and excerpt
hashes. Manifest: `15a8186b46e9ede0174d4380a560900dae656bae5e5db0f7d41b3079ffe77260`.
Eleven questions now have completed, source-reviewed answers; Q14 remains an
incorrect unqualified-label answer awaiting a separate clarification.

### Q09 completed; Q11 needs fuller failure behavior

Q09 attempt 4, run `760389eb-be13-498e-8309-d762027fefb6`, completed
in 250.2 seconds. It covers Lightspeed product/inventory/sales inputs,
restock and missing/negative/image calculations, CSV lifecycle and Slack routing.
All seven distinct cited excerpts match the working-tree content and byte-range
hashes and support the published statements. Manifest:
`f15e3ceb5f5f40d5a758668c761127708518c8fdd8397d4225aad365697de79d`.

Q11 attempt 4, run `cc2ce2ab-5167-4022-b32d-20a4c2cb7fc0`, completed
in 338.7 seconds. Its daily incremental and weekly reconciliation descriptions
are supported, as is failure-state clearing on success. All three distinct
excerpts match their source hashes. However, the published answer omits what
happens to the usable catalog after refresh failure; a claim about retained error
fields alone does not fully answer failure retention. This is an answer coverage
defect despite runtime outcome `complete`. Manifest:
`e19b11684a195e0e92a6903ca16bc8b8845eaa2ffeec7a78568c594296d81852`.
The original answer remains preserved; a separate retry follows the reviewer fix.

### Final fixes before clarification

Commit `b3848d8` drains all cached Ask service tasks before daemon storage closes,
blocks late dispatch during shutdown, and leaves admitted pending runs durable
for restart. Focused shutdown/lifecycle validation passed 167 tests; the final
container guard passed another 15 tests.

Commit `c0f6cdd` lets commit-metadata evidence read a requested full commit id from
the same repository's object database. It preserves the live-index source binding
and validates metadata against the recorded request. Historical changed-path
metadata does not turn current file bytes into historical source evidence.
The agent instructions no longer assume an undefined commit label means HEAD.
Reviewers must judge complete coverage using accepted claims alone, rather than
treating one accepted claim with a question-part id as sufficient.

Validation: 367 Ask/HTTP/MCP tests passed against the isolated test hub, with
HTTP CLI tests using the release candidate and native evidence tests using the
branch debug binary; the final prompt/fixture changes passed all 277 Ask tests.
Six Rust evidence unit tests and 21 native Ask/contract/evidence CLI tests passed.
Rust format/Clippy, Python Ruff/mypy, test quality, test types and suppression
ratchets passed. One obsolete snapshot CLI test was removed after it exposed a
leftover assertion for the deleted `--snapshot-json` surface.

The final candidate was installed via a new inode at `~/.gobby/bin/gcode`:
SHA-256 `ae053aa8131583f88a5dbcd47657dab01b5bed27d53948c59797573a9df53e8d`.
After the feedback-review owner released its hold, the coordinated restart
replaced daemon PID 8950 with 75527 and passed readiness in 55.8 seconds.
The existing gterm host PID 57123 survived. Gobby's ordinary index reported
healthy and a successful 3044 ms incremental flush at 18:34:39 UTC.
Q14's clarified run bound in approximately 2.02 seconds (grant 121.3 ms,
config 918.3 ms, search 812.7 ms, publication 164.9 ms), without a new checkout
or index. The Q14 clarification adds only the actual C3 commit hash to the
original question; it supplies no answer-key facts.

### Gobby repository smoke answered

The original question, `Which module owns Ask snapshot preparation?`, completed
against `/Users/josh/Projects/gobby` in 172.8 seconds with a 3600-second budget.
Run `7f3cd319-05e4-4ab7-baa7-f823de8b5f00` names `gobby.ask.snapshots` and
`AskSnapshotManager.prepare_async`. Both published source citations and their
hashes match the checkout. Binding took 959.9 ms (grant 83.8, config 421.6,
search 345.0, publication 109.5). There is no run-owned `source` checkout.
Manifest: `86af530f33ac34af8ee09e6480db911d38df52953e7b9be27a20cba43746264d`.
Raw stdout/stderr: `/tmp/ask-gobby-13038-final.*`.

### Q14 clarification identified paths but not behavior

Q14 attempt 4, explicit-hash clarification, run
`feb6a17a-a38a-424d-8860-a65af5c44bfa`, completed in 697.4 seconds.
It correctly identifies nine changed paths: eight modified and one added.
It cannot describe historical behavior from metadata alone. Its original draft
states that evidence limitation; review of the partially accepted draft also
exposed deterministic coverage validation incorrectly rejecting a reviewer gap
when another accepted claim mentioned the same question part. These are concrete
remaining defects, not a passing complete answer. Manifest:
`0af9d4c7e5b1f379150de6335b0ae8541cb6b9af7f15f0a894b39b892c5f85a5`.
The next fix adds bounded first-parent Git patches to the existing metadata
records and admits semantic reviewer gaps while preserving accepted claims.

### Q11 completed with failure retention covered

Q11 attempt 5, run `0b8911e3-80d3-4ce4-83c3-ad5341de934a`, completed
in 808.2 seconds after one repair. The final twelve claims cover daily incremental
and weekly refresh, candidate-copy validation and atomic promotion, retention on
failure, stored error state, stale results and weekly attempt timing. All thirteen
distinct cited excerpts match source content and exact byte-range hashes; the
candidate/promote and failure-recording implementations were inspected directly.
Manifest: `856120366a3f563688b58474160b5f0a4240ada733376be8251ebd6bb6de7a33`.
The later exception review below found a remaining qualification error.

Commit `dd10bce` adds first-parent patches to commit evidence and includes them
in the canonical record hash. It also preserves supported claims when the
reviewer identifies a semantic gap, while requiring every structurally uncovered
part and rejecting unknown part ids. Validation passed 367 Ask/HTTP/MCP tests,
six Rust evidence tests, 21 native CLI tests, Rust format/Clippy, Python Ruff/mypy,
and test type/quality/suppression ratchets. The native integration helper selects
the branch debug binary independently of `GOBBY_GCODE_BIN`; rebuilding that
binary resolved the initial mixed-build fixture failure. Both debug and release
builds used the current sources for the final passing run.

Installed release SHA-256 for the final Q14 check:
`d7ad080380dab659327740a99ca7f4b168170f88d806e70943bf01c0fc6b4329`.

Further source review of Q11 attempt 5 found one overbroad supplementary claim:
its weekly RuntimeError handling statement must exclude CatalogError, which is a
RuntimeError subclass handled and re-raised first. The main catalog-retention
explanation is supported, but the answer is not yet fully correct. The shared
agent instructions now require checking exception inheritance and handler order;
a separately labeled Q11 retry will verify the correction after Q14 finishes.

### Q14 completed with the exact historical behavior

Q14 attempt 5, using the same explicit-hash clarification, run
`deadf738-e0c8-4207-9c74-f3d005deb97c`, completed in 236.4 seconds.
Its seven claims cite all nine changed paths at C3 versus first parent
`0216f1e33f05962d49467d95fe84609041c6dba8`. Every published patch was independently
compared with `git diff` for that exact pair and path; all nine match.
The answer correctly gives Hobby Supplies = 2 and `Sleeves: ` = 4, case-sensitive
longest-prefix matching, prefix precedence over category minima for automatic,
non-excluded products, the planner/daily integration, and the tests/docs changes.
No historical checkout or index was built. The caller's current-index binding
remains separate from the requested commit's authenticated patch records.
Manifest: `8fae9194bf9c7806b0dabfc6e2fa0ed3fc334b4ce3f2f725c7a93ff1064218ad`.

Commit `c08a8f5` adds the final exception-inheritance and handler-order instruction.
All 277 Ask tests, Ruff, mypy and the suppression ratchet pass. The following Q11
retry uses its original question unchanged after the coordinated Python reload.

## Final question review

Retrieval and answer correctness are assessed separately below. Source excerpts
were checked against file and byte-range hashes; historical patches were checked
against the named commit and its first parent. A successful run is not by itself
a passing answer, as the Q11/Q14 history above demonstrates.

| Question | Final attempt | Run | Retrieval evidence | Answer assessment |
|---|---|---|---|---|
| Q01 | 10 | `f7f87349-d7ef-4921-b075-184dec027b21` | 3 source excerpts verified | Answer supported |
| Q02 | 1 | `10bff487-8554-4d26-a609-ca98952e482e` | 1 source excerpts verified | Answer supported |
| Q03 | 1 | `d79d656c-4c18-4103-add2-b96106379ecc` | 4 source excerpts verified | Answer supported |
| Q04 | 1 | `3d6a5fc8-d3e4-4086-8d0b-4fa3ab84fdbc` | 4 source excerpts verified | Answer supported |
| Q05 | 1 | `6a756864-3538-4192-940d-5c6ae307a6e8` | 5 source excerpts verified | Answer supported |
| Q06 | 4 | `277f8193-86d8-4ec1-bb17-6e1ef64373f2` | 5 source excerpts verified | Answer supported |
| Q07 | 1 | `c6e16ebf-5849-49c4-8d7e-5d0a07f30ba8` | 6 source excerpts verified | Answer supported |
| Q08 | 1 | `a5a9883c-eadc-4607-bf87-6f03cf9cc5f5` | 7 source excerpts verified | Answer supported |
| Q09 | 4 | `760389eb-be13-498e-8309-d762027fefb6` | 7 source excerpts verified | Answer supported |
| Q10 | 1 | `07efab01-6a66-4a85-8e94-bc993b283894` | 3 source excerpts verified | Answer supported |
| Q11 | 6 | `880df8e5-6633-4fd3-a4c4-1f855dea461d` | 8 source excerpts verified | Answer supported, including exception precedence |
| Q12 | 1 | `ee529b13-9e0f-4b0f-8491-3b4236551bc9` | 4 source excerpts verified | Answer supported |
| Q13 | 3 | `557c2ad6-7126-4170-a3a3-61c39432cea0` | 4 source excerpts verified | Answer supported |
| Q14 | 5, clarified | `deadf738-e0c8-4207-9c74-f3d005deb97c` | 9 exact historical patches verified | All required change details supported |

These direct checks use the existing current checkout for Q01–Q13. They do not
produce a comparable gold-span score against the old frozen 6/14 baseline; no
regression or improvement versus that historical benchmark is claimed. Q14
uses the actual C3 patch, with the explicit-hash clarification kept distinct
from its original ambiguous-label attempts.

### Q11 final exception qualification verified

Attempt 6 asked the original question unchanged with a 3600-second budget.
Run `880df8e5-6633-4fd3-a4c4-1f855dea461d` completed in 835.4 seconds after
one bounded repair. Its fourteen claims explain daily incremental refresh,
weekly reconciliation, candidate validation and promotion, retention of the
committed catalog, stored failure metadata, stale fallback and fatal failures.
It explicitly identifies `CatalogError` as a `RuntimeError` subclass that is
re-raised before the weekly generic handler, and limits the seven-day retry
deferral to failures actually recorded through that handler.

All eight distinct citations match the working-tree file hashes, exact byte-range
hashes and published excerpt bytes. Direct inspection of the cited synchronizer,
SQLite and PostgreSQL failure-recording methods, automation entry point and
exception declaration supports the claims. This resolves the attempt-5 finding.
Raw output: `/tmp/ask-direct-13038-q11-attempt6.json` and `.stderr`.
Manifest: `0d3264b060eaf8a7399ae6528d9361e56051f153ac1016cdc3792616c42c27f2`.

### Workspace disposition

The clean `epic-22010-native-ask` workspace was verified landed on `0.5.0`,
marked merged and deleted through the managed worktree tools. The older clean
`task-22019-ask-interfaces` workspace has no commits beyond its stored creation
base `cec05e6ce327d30b2a191e85fb3751d7d43a41fe`, but that inherited history is
not fully contained in the current branch. Its ownerless managed row
`c46e20c2-9606-4f9a-8a31-0d6e77abe7c6` was explicitly released and marked
abandoned; a follow-up query confirmed that state. Its files and branch remain
preserved for historical reference. No unmerged commit was force-deleted, and
no active implementation depends on that workspace.
