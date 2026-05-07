# GitHub Issue Triage

Gobby can intake GitHub issues through a webhook-first triage path. The cron
reconciler is a recovery path for missed deliveries, disabled webhooks, or local
setups that cannot receive public GitHub traffic.

## Architecture

The Mermaid diagram below is the source-of-truth architecture render.

```mermaid
flowchart TD
    GitHub[GitHub issues webhook] --> Route[POST /api/github/webhooks/triage/{project_id}]
    Route --> HMAC[Validate X-Hub-Signature-256 over raw payload]
    HMAC --> Deliveries[gh_triage_deliveries idempotency and audit]
    Deliveries --> Accepted[202 Accepted]
    Deliveries --> Service[GitHubIssueTriageService]

    Cron[Reconciliation cron] --> Service
    Cron --> ListIssues[github:list_issues open issues]
    ListIssues --> Service

    Service --> Fetch[github:get_issue when needed]
    Service --> Qdrant[VectorStore/Qdrant collection gobby_github_issues]
    Qdrant --> Dedup[Project-scoped semantic duplicate search]
    Dedup --> Gates[Deterministic gates]
    Gates --> Judge[triage-agent + triage-judgment JSON verdict]
    Gates --> Outcome[Python outcome gates]
    Judge --> Outcome

    Outcome --> Tasks[Create or update linked Gobby task]
    Outcome --> Build[build_task / build automation]
    Outcome --> Comments[GitHub comments]
    Outcome --> Labels[GitHub labels]
    Outcome --> Close[github:update_issue state=closed]
    Outcome --> Audit[gh_issues_triaged verdict and vector point]

    Merge[PR merge / direct merge loop] --> MergeTool[gobby-tasks-ops:record_merge_result]
    MergeTool --> CloseTool[gobby-tasks-ops:close_linked_github_issue]
    CloseTool --> Comments
    CloseTool --> Labels
    CloseTool --> Close
```

## Setup

1. Store the webhook secret in Gobby secrets, for example
   `$secret:github_triage_webhook`.
2. Enable project triage with `PUT /api/projects/{project_id}/github-triage`.
3. Configure GitHub manually in v1 to send `ping` and `issues` events to
   `POST /api/github/webhooks/triage/{project_id}`.
4. Select `issues` actions `opened`, `edited`, and `reopened`.

Use `GET /api/projects/{project_id}/github-triage` to inspect the saved project
configuration. Inbound webhooks return `202 Accepted` after validation and
delivery persistence. Disabled triage returns `403`; missing, invalid, or
unauthorized webhook input returns `401`.

Project config fields:

- `enabled`: enables triage for webhook and reconcile paths.
- `webhook_enabled`: allows inbound webhook deliveries.
- `repositories`: `owner/repo` values. If empty, legacy project `github_repo`
  is used as a single-repo fallback.
- `reconcile_interval_seconds`: recovery scan interval. The default is `3600`
  seconds, and values must be greater than zero.
- `webhook_secret_ref`: secret reference, not the secret value.

Required webhook headers:

- `X-GitHub-Event`
- `X-GitHub-Delivery`
- `X-Hub-Signature-256`

Delivery statuses in `gh_triage_deliveries` are `pending`, `processing`,
`processed`, `ignored`, `duplicate`, and `error`. `ping` deliveries are recorded
as `processed`, `issues` deliveries for `opened`, `edited`, and `reopened` start
as `pending`, unsupported events or actions are recorded as `ignored`, and a
repeated `X-GitHub-Delivery` is recorded as `duplicate`.

## Labels

Gobby manages these labels:

- `gobby:accepted`
- `gobby:skipped`
- `gobby:duplicate`
- `gobby:needs-triage`
- `gobby:resolved`

`gobby:ignore` on an issue is treated as a deterministic skip gate.

## Comments

Python services, not the judgment agent, write public comments. Implemented
issues get a task link comment, duplicates reference the duplicate issue key,
skips and escalations include the public-safe reason, and merge-close comments
mention the merged Gobby task and merge SHA when available.

The GitHub MCP side-effect calls are `github:add_issue_comment`,
`github:add_labels_to_issue`, and `github:update_issue`. Required GitHub reads
use `github:get_issue` for direct issue intake and `github:list_issues` during
reconciliation.

## Judgment

The judgment layer is side-effect-free and returns one structured verdict:
`implement`, `skip`, `escalate`, or `dedup`. Python applies the side effects
after validating the outcome.

The service applies deterministic checks before any injected judge:

1. Project-scoped semantic duplicates become `dedup`.
2. Issues labeled `gobby:ignore` become `skip`.
3. If a judge is configured, it returns the structured verdict.
4. Without a judge, the fallback verdict is `implement`.

Pull requests are skipped before duplicate search or judgment.

## Audit Tables

- `gh_triage_deliveries`: one row per `X-GitHub-Delivery`, raw payload hash,
  headers, processing status, error text, and replay protection.
- `gh_issues_triaged`: latest per project/repo/issue decision, linked task id,
  content hash, labels, vector point id, dedup key, source, and timestamps.
- `project_github_triage_configs`: project config and secret reference.

## Qdrant Indexing

Issue vectors use the existing Gobby `VectorStore` and the dedicated collection
`gobby_github_issues`. Point ids are deterministic from
`project_id:repo:issue_number`. Payloads include project, repo, issue number,
issue URL, state, labels, updated timestamp, content hash, and optional task id.
Duplicate search is scoped to the project and can match across repositories in
that project.

## Runbook

- Delivery returns `202` but nothing happens: inspect `gh_triage_deliveries`
  status and `error`.
- Delivery is stuck in `processing`: another worker claimed the pending row; use
  the row timestamps and daemon logs to determine whether it is still active or
  failed before retrying.
- Duplicate webhook delivery: same `X-GitHub-Delivery` is accepted as duplicate
  and not processed again.
- Webhook signature failure: verify the GitHub webhook secret matches the
  configured `webhook_secret_ref`.
- Missed webhook: run the project `gobby:github-triage:{project_id}` cron job or
  wait for the next reconciliation interval.
- Merge did not close issue: verify the task has `github_repo` and
  `github_issue_number`, and that the merge agent called
  `gobby-tasks-ops:close_linked_github_issue`.

## Limitations

Webhook registration is manual in v1. Cron-only mode is supported for private or
local setups, but webhook-first is the documented default. Triage judgment is
structured and side-effect-free; Python owns task creation, GitHub comments,
labels, issue closing, vector writes, and build routing.

_Last verified: 2026-05-07_
