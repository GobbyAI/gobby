# Adversarial plan review

### Adversarial review phase

Start only after explicit adversarial-review approval.

0. Immediately before every adversary round, and after any plan-byte change since
   the previous clean gate, run the deterministic sweep from the project root:

   ```bash
   uv run gobby plans validate <plan-file> -p <project-root>
   uv run gobby plans validate <plan-file> -p <project-root> --mode expansion
   ```

   When either mode reports residue, use a mid-tier internal subagent with this
   prompt: "Load `restraint`, `plan-draft`, and `plan-mechanic`; apply only bounded
   validator-driven repairs to the canonical artifact; rerun both project-aware
   modes; return the `plan-mechanic` report." This is the validator
   rerun-until-clean loop. A `needs-planner` result returns to the planner role;
   repeat the deterministic gate after its semantic repair. Prepare no evidence
   and launch no adversary until both modes are clean. Preserve the final clean
   `plan-mechanic` result, or a zero-residue direct-run result with the same fields,
   as the deterministic sweep report.
1. Call `prepare_plan_review_round` immediately before spawning
   `plan-adversary-taskless` without `task_id`, using `isolation="none"`. Pass
   only `plan_path`, `round_number`, and optional `project`, `session_id`,
   `task_id`, and `stage` preparation inputs. Pass the returned `evidence_id`,
   artifact path, deterministic sweep report, round number, cap, and parent
   session id to the adversary role.
2. Bind the spawned run with `bind_evidence_run`. Expire the evidence if spawn
   or bind fails. After a successful bind, immediately call
   `gobby-sessions:set_handoff` with `clear_session=false`, then use **Waiting on Spawned Runs**. In a
   terminal session that call comes back as a rejected or cancelled tool use
   attributed to the user. That is the daemon interrupting the turn to deliver
   the compaction command, never a refusal: do not stop, do not ask the user
   about it, and resume from the continuation prompt.
3. Read the canonical result. Present every finding with its full text and
   metadata, and collect one accept/decline vote per finding before editing.
   Every vote walks `restraint`'s decision ladder; a finding whose fix adds
   mechanism around a design that already fully solves the problem is declined
   `over-mechanism` with the rung it fails at — fixer-induced chains on the
   previous round's repairs are the usual case. Record declined items and
   deferrals explicitly. In unattended mode, the coordinator judges every item
   and records each vote with its rationale.
4. For a `needs_review` result, persist the rejection checkpoint before applying
   accepted repairs, so the checkpoint records the reviewed artifact and the
   next round snapshots the repaired one. Call
   `append_plan_changelog_round(evidence_id, prose, round_result)`, then
   `finalize_plan_review_evidence(evidence_id, round_result)`. Pass the
   adversary's canonical payload verbatim as `round_result` to both calls; a
   rejection round without it fails with `missing_round_result` unless a durable
   intent already exists. Only after both calls succeed, apply accepted repairs:
   call `apply_plan_review_repairs(evidence_id, accepted_finding_ids)` with
   every accepted finding id — it applies the typed `repairs` those findings
   carry, is idempotent and all-or-nothing, and returns the unified diff plus
   `plan_hash_before`/`plan_hash_after`; then hand-apply the accepted
   prose-only fixes (findings the result lists as `prose_only`) and
   base-validate the artifact. Increment `completed_plan_review_rounds`
   only when finalization succeeds; display attempts, expired evidence, and
   incomplete rounds never count.
5. Present the checkpoint menu after every finalized round and any accepted
   repairs. Choosing
   `continue interactively` after `needs_review` revises and launches the next
   approved review round. Choosing it after `approved` keeps planning open;
   edits invalidate approval and require a fresh reviewed round.
   When a `needs_review` verdict reaches the configured review cap, process
   every final finding and vote before editing, append and finalize the normal
   rejection checkpoint on the unchanged artifact with the canonical
   `round_result`, apply accepted repairs, base-validate, then append a
   human-handoff changelog entry. Do not launch another adversary round.
   Continue only through the explicit human-handoff tools described below.
6. On approval, apply the server-derived manifest, persist the checkpoint,
   finalize evidence, complete lesson-mint checkpointing, and run:

   ```bash
   uv run gobby plans validate <plan-file> --mode expansion
   ```

   Then present the checkpoint menu as the final-approval checkpoint.
