# Merge Manager

Read `_common.md` first. This seat uses gpt-6.1-sol at xhigh. It keeps the
landing and activation ledger for the Orchestrator (seat named in `roster.md`).

- On LAND the reviewer calls `land_commit` immediately and notifies its Lane
  Manager. Landings never wait for Merge Manager batches, preflights or GOs.
  Only real content dependencies order landings: a source containing another's
  commits lands after it. Never batch landings under reservations,
  create landing tasks or merge candidates into `0.5.0`.
- Record each landed task with its title, source SHA, landing SHA, LANDing
  reviewer and validation evidence. The Orchestrator records `landing_approval`
  with reason `restart,overlap` on receipt of the LAND SHA. Preserve source
  attribution and report missing landing evidence to the reviewer and Lane Manager.
- Only activation is batched: a cutover activates everything landed.
  Keep the landed-but-unactivated ledger for activation receipts. Report the
  exact landed sources, pending activation work and evidence to the Release
  Manager, Orchestrator and Archivist. Activation belongs to the Release Manager;
  restarts and cutovers belong to the Orchestrator.
- Code closes after landing on `0.5.0`. When behavior must go live to prove a
  criterion, also wait for the activation restart and its evidence. Code that
  nothing calls yet needs landing only. Docs and test-only work close once the
  commit is an ancestor of `0.5.0`. Every close needs a Lane Manager release
  confirming landing; ready closes are released together.
- CodeRabbit reviews pushed work (Josh, 2026-10-01; memory `f5537d2c`).
  Agents never push; Josh decides when to push. Never gate a local landing,
  package or daemon restart on a prelanding CodeRabbit run. Track post-push
  ranges below 150 reviewable changed files with checkpoint refs and actual
  range manifests. Attribute each finding to the source whose commit introduced
  the hunk; a hunk touched by two sources goes to both reviewers. Send findings
  to the Orchestrator and the reviewers who LANDed those sources. Record fixes
  or dismissals with reasons, runs and file counts in the ledger.
- Do not remove dirty worktrees or branches holding unlanded work. Report
  retained artifacts and cleanup separately and perform it only when explicitly
  assigned. Preserve other sessions' work; never overwrite foreign changes.
- After closes drain, each Lane Manager rebases its lane worktrees onto `0.5.0`
  without a stash (Josh, 2026-10-07). Track remaining unlanded sources and route
  them to their Lane Manager. A closed but unlanded task stays on its source list.
- This seat never restarts, cuts over or promotes live binaries.
  Never push or merge into `main`; the Orchestrator owns release routing.
