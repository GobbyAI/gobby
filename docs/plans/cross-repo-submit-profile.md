# Cross-Repo Submit Profile

## Problem

`gobby build --profile submit` previously stopped before merge but left PR
delivery as an agent convention: the merge orchestrator pushed a branch, called
GitHub directly, and recorded the result afterward. That worked for same-repo
PRs, but it could not express same-organization cross-repo PRs because GitHub's
REST API requires `head_repo` when both source and target repositories share an
organization.

## Schema

Build profiles carry delivery intent:

- `delivery_mode`: `auto` or `pull_request`
- `delivery_target_repo`: optional PR base repository in `owner/repo` form

Bundled profiles use `delivery_mode: auto` except `submit`, which uses
`pull_request`. A project profile can override `submit` and set
`delivery_target_repo` to open PRs against another repository.

Task delivery campaigns persist resolved repository metadata:

- `task_delivery_campaigns.delivery_mode`
- `task_delivery_campaigns.source_repo`
- `task_delivery_campaigns.target_repo`

PR units persist opened or reused PR metadata:

- `task_delivery_units.repo`
- `task_delivery_units.source_branch`
- `task_delivery_units.target_branch`
- `task_delivery_units.pr_url`
- `task_delivery_units.github_pr_number`
- `task_delivery_units.pr_state`

## Resolution Flow

During build profile resolution, profile fields are overlaid onto
`BuildOptions`. When `delivery_mode` is `pull_request`, build records a delivery
campaign for the root task.

Source repo resolution order:

1. Project `github_repo`
2. Project `github_url`
3. Git `origin` remote from `repo_path`

Target repo resolution order:

1. Profile `delivery_target_repo`
2. Resolved source repo

The merge orchestrator reads campaign state and calls
`gobby-tasks-ops:open_delivery_pr`. The tool pushes the source branch, checks
for an existing open PR, opens one if needed, and records the delivery unit
idempotently.

## Examples

Same-repo submit:

```yaml
name: submit
delivery_mode: pull_request
delivery_target_repo: null
```

If the project resolves to `acme/app`, `gobby build --profile submit` records
`source_repo=acme/app` and `target_repo=acme/app`.

Cross-repo submit:

```yaml
name: submit
delivery_mode: pull_request
delivery_target_repo: acme/upstream
```

If the current project repo resolves to `acme/app`, build records
`source_repo=acme/app` and `target_repo=acme/upstream`.

## GitHub API Constraint

GitHub's create-pull-request REST endpoint accepts `head` as the branch where
changes were made and requires `head_repo` for cross-repository pull requests
when both repositories are owned by the same organization. Gobby uses GitHub MCP
for same-repo and cross-owner PRs because its schema can express `head`. For
same-organization cross-repo PRs, Gobby calls GitHub REST directly and includes
`head_repo`.

Reference: https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request

## Acceptance Criteria

- Bundled `submit` resolves to `delivery_mode=pull_request`.
- Project profile overrides can set `delivery_target_repo`.
- Invalid `delivery_target_repo` values fail profile validation.
- `gobby build --profile submit` records campaign `delivery_mode`,
  `source_repo`, and `target_repo`.
- `open_delivery_pr` reuses local delivery state or an existing GitHub PR before
  creating a new PR.
- Same-organization cross-repo PR creation uses REST with `head_repo`.
- PR URL, repo, source branch, target branch, PR number, and state are persisted
  in `task_delivery_units`.
