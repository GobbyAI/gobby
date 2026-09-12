# PR delivery

Load before remote publication, PR creation/review, or protected-branch landing.

Discover `gobby-merge:probe_branch_protection` and `get_delivery_state`,
`open_delivery_pr` and the PR recording tools on `gobby-tasks-ops`.
Discover an installed GitHub connector or operator CLI separately;
a connected server is not automatically an installed skill.

1. Read the campaign/task artifacts, existing delivery state, target and approved
   publication scope. Probe the exact repository and branch; retain the source
   and any limitations of the returned protection evidence.
2. Persist the observed gate state through `record_pr_state`. If direct delivery
   is allowed by both policy and probe, record that choice and follow the task's
   PR-stage verdict workflow. Unknown or inaccessible policy is not an approval.
3. For a required PR, use `open_delivery_pr` within the authorized publication
   scope. It pushes by default, reuses or opens a PR, and persists its metadata.
   Provide the intended source/target repositories and branches, title and body.
   For a PR created separately, persist it with `record_pr_opened`.
   Write a concrete description and validation evidence.
   Never publish unrelated commits or post reviews without authorization.
4. Inspect CI, reviews and mergeability; persist each meaningful snapshot. When
   waiting on CI or humans, yield/finish the worker so dispatch can resume it.
   Do not keep an agent alive in a polling loop or approve its own protected PR.
5. Resolve an out-of-date source through the authorized branch-update flow.
   The existing methodology permits one local resolution fallback after the
   remote update fails; retain its attempt count and escalate recurring conflicts.
6. Record `record_pr_verdict` with approve, request_changes or needs_discussion
   according to observed evidence. Land only after the required gates and user
   authority are satisfied, then record merge evidence and clean up isolation.

Probe output reflects the current implementation's GitHub protection request and
fallback evidence; inspect repository rules and permissions when that result is
inconclusive. A yolo/unattended setting does not grant bypass of branch protection,
human decisions or remote publication authority.

`open_delivery_pr` reports `ok`, not `success`. A stored PR URL can produce an
idempotent return without pushing or checking GitHub again; verify live PR state
before treating reuse as evidence of current mergeability. Failed opening records
a blocked campaign with its error; inspect remote and delivery state before retry.

Operator release work additionally verifies every package's version source,
changelog, lockfile and actual release workflow triggers. Do not assume a Python
version bump covers native packages or that every tag starts the same workflow.
Release/tag/PR actions require their own authorized scope and observed CI results.

See [source-control HTTP](../../../../../../../../docs/guides/http-endpoints.md#source-control-files-projects-and-config)
and the repository's `.github/workflows/` definitions for detailed carrier behavior.
