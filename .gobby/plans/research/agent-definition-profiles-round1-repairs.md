# Working context: #22902 round-1 repairs (Plan Writer gobby#14578)

State: canonical `.gobby/plans/agent-definition-profiles.md` = commit e7f285471a,
unchanged since review. Evidence 954b3556 (snapshot 7e975ca8) is open; append
and finalize are blocked on #22912 (Lane 3 gobby#14531 after #22680, PD GO
pending). The private draft `/Users/josh/.claude/plans/tidy-inventing-flute.md`
now carries every round-1 repair and validates by absolute path
(`uv run gobby plans validate <draft> -p /Users/josh/Projects/gobby`).

Repairs applied in the draft (finding -> where):

- PA-001 spawn bound: Decision 6; 2.2 ledgers `plan_writer_planning_tasks` /
  `plan_writer_enhancer_tasks`, rules track-claim / enhancer-only /
  enhancer-consumed, no re-arm; acceptance 2.2.2 rewritten. Advisor fix
  (2026-09-25): track-claim no longer filters on a task category the
  `claim_task` / `create_task` payloads do not carry; every successful claim
  is ledgered.
- PA-002 step scope: P3 intro (instance flags only via
  `gobby-workflows:set_variable(scope="step")` or handlers); every seat's
  allowlists name it; 3.5 invariant.
- PA-003 skill gates: Decision 7 (researcher joins the workflow set); P3 intro
  `load_skills` first step; developer `route_skills`; researcher
  `load_skills -> serve`; 3.5 invariant.
- PA-004 loop resets: developer (assigned_task_id plus assigned_task_ref
  bound from `get_task` `ref` in route_skills so the CANDIDATE `TASK=#NNNNN`
  match works; close_task handler),
  code-reviewer (candidate_task, verdict handler), log-monitor (tick_window,
  report handler), plan-adversary (evidence_id, finalize handler in resolve
  only), plan-writer (checkpoint step, round_* flags, close_task handler);
  3.5 engine tests `test_seat_step_loops.py`, acceptance 3.5.5.
- PA-005 checkpoint: Decision 14, 3.4.5, D3 (#22912, Lane 3).
- PA-006 write scope: Decision 8; 2.2 `seat-write-scope.yaml` with
  `write_paths_within` in new `condition_helpers_paths.py`, registration
  moved out of `safe_evaluator.py` (865 lines, no growth); 3.1 / 3.3 prose;
  acceptance 2.2.4, 2.2.5, 3.3.2.
- PA-007 stored bodies: 1.1 target `agents.py::parent_body`, consumers
  unchanged (sync.py, imports.py), acceptance 1.1.4.
- PA-008 rollout: Decision 13, 4.1 rollout paragraph, V1 live check.
- PA-009 seat tag: Decision 2.
- PA-010 test retargets: 3.4 per-module table.
- PA-011 coverage doc: 3.2 target `docs/contracts/plan-coverage.md`.

Advisor pass 2026-09-25 also: P3 intro states handler `tool_input` carries
promoted MCP arguments (`_step_handler_tool_input`); plan-writer `handoff`
allowlist adds `gobby-plans:update_plan_hash` and `link_commit` (memory
5538e963); adversary approval payload sent from `await` is now explained
(transition fires in the finalize pass). Draft revalidated: passes.

Next, in order:

1. Wait on gobby#14579 (`wait_for_coordination`). When retry is supported:
   `append_plan_changelog_round(evidence_id="954b3556-9531-49b2-86cf-8f576146bf25",
   prose=<dispositions>, round_result=<canonical result verbatim from message
   5c6cf6a7-1d1c-4025-897a-aa713af1b072>)`, ack by `send_message`, wait for
   the Adversary's finalize confirmation.
2. Only then: copy draft -> canonical, validate canonical, `git add` the plan,
   `ocr delegate rule --format json <path>` (code-review gate), commit
   `[gobby-#22902] docs: apply round-1 adversary repairs` with `-m` flags,
   `link_commit`, notify Adversary (fresh round, lead with the changelog) and PD.
3. Keep #22902 open until the round completes and the PD confirms Josh's
   approval; then #22895.
