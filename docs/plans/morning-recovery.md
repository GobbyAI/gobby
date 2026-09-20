# Morning recovery — 2026-09-20

Recovery snapshot from session `gobby#14019`, following the overnight daemon
instability and reboot. The daemon instability investigation is owned by another
agent. This document records the audit and recommended recovery order; task and
process state must be rechecked when resuming.

The work survived the reboot. Task records, Git branches, dirty worktrees, and
saved transcripts were verified. All the Codex transcripts below exist locally.

## Operating constraints

Keep **one implementation session working at a time**. Let its close validator
finish before starting the next. Give each resumed session this instruction:

> Recover the existing task and worktree. Recheck ownership before editing. Work
> serially; do not spawn implementation workers or restart/cut over the daemon.
> Preserve other sessions' changes.

Three recovery details matter:

- Most interrupted tasks are now **unclaimed**, despite their preserved
  worktrees. `#13991` still appears running and holds `#22561`, but its recorded
  PID `27941` is absent.
- Main contains unfinished work from several sessions, including staged terminal
  tests, a plan archive move, the cohort's intentionally removed answer key, and
  seven files for `#22622`.
- Task closure and landing differ: `#22560` is closed but unmerged; `#22566` is
  merged but still open; `#22596` is closed and merged into the communities lane,
  with landing to main still pending.

## Recommended resume and landing order

Run these individually. Explicit `-C` selects the intended checkout
([Codex reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli#codex-resume)).

### 1. Feedback-fix coordinator — session #13983

Reconcile the interrupted runs first, without restarting its worker batch.
Finish `#22566`'s closure: commit `dcb8032e8d` is already on main, and the session
reported successful sync, but the task retains the earlier invalid verdict.

```sh
codex resume -C /Users/josh/Projects/gobby 01a0bd29-81fa-7e80-a464-454ee2784718
```

### 2. Staged-index preservation — session #13992, task #22564

Clean worktree, commit `ef769870c3`; close review interrupted. Close and land this
before other worktree merges.

```sh
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22564-preserve-staged-gobby-index-entries-acro 01a0bd58-16de-7b51-905b-85c599113fc4
```

### 3. Rejected-verdict reuse — session #13991, task #22561

Clean, committed as `b521a9e24a`; reconcile its stale running record, finish
closure, then land. This reduces duplicate reviews of unchanged rejected
evidence.

```sh
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22561-reuse-a-delivered-rejected-close-verdict 01a0bd56-34f8-7980-bf63-d9b0e089419f
```

Have coordinator `#13983` also land **`#22560` / `f4866d0709`** here. Its worker
`#13990` already obtained a valid closure; another implementation session is
unnecessary.

### 4. Dirty-template startup guard — session #13994, task #22567

Commit `ec01005e33` exists, plus three uncommitted reviewer-fix files. The rejected
criterion concerns reporting the current session's ownership. Finish those
corrections, validate, close, and land before subsequent cutovers.

```sh
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22567-block-restart-and-dev-startup-sync-from 01a0bd5c-faf9-7781-b3b0-7bc67c44b8c8
```

### 5. Unfinished daemon --force change — session #14008, task #22622

This session was outside the screenshot. Seven modified main-checkout files
remain; its recorded test run had **66 passed, 2 failed**. Resume only after the
instability investigator releases this overlapping lifecycle surface.

```sh
codex resume -C /Users/josh/Projects/gobby 01a0be41-ad1e-78b1-a7d4-08c4d48e8600
```

### 6. Acceptance-artifact parsing — session #14004, task #22621

Clean commit `cd866062c3`; closure interrupted. Close and land before resuming
community remapping, whose TDD gate was blocked by this defect.

```sh
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22621-expansion-artifact-prefix 01a0be3a-e655-7a83-8cdd-7be621ee22bd
```

### 7. Jev report — session #13986, task #22623

Report already committed on main as `7624b06efa`; only closure remains.

```sh
codex resume -C /Users/josh/Projects/gobby 01a0bd38-6631-70b2-8288-41b4b270140e
```

### 8. Remaining feedback fixes

Finish these one at a time. All retain uncommitted work.

```sh
# #13997 / task #22562: terminal termination; 11 staged files, review/commit/close remain.
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22562-make-external-terminal-termination-reach 01a0bd64-2465-7592-a447-9bb402493273

# #13993 / task #22565: shell-write ownership; 9 dirty paths, test-quality repairs remain.
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22565-close-unknown-scope-shell-write-gobby-at 01a0bd59-c9ff-7240-882b-55e52d286055

# #13996 / task #22563: exact-phrase search; 9 staged files, review/commit/close remain.
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22563-let-searchtoolresult-express-exact-phras 01a0bd61-ce4a-7342-9cfc-2edbde612d48
```

### 9. Direct-input completion audit — session #13977

`#22573`, `#22579`, and `#22580` are closed, but `#22557` remains escalated for
missing live typing evidence. This session also found incomplete plan
coverage/archive bookkeeping. Reconcile those and the installed-client
verification before continuing broader UI work.

```sh
codex resume -C /Users/josh/Projects/gobby 01a0bd1a-b4e1-7471-9460-aba044428031
```

### 10. Communities coordinator #13940, then remapping worker #13998

Migration `#22594` is closed and landed. The coordinator reopened `#22602` after
finding a SQL binding-order bug; its two-file correction remains uncommitted.
Finish that correction and land it into the communities lane. Then resume
remapping after `#22621` is available.

```sh
codex resume -C /Users/josh/Projects/gobby 01a0bbeb-fff0-7b03-9d7d-f2b3faafbb54

# #13998 / task #22593: three preserved dirty paths; implementation and validation remain.
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22593-community-remap 01a0bd6a-beb4-7d41-9011-6943f9967e11
```

The communities epic still has substantial downstream work: persistence,
views/reporting, evidence/Ask, label generation/Jev, documentation, and
memory-community work. Keep its coordinator from launching the next batch
automatically.

### 11. Gclient workspace UI — session #13936

Resume in its actual worktree, despite the conversation's original main-checkout
directory. `#22617` has 14 modified files; focused UI tests had passed, with
parity/golden validation still underway. `#22618` and `#22619` remain open.

```sh
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22616-gclient-workspace-ui 01a0bbca-c112-71d1-8749-75eeca54c18b
```

### 12. Cohort research — session #13987, task #22405

Run last, after stability is established. Preflight completed; the four-run
cohort was waiting for workers to drain. Its answer-key removal is the large
research-document diff on main. Preserve that experiment state. The task
explicitly records a previous simultaneous four-agent launch overloading the
daemon.

```sh
codex resume -C /Users/josh/Projects/gobby 01a0bd43-2de6-7cf3-a104-88e532c7ef0b
```

## Completed worker sessions retained for reference

These screenshot sessions need **no routine worker resume**, but their commands
are retained for completeness:

```sh
# #13990: task #22560 closed; coordinator only needs to land f4866d0709.
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22560-credit-scope-preserving-runner-reporting 01a0bd54-c443-7181-92f1-ea5a72281fc8

# #14000: original #22602 worker finished; #13940 now owns the follow-up correction.
codex resume -C /Users/josh/.gobby/worktrees/gobby/task-22602-community-label-storage-config 01a0bd70-9331-7ad1-94a8-7791349ee59f

# #13999: #22596 closed and merged into the lane; original worktree was removed.
codex resume -C /Users/josh/.gobby/worktrees/gobby/lane-22581-gcode-import-communities 01a0bd6d-a88f-7852-831d-02a7343c0ea1
```

## Exclusions

`#14011` is a close validator and is excluded, along with nightly reviewers.
`#13984` is a **Claude** session, so it has no Codex resume command; its remaining
UI investigation includes open task `#22620` for shells starting in `/tmp`, which
overlaps the workspace work and should also run serially.
