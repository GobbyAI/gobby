# Where CodeRabbit fits in the lane, reviewer and Merge Manager workflow

Task: #23189 (research spike, P3). Requested by Josh on 2026-10-01: "We should find a way to incorporate coderabbit into our workflow". Researcher gobby#14550. Read-only.

This investigation pushed nothing, opened no PR, installed nothing, accessed no storage and ran no CodeRabbit review. Its only network call was `coderabbit usage`, which returns account data and sends no code.

## Recommendation

The **Code Reviewer seat** should run CodeRabbit as an **advisory** input on the **exact candidate SHA**. It runs in a temporary detached worktree, and the reviewer folds the findings into its own HIGH/MEDIUM/LOW findings.

This adds no new mechanism: no code, pipeline, rule or gate. It needs one bullet in `.gobby/roles/code-reviewer.md` and Josh's answers to the decisions in section 6. It is the only option that binds CodeRabbit's evidence to the same SHA the LAND names, and the reviewer already has to judge findings.

It fits Josh's confirmed **Essentials** plan:
- **Per-candidate runs are small.** The median is 6 files, and about 98% of candidates fit the 150-file cap. A Merge Manager package pass (package 3 was 210 files) would have to be split.
- **The volume fits.** The ~36 candidates a day sit within 5 reviews an hour, as long as a busy hour may skip with a note.

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
  - The binary holds no hard-coded file limit. It also builds narrower-scope candidates (`--committed`, `--uncommitted` or up to five `--dir` scopes) with a fits/does-not-fit flag. The CLI never picks one or retries by itself **[docs-latest, cli/reference]**.
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

These figures come from merges on 0.5.0 since 09-24:
- **287 landings in 8 days:** 63, 34, 31, 32, 51, 44, 16 and 16 per day, so about 36 per day on average with bursty peaks.
- **Files per landing:** p50 = 6, p90 = 49, max = 1034. That's 6,724 files in total.
- **Landings over the cap:** recounted at 0.5.0 b1dc981f1f, 7 of the 298 merges since 09-24 changed more than 150 files, and 2 changed more than 300.
- **Packages:** package 3 (#23190 at b1dc981f1f) changed 210 files against its 0.5.0 parent, over the Essentials cap.

Fit on the confirmed Essentials plan, against the alternative of upgrading to Team:

| Limit | Essentials (current) | Team (upgrade) |
|---|---|---|
| Reviews per hour, shared by all 5 reviewer seats | 5. About 120 a day at the most, so it fits the ~36/day average. A burst of more than 5 candidates in one hour has to wait or skip. | 8. Comfortable, including re-reviews after a BOUNCE. |
| Files per run | 150. About 97.7% of candidates fit (291 of 298). An over-cap candidate fails with `too_many_files`; reviewing it needs split runs. | 300. Only 2 of 298 candidates exceed it. |

Cost if credits were ever enabled: a p50 candidate is 6 × $0.25 ≈ $1.50, and a p90 candidate is about $12.

## 4. Integration points

### A. Author pre-CANDIDATE self-check
- **Signal:** low independence. The author picks which findings to act on, and the reviewer never sees the rest. Its real value is catching defects before a reviewer round trip. `pre-push-test.sh` already offers this for humans.
- **Latency and load:**
  - Adds 1–5 minutes to every candidate, including both runs per bounced candidate.
  - All lanes share one hourly limit, the most runs of any option, so it saturates first.
- **Failure mode:** findings are silently dropped or partially applied, and a rate-limited lane stalls or skips without anyone knowing.
- **LAND and receipt:** no interaction. Fixes produce new SHAs before the CANDIDATE, so the LAND contract is unchanged, and the reviewer still sees nothing CodeRabbit said.

### B. Reviewer-seat input on the exact SHA (recommended)
- **Signal:** high. CodeRabbit is an independent model and toolchain (ruff, shellcheck, clippy, osv and the repository's `path_instructions`). It lands at the decision point, and a reviewer who already must verify findings filters it, so false positives cost one triage line, not a code change.
- **Latency and load:**
  - Adds about 1–5 minutes per candidate inside the reviewer's own pass. It can run while the reviewer reads the diff.
  - It's one run per review (BOUNCE re-reviews included), with no heavy-run key needed: it's a network call plus a local `git worktree add`.
- **Failure mode:** a rate limit, `too_many_files` (a candidate over 150 files on Essentials, about 2% of landings), an auth expiry or an `error` event leaves the reviewer without CodeRabbit input. An over-cap candidate can still get partial coverage: the reviewer reruns with one of the CLI's narrower `--dir` candidates, each of which costs another review from the hourly 5. As advisory, the reviewer records "CodeRabbit: unavailable (<reason>)" and proceeds. False positives are triaged like any lead (skill contract: "findings are leads, not patches").
- **LAND and receipt:**
  - The review runs on the same SHA the LAND names. `internalState.json.reviewedCommitIds` and `git.json.head` record that SHA, so the LAND evidence can cite "CodeRabbit run <end-ms> on <sha>: N findings, M adopted".
  - Blocking findings go through the existing BOUNCE and bounded correction loop. The single correction pass counts them like any other finding, so the loop stays as it is.
  - A new SHA needs a new run.

### C. Merge Manager package pass on the staged tree
- **Signal:** only for cross-candidate interactions, which are rare. Most of it duplicates per-candidate findings, attributed to the wrong owner.
- **Latency and load:**
  - Packages bundle about 16 positions, so hundreds of files. Package 3 changed 210 files, which is over the Essentials cap of 150 files per run. A package pass would have to be **split** into several `--dir`-scoped runs, each costing one of the 5 hourly reviews. Splitting by directory also cuts across candidates and loses the cross-file context the pass was meant to add. Expect 7–12+ minutes per run. Cost scales with total files (about $0.25 each on credits).
  - It holds the MM reservation longer and blocks every other landing.
- **Failure mode:** findings arrive **after** the LAND approval. MM may only "fix gaps caused by integration" and must return semantic changes to an independent source reviewer, so each actionable finding reopens review for an already-approved candidate in the middle of a reservation.
- **LAND and receipt:** it conflicts with the contract. The LAND approves a source SHA, and a package finding against the merged tree doesn't map to any single LAND.

### D. Pipeline step (headless, daemon-run)
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

## 5. Proposed role text and procedure (the Orchestrator applies it after Josh decides; not edited here)

Proposed bullet for `.gobby/roles/code-reviewer.md`:

> - CodeRabbit (advisory, Josh 2026-10-xx): for each CANDIDATE, run CodeRabbit on the exact candidate SHA in a temporary detached worktree. Verify every finding against the code like any lead, and fold valid ones into your HIGH/MEDIUM/LOW findings. Cite the run in the LAND evidence: SHA, finding count and adopted count. If the run fails (rate limit, `Too many files`, auth, error event), note "CodeRabbit unavailable: <reason>" and continue. A CodeRabbit finding never blocks by itself.

Procedure (CLI 0.7.3; do not run until Josh approves the data egress):

```bash
SHA=<exact candidate sha>
WT=/private/tmp/cr-review-${SHA:0:10}
git -C /Users/josh/Projects/gobby worktree add --detach "$WT" "$SHA"
cd "$WT" && coderabbit review --agent --committed \
  --base-commit "$(git merge-base "$SHA" 0.5.0)" > "/private/tmp/cr-${SHA:0:10}.ndjson"
# parse the `finding` and `complete`/`error` events; do not trust the exit code on 0.7.3
git -C /Users/josh/Projects/gobby worktree remove "$WT"
```

`--base 0.5.0` in place of `--base-commit` matches the 07-29 precedent (`baseBranch: origin/0.5.0`). `--base-commit <merge-base>` pins the base exactly, so the reviewed range equals the candidate's own commits.

## 6. Decisions for Josh (Telegram buttons)

| ID | Question | Choices |
|---|---|---|
| D1 | ~~Plan tier~~ **Answered 2026-10-01: Essentials (5/h, 150 files per run).** Remaining choice: | `Stay on Essentials (recommended)` · `Upgrade to Team (8/h, 300 files)` |
| D2 | Adopt CodeRabbit as a Code Reviewer input on the exact SHA? | `Adopt (recommended)` · `Not now` |
| D3 | Advisory or blocking? | `Advisory (recommended)` · `Unresolved HIGH blocks LAND` |
| D4 | What code may be sent to CodeRabbit? | `All candidate paths` · `Exclude .gobby/ and roles` · `Only src/ and crates/` |
| D5 | CLI version? | `Stay on 0.7.3 (recommended)` · `Update to 0.8.x (review only; never use handoff, which uploads the session transcript)` |
| D6 | When the plan allowance runs out? | `Skip and note it (recommended)` · `Enable credits with a cap` |
| D7 | Which candidates get a run? | `Every candidate (recommended)` · `Only src/ or crates/ changes` |

On Essentials with D7 "Every candidate", choosing D6 "Skip and note it" keeps the reviewer moving through hours with more than 5 candidates and past the rare candidate over 150 files. Upgrade to Team only if skipped runs become common.

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
  - `git log 0.5.0 --merges --since=2026-09-24`
- **Memories:** f5537d2c, f153518c, 245c154c, baba0f0a.
