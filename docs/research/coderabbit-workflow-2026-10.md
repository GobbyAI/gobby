# Where CodeRabbit fits in the lane, reviewer and Merge Manager workflow

Task: #23189 (research spike, P3). Requested by Josh on 2026-10-01: "We should find a way to incorporate coderabbit into our workflow". Researcher gobby#14550. Read-only.

This investigation pushed nothing, opened no PR, created no branch on the remote, installed nothing, accessed no storage and ran no CodeRabbit review. It made two network calls, and neither sent code: `coderabbit usage`, which returns account data, and one `git ls-remote --symref origin HEAD`, which confirmed that the default branch is `main`.

## Josh's rulings

Josh's rulings so far, in order. The latest one governs.

1. 2026-10-01: "On coderabbit, we can batch together multiple commits. Thought is we have merge manager do the same before merges, so we keep under the limit".
2. 2026-10-01, 16:22 CT: "CodeRabbit won't work until after we push, and then we have to make sure we're packaging < 150 reviewable file changes for a CodeRabbit pass."

Ruling 2 sets the constraint for the rest of this note:
- CodeRabbit runs only on pushed commits.
- Every CodeRabbit pass covers **fewer than 150 reviewable changed files**.
- Pre-landing runs are out. That covers the author self-check, the reviewer run on the exact SHA and an MM package pass on the staged tree.
- Ruling 1's batching survives as **package sizing**: the Merge Manager keeps each landing small enough to be one post-push batch.

Sections 1–3 are background on the CLI and the repository and remain accurate. Sections 4–6 apply the post-push constraint, and section 7 is the batch plan for the current backlog.

## Recommendation

Run **post-push batch reviews**, one review-only pull request per batch, each covering fewer than 150 reviewable files. Then triage the findings into ordinary fix tasks with the existing `coderabbit` skill.

- **Sizing (MM, pre-landing, no CodeRabbit run):** the Merge Manager keeps every package landing under 150 reviewable files. It counts them with `git diff --name-only <first parent> <package merge>`, after the `.coderabbit.yaml` `path_filters`. It records the count and the SHA range in the package receipt. An oversize package is split into two landings.
- **Review (Josh, post-push):** Josh pushes each batch end as a throwaway `cr/` branch. He opens a pull request against the previous batch's branch and comments `@coderabbitai review`.
- **Triage:** the PD routes each pull request's findings through `$gobby coderabbit` (the canonical skill: native Plan Mode, a fix/no-fix table, verify before fixing) to the lane that owns the paths, as normal fix tasks. The code has already landed, so findings feed fix work and never gate a LAND.

This adds no code, pipeline or gate. It needs:
- a sizing bullet in `.gobby/roles/merge-manager.md`;
- a routing bullet for the PD;
- Josh's answers to section 6.

It is the only option that satisfies both rulings with existing mechanisms.

## 1. Current usage

### Editor extension
- CodeRabbit is normally run locally through the editor extension (memory f5537d2c). Josh pastes the findings into a session as `$gobby coderabbit <findings>` (memory f153518c).
- On disk, `coderabbit.coderabbit-vscode` 0.21.3, 0.21.6 and 0.21.7 are installed under `~/.antigravity-ide/extensions` and `~/.cursor/extensions`.

### CLI
- The CLI at `~/.local/bin/coderabbit` (alias `cr`) is version **0.7.3**: a 62.5 MB arm64 Mach-O binary. `~/.coderabbit/doctor.json` records its install event on 2026-08-19.
- `coderabbit usage` reports: organization GobbyAI, user joshwilhelmi, usage billing **inactive**, 0 reviews this period, and a reset on 2026-10-29. It does **not** show the plan tier.
- Local history, from `~/.coderabbit/stats.json`: 6 reviews (05-14 to 08-19) with 64 findings. `~/.coderabbit/reviews/` holds 12 review records dating back to 2026-03-18.

### Repository hooks
- `pre-push-test.sh` (lines 544-564) runs `coderabbit review --agent --type all > ./reports/coderabbit-<ts>.md` as a **non-gating** step. CI's `postgres-pgsearch-smoke.yml` runs the same script; the step skips there because the CLI isn't installed.
- `--type` is missing from 0.7.3 `--help`. I checked that 0.7.3 accepts `--type all` while it rejects an unknown flag (`error: unknown option '--bogus-flag-xyz'`), so `--type` is a hidden option and the step isn't broken.

### Skill and config
- The canonical findings workflow is the bundled skill `src/gobby/install/shared/skills/coderabbit/SKILL.md` (memory 245c154c). It covers the native Plan Mode gate, a per-finding fix/no-fix table, `./reports/coderabbit-*.md` ingestion and cleanup, and CLI-failure reports such as `Too many files`.
- `.coderabbit.yaml` sets:
  - `profile: assertive`
  - `path_instructions` for `src/**/*.py`, `tests/**/*.py`, `mcp_proxy`, `hooks`, `crates/**/*.rs`, `Cargo.toml` and `Cargo.lock`, including the psycopg `%s` rule from #19205
  - the tools ruff, shellcheck, clippy, osvScanner and `ast-grep: {}`
  - `path_filters` that exclude completed plans, `uv.lock` and caches.
- `ast-grep` is a rule surface, not a pluggable analyzer (memory baba0f0a). `auto_review.base_branches: [main, dev]` affects only PRs.

### Other history
- **Past remediation tasks:** #9865, #14995, #15012, #17313 and #18909. The remediation epic is in `.gobby/plans/completed/coderabbit-fixes/`; it split one 786-finding pass into domain fixes and forward-ported them to 0.5.0.
- **Former `no-coderabbit` rule:** it appears only in `docs/plans/completed/workflows-v2.md` and in `tests/workflows/test_agent_scope_rules.py`, which seeds its own row. It isn't among the current `src/gobby/install/shared/workflows/rules/` templates. The installed DB rule rows are authoritative (repository rule 8), and this task's no-storage constraint meant I didn't read them, so whether any installed rule blocks agents from running the CLI is **unverified**. Check the installed rules before adoption.

## 2. CLI capabilities on 0.7.3

Each fact is tagged with its source:

| Tag | Source |
|---|---|
| **[help]** | `coderabbit --help` / `review --help` on the installed 0.7.3 |
| **[local]** | Artifacts under `~/.coderabbit/` |
| **[changelog]** | docs.coderabbit.ai/changelog, entries ≤0.7.3 |
| **[docs-latest]** | Docs that describe 0.8.1 (2026-09-25) and aren't tied to a version |
| **[undocumented]** | No source found |

### Selecting what to review
- **Local working tree only [help]:**
  - Modes: `--committed`, `--uncommitted` and `--include-untracked`.
  - Base: `--base <branch>` or `--base-commit <commit>` ("Base commit on current branch for comparison").
  - Scope and depth: `--dir <path>` and `--light`.
  - Extra instructions: `-c/--config <files...>`.
- **No exact-SHA or commit-range argument [help].** The reviewed side is always the current checkout. To review an exact SHA, check it out, for example as a detached worktree.
- Remote mode (`--remote owner/repo --source-branch <branch or 40-char SHA>`) takes a SHA but reads from GitHub and needs ≥0.7.7 **[docs-latest]**. It isn't available here and would need a push anyway.
- **Precedent for a detached exact-SHA review [local]:**
  - Run: review `1785283702868` on 2026-07-29 for #19205.
  - Recorded state: `currentBranch: HEAD`, `head: 07bae16b6a…`, `baseBranch: origin/0.5.0`, `workingDirectory: /private/tmp/gobby-19205-review.*`, `reviewedCommitIds: [07bae16b6a…]`.
  - Version caveat: that run predates the 0.7.3 install event (08-19), so it ran on an earlier 0.7.x. 0.7.3 still has `--base`/`--base-commit` **[help]**, and nothing documents a prohibition. I didn't re-run it on 0.7.3, to avoid sending code.

### Output
- **Formats:** plain text by default, or `--agent` for structured findings **[help]**. The documented `--agent` format is NDJSON events (`review_context`, `status`, `heartbeat`, `finding`, `complete`, `error`). A finding carries `type`, `severity`, `fileName`, `codegenInstructions`, `suggestions` and `comment` **[docs-latest]**. Since 0.7.2, `--agent` errors come back as a single structured error **[changelog]**.
- **Later findings:** `coderabbit review findings [--dir]` re-reads the last local review **[help]**.
- **Local records:** each review stores `git.json` (head, base, branch, worktree), `diff.json`, `incrementalDiff.json` and `internalState.json` (`reviewedCommitIds`) under `~/.coderabbit/reviews/<repo>/<id>/reviews/<end-ms>/` **[local]**.
- **Exit codes:** the 0.7.3 exit code for a failed or incomplete review is **[undocumented]**. "Failed or incomplete exits 1" applies only from 0.7.7. Nothing says findings alone produce a nonzero exit. Consumers must parse the `complete` or `error` event, not the exit code.

### Auth
- Commands: `coderabbit auth login` (browser OAuth; `--agent` for agent OAuth), `--api-key` and `--region us|eu` **[help]**. The region flag arrived in 0.7.2 **[changelog]**.
- An API key must be an "Agentic API key" from an organization where the user holds a seat, and billing follows that key's organization **[docs-latest]**.
- Every session on this machine authenticates as **joshwilhelmi in GobbyAI** through the stored login. I didn't open `~/.coderabbit/auth.json`.

### Rate and cost
- **Plan: Essentials.** Josh confirmed this on 2026-10-01: "I'm on the Coderabbit essentials plan. Can upgrade if the need arises."
- Two separate limits apply. A per-run file cap and an hourly review count.
- **Per-run file cap [docs-latest, management/plans]:** at most 150 files per review on Free and Essentials, 100–300 on OSS, and 300 on Team, Advanced and Enterprise. The docs say this is "the maximum number of files CodeRabbit reviews in a single review, not an hourly limit". Josh's estimate of a 150-file cap matches the Essentials row.
- **How 0.7.3 enforces the cap [local, installed binary strings]:**
  - The server reports the limit. The CLI parses "Too many files! This PR contains N files, which is X over the limit of M." and emits error code `too_many_files` with `retryable: false` and `actionRequired: true`.
  - The strings I searched show only a server-reported limit (`M` parsed from the error, plus `maxFiles` fields). They don't contain a client-side file-count constant, but absence from a strings search doesn't prove the binary has none. It also builds narrower-scope candidates (`--committed`, `--uncommitted` or up to five `--dir` scopes) with a fits/does-not-fit flag. The CLI never picks one or retries by itself **[docs-latest, cli/reference]**.
  - There is a separate client-side cap of `MAX_DIFF_SIZE_MB=20` on the diff, and `payload_too_large` means "diff too large".
- **Hourly limit per developer [docs-latest, management/plans]:** Free 3, OSS 3, **Essentials 5**, Team 8, Advanced 10, Enterprise 12.
  - The five Code Reviewer seats (gobby#14641, #14680, #14681, #14944 and #14945) would all count as **one** developer.
- **Usage-based add-on [docs-latest]:**
  - Price: $1 per credit, 4 files per credit, so $0.25 per reviewed file.
  - It applies only after the plan allowance is exhausted, only on Essentials, Team or Advanced, and only with an explicit `--use-credits`.
  - In headless mode the CLI returns `action_required` and never charges silently.
  - Billing for GobbyAI is currently **inactive** **[local, `coderabbit usage`]**.
- 0.7.3 added `coderabbit usage`, an estimated cost in the completion summary and a warning at 75% of the spending cap **[changelog]**.

### What leaves the machine
- **[undocumented]** for a local review. The docs don't say whether only the diff or full files and repository context are uploaded.
- The local record keeps `diff.json` and `incrementalDiff.json`. That shows what was reviewed, not what was uploaded. The safe assumption is **at least the full diff of the selected range, plus `.coderabbit.yaml` instructions**, and possibly surrounding file content.
- What can widen what is sent:
  - `-c CLAUDE.md AGENTS.md` sends those instruction files with a review.
  - The v0.8.0 changelog describes an automatic Claude Code or Codex session-transcript upload for the **cloud handoff** command (`cr handoff --summary ...`), not for `cr review` **[changelog]**. I found no evidence that updating the CLI makes ordinary reviews send transcripts. The recommended workflow never uses `handoff`.
- **Retention [docs-latest, coderabbit.ai/security and FAQ]:**
  - "nothing is stored after the review" unless review caching is enabled.
  - Cached data is never used for training.
  - SOC 2 Type II and GDPR.
- Zero-data-retention agreements with model vendors appeared only in search snippets; I didn't confirm them on a primary page.

### Latency
Measured from local records as `git.json.timestamp` (start) to the review directory name (end):

| Date | Head | Branch | Diff entries | Seconds |
|---|---|---|---|---|
| 07-29 | 07bae16b6a | detached `HEAD` | 1 | 38 |
| 07-29 | 6c563322c9 | 0.5.0 | 13 | 164 |
| 08-19 (0.7.3) | c9c6048bc5 | 0.5.0 | 1 | 290 |
| 04-04 | three runs | 0.3.3 | 77–89 | 143–183 |
| 05-15 to 05-18 | three runs | 0.4.x | 222–264 | 440–712 |

A typical candidate takes about 1–5 minutes, and packages of 200+ files take 7–12 minutes. Only the 08-19 run is certainly 0.7.3.

## 3. Throughput being served

These are first-parent merges on 0.5.0 since 09-24, counted at b1dc981f1f. Each one is a landing that a reviewer approved, and its size is the merge diff against the first parent. Command: `git log b1dc981f1f --first-parent --merges --since=2026-09-24`, then `git diff --name-only <m>^1 <m>` for each merge.

An earlier draft counted all-ancestry merges, which include merges inside candidate branches, so those aren't review candidates.
- **232 landings in 8 days:** 25, 30, 31, 27, 45, 42, 15 and 17 per day, so about 29 per day on average with bursty peaks.
- **Files per landing:** p50 = 6, p90 = 26, max = 210. That's 3,125 files in total.
- **Landings over the cap:** 2 of 232 changed more than 150 files: package 3 (#23190, 210 files) and one sync merge, 7678c85f78 (153 files). None changed more than 300.
- **Packages:** package 3 (#23190 at b1dc981f1f) changed 210 files against its 0.5.0 parent, over the Essentials cap.

Fit on the confirmed Essentials plan, against the alternative of upgrading to Team:

| Limit | Essentials (current) | Team (upgrade) |
|---|---|---|
| Reviews per hour, shared by all 5 reviewer seats | 5. About 120 a day at the most, so it fits the ~29/day average. A burst of more than 5 candidates in one hour has to wait or skip. | 8. Comfortable, including re-reviews after a BOUNCE. |
| Files per run | 150. 230 of 232 landings fit. An over-cap landing fails with `too_many_files`; reviewing it needs split runs. | 300. All 232 fit. |

Cost if credits were ever enabled: a p50 candidate is 6 × $0.25 ≈ $1.50, and a p90 candidate is 26 × $0.25 ≈ $6.50.

## 4. Integration points under the post-push constraint

Ruling 2 rules out the first three options. Each is kept here with the original analysis condensed.

### A. Author pre-CANDIDATE self-check: ruled out (pre-push)
The original analysis already rated its signal low: the author filters its own findings, and the reviewer never sees them.

### B. Reviewer-seat input on the exact SHA: ruled out (pre-push)
This was the earlier recommendation. It bound evidence to the LAND SHA, but it runs before any push. Its strengths carry over to E as triage quality: an independent toolchain whose findings a verifier filters.

### C. Merge Manager package pass on the staged tree: ruled out (pre-push)
Ruling 2 rules it out explicitly. It would also have had to split oversize packages and attribute findings after the LAND. The original analysis follows for the record.
- **Signal:** only for cross-candidate interactions, which are rare. Most of it duplicates per-candidate findings, attributed to the wrong owner.
- **Latency and load:**
  - Packages bundle about 16 positions, so hundreds of files. Package 3 changed 210 files, which is over the Essentials cap of 150 files per run. A package pass would have to be **split** into several `--dir`-scoped runs, each costing one of the 5 hourly reviews. Splitting by directory also cuts across candidates and loses the cross-file context the pass was meant to add. Expect 7–12+ minutes per run. Cost scales with total files (about $0.25 each on credits).
  - It holds the MM reservation longer and blocks every other landing.
- **Failure mode:** findings arrive **after** the LAND approval. MM may only "fix gaps caused by integration" and must return semantic changes to an independent source reviewer, so each actionable finding reopens review for an already-approved candidate in the middle of a reservation.
- **LAND and receipt:** it conflicts with the contract. The LAND approves a source SHA, and a package finding against the merged tree doesn't map to any single LAND.

### D. Pipeline step (headless, daemon-run): not recommended
A post-push variant could drive the CLI or the GitHub app from a pipeline. It still carries every cost below, and the hourly limit and the 150-file cap still apply.
- **Signal:** same as B, but without a reviewer filtering it, so raw findings are noisy (`profile: assertive`; 64 findings over 6 reviews).
- **Latency and load:** the most new mechanism of any option:
  - a pipeline definition and a step runner
  - stored credentials, through `--api-key` or a daemon-owned login
  - parsing NDJSON because 0.7.3 exit codes are undocumented
  - a rate-limit queue
  - storing and routing findings to the reviewer
- **Failure mode:**
  - Exit codes are unreliable on 0.7.3, so the pipeline needs its own event parsing.
  - Headless `action_required` stalls the pipeline at the allowance.
  - A step that gates anything is a new gate. The Lane Manager role says no unapproved gate or throttle without Josh's approval.
- **LAND and receipt:** it could stamp `reviewedCommitIds` into a receipt automatically. That benefit is all that B lacks, and B can cite the same field by hand.

### E. Post-push batch pull requests, sized by the MM (recommended)
- **Signal:** high. It uses the same independent toolchain as B (ruff, shellcheck, clippy, osv and `path_instructions`). The `coderabbit` skill's verify-then-decide contract filters its output. A batch spans several candidates, so it also sees cross-candidate interactions, which was C's only unique signal.
- **Latency and load:**
  - Lanes and reviewers wait on nothing, because it runs after the code lands and Josh pushes.
  - Volume:
    - Batch reviews: one per batch. The current backlog is 6 batches (section 7). From here on it is about one per package.
    - Runs: well inside Essentials' 5 per hour. When more than 5 batches queue up at once, as with this backlog, trigger them about an hour apart.
  - Sizing costs the MM one `git diff --name-only` count per package. It needs no CodeRabbit run and no network.
- **Failure mode:**
  - **Diff over the cap:** a batch at 150 or more files fails with "Too many files" and nothing is reviewed. Sizing prevents this. The cut rule in section 7 recovers an oversize range.
  - **No auto-review:** `auto_review.base_branches` is `[main, dev]`, and the default branch is `main`. A pull request into a `cr/` branch is therefore never auto-reviewed. The manual `@coderabbitai review` trigger is the documented path. **It is not yet verified on this repository**, so confirm it on batch 1.
  - **Wrong base:** if Josh pushes `0.5.0` before he opens batch 1, a pull request based on `0.5.0` shows an empty diff. Pinning the base as `cr/0.5.0-b0` prevents this.
  - **Findings ignored:** routing to fix tasks through the skill makes every finding an explicit fix or no-fix row.
- **LAND and receipt:**
  - LAND is unchanged; findings never gate it.
  - The package receipt gains one line, "CodeRabbit batch: `<from>..<to>`, N reviewable files". The PD's triage maps each pull request back to its batch range, and so to the package and task whose paths it touches.

## 5. Proposed role text and procedure (the Orchestrator applies it after Josh decides; not edited here)

Proposed bullet for `.gobby/roles/merge-manager.md`:

> - CodeRabbit batch sizing (Josh 2026-10-01): keep every package landing under 150 reviewable changed files. Count them with `git diff --name-only <first parent> <package merge>`, minus `.coderabbit.yaml` `path_filters`. Record "CodeRabbit batch: <first parent>..<package merge>, N reviewable files" in the package receipt. If staging reaches 150, split the package into two landings. Never run CodeRabbit before a push.

Proposed bullet for the PD role:

> - CodeRabbit triage (Josh 2026-10-01): when Josh reports a batch pull request reviewed, route its findings through `$gobby coderabbit` to the lane that owns the paths, as ordinary fix tasks. Findings never reopen a LAND.

Procedure for Josh, per batch k (k = 1..N, using section 7's table):

```bash
# once, before pushing 0.5.0 itself: pin the current remote base
git push origin <from_1>:refs/heads/cr/0.5.0-b0
# per batch
git push origin <to_k>:refs/heads/cr/0.5.0-b<k>
gh pr create --base cr/0.5.0-b<k-1> --head cr/0.5.0-b<k> \
  --title "CodeRabbit batch k/N" --body "Review-only; do not merge."
gh pr comment <pr> --body "@coderabbitai review"
# after review: close the PR unmerged; delete the cr/ branches when all batches are done
```

- Open each pull request as non-draft, because `drafts: false`.
- Because `<from_k>` is an ancestor of `<to_k>`, each pull request's diff is exactly that batch's range.

## 6. Decisions for Josh (Telegram buttons)

| ID | Question | Choices |
|---|---|---|
| D1 | Adopt post-push batch pull requests with MM package sizing? | `Adopt (recommended)` · `Not now` |
| D2 | Plan tier? Answered as Essentials (5 per hour, 150 files per run). | `Stay on Essentials (recommended)` · `Upgrade to Team (8/h, 300 files: the backlog needs at least 3 batches)` |
| D3 | How is each batch triggered? | `Manual @coderabbitai review (recommended)` · `Add a cr/ pattern to auto_review.base_branches` |
| D4 | Where do findings go? | `Fix tasks for the owning lane (recommended)` · `Hold the next push until they are triaged` |
| D5 | What happens to the current backlog? | `Review all 6 batches (recommended)` · `Only from package 5 onward` |
| D6 | When the allowance runs out? | `Wait for the next hour (recommended)` · `Enable credits with a cap` |

D3's alternative edits `.coderabbit.yaml`. The setting takes regex patterns according to [docs-latest]. That is unverified on this repository.

## 7. Batch plan for the current backlog

**Range.** `origin/0.5.0` = `2d1d73f579` (2026-09-29) to `0.5.0` = `9a514a7cf1`, counted 2026-10-01 at 16:22 CT. It is a fast-forward of 315 commits, 55 of them first-parent, with 600 net changed files. 599 files are reviewable, because `uv.lock` is the only path that `path_filters` excludes.

**Method.** Each count is `git diff --name-only <from> <to>` with `.coderabbit.yaml` `path_filters` applied, which is the pull request's diff. The cut rule works in three steps:
1. Walk `0.5.0`'s first-parent history and end each batch at the furthest commit that keeps the range under 150.
2. Where one first-parent commit alone is 150 or more, cut inside its second parent, the package branch.
3. Check that each `from` is an ancestor of its `to`.

Every batch's raw count, before filters, is also under 150, so the result does not depend on whether CodeRabbit counts filtered paths.

| Batch | from (exclusive) | to | Reviewable files | Commits | Ends at |
|---|---|---|---|---|---|
| B1 | `2d1d73f579` | `aaa466e4ff07d99c7d503ba9117385b7d60ade3c` | 143 (raw 144) | 59 | #23145 roster and coordination policy |
| B2 | `aaa466e4ff` | `63d90c3b479c2d6a8bed0b88364ee220b32de0b8` | 110 | 64 | #23171 typing and closing reference |
| B3 | `63d90c3b47` | `8fd2bfd3f2521b512cbeef97eb4d18ca49675f50` | 120 | 30 | #23184 SRT cursor |
| B4 | `8fd2bfd3f2` | `724e4d848d36afa77a788b529e25d8c06d983420` | 125 | 52 | inside package 3 (#23190): bounded websocket shutdown |
| B5 | `724e4d848d` | `b1dc981f1fbc440398c603788236af4a362dc2ad` | 100 | 36 | package 3 merge |
| B6 | `b1dc981f1f` | `9a514a7cf1060efb6aacd709598cbac374c7951d` | 109 | 74 | `0.5.0` head: package 4, docs batch, #23268 |

- **Package 3 split:** package 3's merge `b1dc981f1f` alone changes 210 files, so no first-parent cut fits. B4 and B5 cut on its second parent, `a7320ef60d`, at `724e4d848d`.
- **Totals:** the batches add up to 707 file reviews against 599 net, because some files change in more than one batch.
- **Batch count:** 599 / 149 means at least 5 batches. The greedy cut gives 6.
- **Rate limit:** Essentials allows 5 reviews per hour, so trigger B6 an hour after B1.
- **Later landings:** package 5 onward starts at `9a514a7cf1`. With MM sizing, each package is one batch.

## Sources

- **Installed CLI:** `coderabbit --help`, `coderabbit review --help`, `review findings --help`, `config --help`, `usage --help`, `stats --help` (v0.7.3). `coderabbit usage` output (2026-10-01).
- **Local artifacts:** `~/.coderabbit/doctor.json`, `stats.json`, and `reviews/*/*/reviews/*/{git,internalState}.json`.
- **Docs:**
  - https://docs.coderabbit.ai/cli/reference
  - https://docs.coderabbit.ai/cli/overview
  - https://docs.coderabbit.ai/cli/headless-cli-integration
  - https://docs.coderabbit.ai/changelog
  - https://docs.coderabbit.ai/management/plans
  - https://docs.coderabbit.ai/management/usage-based-addon
  - https://docs.coderabbit.ai/cli/claude-code-integration
  - https://www.coderabbit.ai/security
  - https://www.coderabbit.ai/faq
- **Repository:**
  - `.coderabbit.yaml`
  - `pre-push-test.sh:544-564`
  - `src/gobby/install/shared/skills/coderabbit/SKILL.md`
  - `.gobby/roles/{_common,code-reviewer,merge-manager,lane-manager,roster}.md`
  - `git log b1dc981f1f --first-parent --merges --since=2026-09-24`
  - Section 7:
    - `git rev-list --first-parent origin/0.5.0..0.5.0` for the cut candidates.
    - `git diff --name-only <from> <to>` for the counts, filtered by `.coderabbit.yaml` `path_filters` through Python `PurePosixPath.full_match`.
    - `git merge-base <from> <to>` to confirm ancestry.
  - `git ls-remote --symref origin HEAD` for the default branch.
- **Memories:** f5537d2c, f153518c, 245c154c, baba0f0a.
