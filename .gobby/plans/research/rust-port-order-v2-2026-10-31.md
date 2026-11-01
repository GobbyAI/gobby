# Proposed burn-down v2: retire Python daemon, then port route families

Date: 2026-10-31 (America/Chicago)  
Supersedes: [`rust-port-order-v1.md`](./rust-port-order-v1.md)

## Source files

| Attachment | What it is |
| --- | --- |
| `11d0692b…md` | Plan **#25341** (cloud big-bang) — **not a requirement**; slice map / family IDs / D5 roots still useful |
| `b5d25981…md` | Research **#25237** — dependency encyclopedia; hub-node-first schedule **not** followed |
| `1689f027…md` | Research **#25351** — runner-first; supervised Python worker; **no PyO3** |
| `3735db30…md` | **Parity ledger** — 1116 surfaces; **1071 proxied**, 41 dropped, **4 delegated** |

**Product sequence:** (1) attestation + self-sufficient `default` (Codex) → (2) Rust burn-down under the execution model below. Task/rules families (**B0, C1–C4**) wait until attestation settles.

**Dropped as gates (vs #25341 / v1):** cloud D3 “no `.py`”, cloud chunk/usage-window sizing, “R0 must be special-cased local-only”, DevTeam / developer+reviewer **pair pilot**, hub-node-first as the spine.

**Kept:** dependency order (#25351 + Stage 2); runner before domain ports; attestation before task/rules natives; parity ledger as Stage 3 exit meter.

---

## Execution model

Porting runs on a **dedicated port branch** cut from `0.5.0` (name is operator choice, e.g. `port/1.0`). Two **`default`** agents cooperate asynchronously; neither is a DevTeam seat.

### 1. Porter (`default`)

- Implements one landable task at a time on the port branch.
- **One task = one commit** (subject `[gobby-#N] …`). That commit must stand alone for review—no “branch-wide” dump.
- Closes the task with **its own attestations** on that commit: TDD (red evidence before green), implementation, validation, OCR self-review, linked commit—whatever the post-attestation policy requires of `default`.
- Starts the next task without waiting for review (non-blocking).

### 2. Trailing reviewer (`default` + code-reviewer skills)

- Runs **asynchronously behind** the porter.
- Reviews **each task commit individually** (diff/attestations for that SHA only—not an accumulated branch review).
- Writes a **review attestation keyed to that commit SHA** (pass or fail + findings).
- Does not author the port implementation on the happy path.

### 3. Promotion

- **Only review-pass commits** are promoted/merged into `0.5.0` (fast-forward or cherry-pick of reviewed SHAs—operator/Orchestrator policy).
- **Failed review** opens a **fix task** (new leaf, new commit) targeting the failing SHA’s findings; it does **not** block the porter from continuing other work. Prefer fixing in **Rust on the port branch**; do not patch Python “to make the port easier” unless it is a Phase 0 design-level flaw.
- Fix commits are reviewed the same way; promotion follows the fix SHA once it passes.

### Implications for sizing and exits

- Every phase leaf must be **reviewable alone** at one SHA (owned paths + tests + attestations in that commit).
- Phase **parity exit** = ledger rows for that phase’s owners are `delegated` (or `dropped by decision #N`) **and** each contributing commit has a **pass** review attestation before it sits on `0.5.0`.
- Oracles below are what the porter runs before commit; the trailing reviewer re-checks evidence at the SHA.


### 4. Port strategy: behavior as-is, fix in Rust

Default rule for every family commit:

- **Port observed Python behavior as-is** into Rust (parity with the ledger/oracles), then improve in Rust once native.
- **Do not** use the port to clean up incidental Python bugs. If the trailing reviewer (or porter) finds a **behavior** bug while porting, **record a post-native fix task** keyed to the surface/SHA; leave the Python implementation alone unless it blocks the native cutover.
- **Exception — design-level flaws fixed before that family ports** (Python/YAML/product work, usually Phase 0 or a pre-family leaf):
  - Attestation / closure rework (evidence in task metadata; obligations in rules; `default` self-sufficient close).
  - Hardcoded runbook / role / rule-name couplings (see coupling inventory: e.g. `code-reviewer` step id, remediation rule frozensets, `devteam` strings, role-named errors).
- Runbook-cohort machinery is not “ported faithfully” into Rust as a product requirement: **prune or drop-by-decision** (ledger / task tree), then port the remaining agent/dispatch surfaces.

New leaves that appear during the port should come primarily from the **trailing reviewer** (fail → fix task; pass-with-debt → post-native fix task), not from replaying the entire #25237 drain list.


---

## How to read each phase

- **Parity exit:** ledger rows that must leave `proxied` (counts from the attached ledger).
- **Porter tasks:** prefer **<1 hour** leaves when files allow; split #25351 L-slices into single-commit checkpoints.
- **Oracles:** porter-side proof before the single commit; reviewer verifies at SHA.

---

## Phase 0 — Attestation product gate (blocks B0/C*)

| | |
| --- | --- |
| **Work** | Codex: `default` claims → implements → validates → commits → closes with no overrides; obligations in YAML; strip hardcoded role/rule names. This is the **design-level exception** to as-is porting—finish before B0/C* (and before relying on close/land natives). |
| **Parity exit** | None. Optional: confirm **#23341** drops (39 stage surfaces) remain dropped. |
| **Porter tasks** | None for Rust; product gate only. |
| **Oracles** | One leaf closed by `default` under enforced rules. |
| **vs v1** | No developer/code-reviewer **pilot** as a product gate. Trailing reviewer appears only once port commits exist (Phase 1+). |

---

## Phase 1 — Runner facade + optional checklist tooling (R0)

**Goal:** callable lifecycle contract; inventory every timer/service/continuation (#25351 R0).

| | |
| --- | --- |
| **Parity exit** | No new domain rows. Preserve already **delegated**: `WS transport /ws`, `WS transport /ws/{path:path}`, `WS message subscribe`, `WS message unsubscribe`. |
| **Porter tasks (1 commit each)** | (1) Lifecycle inventory + fixtures from `runner.py` / `runner_lifecycle_*.py`. (2) Versioned private control handshake stub. (3) Python facade: init phases without `run_daemon`. (4) Optional checklist generator scripts—nice-to-have, not a gate. |
| **Oracles** | Facade unit tests; `runner: python` still boots; review attestation on each SHA before promote. |

---

## Phase 2 — Rust owns daemon lifecycle (R1–R7)

**Goal:** `runner: rust` starts/recovers/schedules/stops; domain still in supervised Python worker. **No PyO3.**

| Checkpoint | Commits (porter) | Parity exit | Oracles |
| --- | --- | --- | --- |
| **R1** selector + handshake | 1–2: bootstrap `runner: python\|rust` both parsers; handshake/refuse bad override | none (lifecycle) | unit + isolated spawn both selectors |
| **R2** init / readiness | 2: storage/config/content; service graph + startup vs recovery ready | none | readiness + content-refusal |
| **R3** ordered recovery | 3: hook barrier; sessions/handoffs/wakes; agents/pipelines | none | restart order fixture; no cron before barrier |
| **R4** periodic / cron / automation | 3: maintenance iteration; cron claim; pipeline/system automation | none | exactly one scheduler owner |
| **R5** hook/MCP/WS **handles** | 2: admission/events; drain handles | none (HTTP `/api/hooks/execute` stays proxied) | cancel/drain tests |
| **R6** shutdown / fencing | 2: quiesce/drain; fault/restart/lease-loss | none | drain budget; WS SESSION_END before hook-worker close |
| **R7** dual-mode smoke | 2: isolated matrix; canary/rollback notes | none | #25351 smoke list; never two effectful runners on prod DB |

**Phase 2 exit:** each R* commit review-pass + promoted; Rust/Python lifecycle transcript parity. Ledger may still show ~1071 proxied.

---

## Phase 3 — First route families (no attestation dependency)

### 3a — A1 runtime / config (**#21559**)

| | |
| --- | --- |
| **Parity exit** | All **26** proxied #21559 rows → `delegated` after their commits are review-pass + promoted: `/api/config/*`, `/api/runtime/config`, handshake, MCP `gobby-config:*`, `gobby-hub:get_machine_id`. P6 **#24474–#24477** local follow-ups as separate commits if still open. |
| **Porter tasks** | One commit per cluster, e.g. read paths; patch/import/secrets; runtime handshake; MCP config tools; ledger shard update. Prefer leaves #24452–#24473. |
| **Oracles** | HTTP contract tests / corpus; `gdaemon compare-report` for config; trailing review per SHA. |

### 3b — A2 tool gateway (**#24881**, #24884–#24885)

| | |
| --- | --- |
| **Parity exit** | Gateway discovery/policy/dispatch surfaces for #24881 leaves → `delegated`. Compat backend may remain Python until C3. |
| **Porter tasks** | Native policy/discovery commit; worker-boundary wire-up commit; compare routing commit. |
| **Oracles** | Gateway unit tests; one tool call native policy + Python compat; per-SHA review. |

**Not in Phase 3:** B0 #21560.

---

## Phase 4 — Wave B domain ports (tasks/rules deferred)

| Step | Roots | Proxied | Parity exit (post promote) | Porter commits | Oracles |
| --- | --- | --- | --- | --- | --- |
| **4a B1** | #21562 | 63 | sessions CLI/HTTP/MCP | ~8–12 by verb cluster | session corpus + compare; review each SHA |
| **4b B3** | #21566, #23275 | 31+1 | `mcp-proxy*` (non-flip), mux HTTP/MCP; `mcp-server` | add/list/status; OAuth; import/remove; stdio last | mux + stdio smoke |
| **4c B2** | #21563 | 80 | `memory*` CLI/HTTP/MCP | CRUD; dream; graph; reindex; export | memory tests |
| **4d B4** | #24149, #21564 | 129 | agents CLI + most `MCP gobby-agents:*` + HTTP | spawn/list/kill/status first; **runbook-seat MCP** (`admit_team`, `check_runbook_seats`, …) **defer or drop-by-decision** | agent unit tests; no DevTeam e2e |
| **4e B5** | #21580,#21582,#21585,#21581,#21584,#21589–91 | cron 28, files 12, merge 23, … | per-subfamily rows | one subfamily per commit group | cron/merge/files fixtures |
| **4f B6** | #21565,#21586,#23276 | 54+10 | panes CLI; code-index HTTP; remaining terminal WS app msgs | panes; index GETs | pane + index contracts |
| **4g B7** | #21587,#21588,#21592,#22143,#22075 | voice 11, chat/WS 23, … | voice + chat HTTP/MCP/WS app | voice; chat attachments; inbound WS handlers | voice/comms corpus if present |

**#21568** (253 proxied): follow-on clusters (plans, admin/health, feedback, auth, install, …)—not a Phase 4 gate.

**Wave B exit:** for each owner above, proxied→delegated in ledger shards **and** every promoted SHA has pass review attestation.

---

## Phase 5 — Task engine + rules/workflows (after attestation)

| Step | Roots | Proxied | Parity exit | Porter commits | Oracles |
| --- | --- | --- | --- | --- | --- |
| **5a B0** | #21560 | **123** | all tasks CLI/HTTP/MCP (close/commit/claim/deps/…) | lifecycle clusters against **post-attestation** semantics; node checkout bits may stay needs-local (#24521/#24532) | tasks corpus; close attestation fixtures; optional long compare soak after promote |
| **5b C1** | #24151, #21569 | 7+14 | `HTTP POST /api/hooks/execute` (+ cited node routes); `CLI gobby hooks*` | execute/envelope; hooks admin | s2-11 §1.1 rule corpus; known-bad registry |
| **5c C2** | #21567 | **105** | `pipelines*` CLI/HTTP/MCP | catalog; run/approve/reject; history—**no DevTeam-only assumptions** | non-devteam pipeline e2e |
| **5d C4** | #21583 | 36 | `comms*` | status/send/channels; attach | comms corpus |
| **5e C3** | #21570 | 25 | MCP flip: call-tool/list-tools/schema/search; `HTTP * /mcp` | after B3+C1 | MCP protocol tests |

---

## Phase 6 — Hub/node proof + Python deletion (late)

| | |
| --- | --- |
| **Work** | Optional #24604/#24657/N*; #21544; **#21574** → **#21573**. |
| **Parity exit** | Every required row `delegated` or dropped-by-decision; **#21571** `start/stop/status/restart` delegated. |
| **Porter / reviewer** | Ledger sweep commits; deletion commit; naming-switch commit—each reviewed before promote. |
| **Oracles** | Ledger generator clean; release proof if required. |

---

## Pruning the 1.0 tree

Do **not** burn down #25237’s 490-task / 314-step list task-by-task. **Collapse the open tree to the port phases above**; dependency detail in #25237 remains a reference, not a queue. New work during the port should be **reviewer-spawned fix tasks** (and Phase 0 design fixes), not resurrected cohort drains.

### KEEP (map into phases)

| Chunk / epic | Cite | Maps to |
| --- | --- | --- |
| Runner-first R0–R7 / proposal #25352 | #25351 | Phases 1–2 |
| A1 #21559 P4/P5 (#24452–#24473); P6 local | #25341 slice A1; #21559 | Phase 3a |
| A2 #24881 (#24884/#24885) | #25341 A2 | Phase 3b |
| B1–B7 family roots (#21562, #21566/#23275, #21563, #24149/#21564, B5 octet, #21565/#21586, B7) | #25341 slice map; ledger owners | Phase 4 |
| B0 #21560; C1 #24151; C2 #21567; C4 #21583; C3 #21570 | #25341; ledger | Phase 5 (after attestation) |
| Stage 2 close #21544; Stage 3 #21574/#21573; #21571 when ledger-ready | ROADMAP / ledger | Phase 6 |
| Already-dropped ledger surfaces | #23341, #19319, #23563 | keep dropped |
| Parity ledger exit discipline | ledger header / #21573 | all promote exits |

### DROP from the critical path (close, supersede, or `dropped by decision`)

| Item | Cite | Why |
| --- | --- | --- |
| “Admit six epic DevTeams” (#23561, #23220, #22643, #22948, #24820, #24821) right after drain | #25237 operator order | Cohort expansion; conflicts with retiring runbook-cohort |
| Early #25237 drain leaves that only stabilize DevTeam/Orchestrator topology (#25220 Support tab seats, #25224 team teardown, #25129 end every seat, #25139 devteam relaunch, #25200 Dev Manager roster, #25172 seat-Triage-Orchestrator rewrite, #25223 research runbook as a gate, …) | #25237 steps ~1–24 | Runbook/cohort-specific; not required to port `default` + runner + families |
| Treating #25341 cloud process (D3, usage windows, cloud-only authoring) as mandatory | #25341 | Josh: cloud plan was frustration with the runbook, not a requirement |
| PyO3 embedding; skeleton crates; shared CLI dispatcher A3 | #25351 / #25341 rejected | Stay rejected |
| Porting stage-dispatch / stage-review APIs | ledger #23341 drops | Already retired |

### DEFER (not deleted; not ahead of the port spine)

| Item | Cite | When |
| --- | --- | --- |
| Hub-node live spine (#24604 early transport, #24532, #24657 installer, reserved N2–N4/N1) | #25237; #25341 D5 | Phase 6 / Stage 4—after daemon lifecycle + families, not before |
| Whole #24818 runbooks v4 prep as a port prerequisite | #25237; #25341 D5 | Local/secondary; disable YAML rather than port seats |
| #21568 remaining-route grab bag (253 proxied) | ledger | After Wave B core; cluster when stamped |
| B4 runbook-seat MCP (`admit_team`, `check_runbook_seats`, …) | ledger under #21564 | Drop-by-decision or after cohort YAML gone |
| Stale briefs (#21546 transitional node wording; #23561 old close-module plans) | #25237 stale section | Refresh or fold before any scheduling—do not blind-replay |
| #21546 Stage 4 epic itself | #25237 / ROADMAP | After Phase 5–6 readiness |
| Long Tasks compare soak / fan-out gate paperwork from #25341 P1 | #25341 D11 | Optional after B0 native; not a runner exit |

**Operating rule:** if a leaf is not in KEEP and not a reviewer fix / Phase 0 design fix, it does not block the porter.

---

## Appendix — Scoring a one-shot attempt

Same **as-is port** rule: do not “fix Python first” inside the one-shot; record behavior debt as post-native fix tasks. Design-level attestation/coupling work must already be on the base (or attempted families that need it stay `blocked(attestation)`).


Alternative: one cloud (or local) agent one-shots the port on a branch off `0.5.0`, committing **one family (or one leaf) per commit**, with `runner: python|rust` available.

**Same trailing reviewer** scores the branch **commit-by-commit** (not branch-wide): for each SHA, write a review attestation; **keep/promote only pass SHAs**; failing SHA → fix task (or drop that commit from the promote set) without discarding earlier pass commits.

**Per-commit keep rule**
1. Build/tests for touched crates green at that SHA.  
2. Ledger shard update for surfaces claimed in that commit is honest (`delegated` / dropped-by-decision).  
3. Family oracle / `compare-report`: no unexplained `diff` / `native_error` / `mismatch` for claimed surfaces; writes have a `verified` when applicable.  
4. Runner smoke still passes if `runner: rust` is in play.  
5. Review attestation = **pass** keyed to that SHA.

**Order hint for the one-shot author:** R\* → A1 → A2 → B1 → B3 → B2 → B4 (skip runbook MCP) → B5 → B6 → B7 → stop before B0/C\* unless attestation is already on the base (`blocked(attestation)` if attempted early).

**Scorecard:** pass commits kept / commits attempted; ledger coverage vs 1071 baseline; R7 smoke; B0/C\* not counted as failures if attestation missing from base.

---

## One-page checklist

| Phase | Name | Promote when |
| --- | --- | --- |
| 0 | Attestation + `default` | product gate |
| 1 | R0 facade | each SHA review-pass; WS delegated preserved |
| 2 | R1–R7 | each checkpoint SHA review-pass + lifecycle smoke |
| 3 | A1→A2 | #21559’s 26 rows delegated via review-pass SHAs |
| 4 | B1–B7 | owner rows delegated; runbook MCP deferred/dropped |
| 5 | B0, C1–C4 | after attestation; owner rows delegated |
| 6 | Delete Python | full ledger exit; #21571/#21573/#21574 |

