Plan artifact: `.gobby/plans/retire-linear-github-issue-bridge.md`
**Plan ID:** retire-linear-github-issue-bridge

# Retire Linear/GitHub Gobby bridges

## Overview
`kind: framing`

Strip Gobby-owned Linear and GitHub issue/PR identity: import, bidirectional
sync, GitHub issue triage, GitHub PR open/reuse, stored repo/PR fields, and
the `gobby_github_issues` embedding collection. Linear and GitHub stay as MCP
servers so agents can call them directly. Gobby will not open GitHub PRs or
mirror issues into tasks; a later integration replaces this.

Confirmed Decision Record:

1. **Cut inbound issue bridge** — Linear/GitHub import, sync, linkage,
   GitHub issue triage, unwind of `gobby_github_issues`, schema drop.
2. **Keep MCP servers** — `src/gobby/install/shared/mcp/templates/github.yaml`
   and `linear.yaml` plus generic proxy. Agents still call those servers.
3. **Cut GitHub PR client** — `open_delivery_pr`, GitHub MCP/REST PR
   create/reuse, `record_pr_state` / delivery campaign tables, `pull_request`
   delivery mode, `tasks.github_pr_number` / `tasks.github_repo`,
   `projects.github_repo` / `github_url`, task Trace PR link, merge
   orchestrator GitHub PR publication, GitHub API branch-protection probe.
   Local git merge and `git_output` / git dry-run stay.
4. **Delete dead GitHub-MCP source-control HTTP** — `/api/source-control/prs`,
   `issues`, `cicd`, and `GitHubMCPHelper` / `GitHubIntegration`. Keep git-local
   source-control used by BranchIndicator, IsolationTargetSelector, and gclient
   (status, branches, worktrees, clones). Git-only commit listing may remain;
   do not call GitHub MCP from it.
5. **Delete entire `gobby github` and `gobby linear` CLI groups**, including
   `gobby init --linear-setup` and `gobby tasks import_github`. Drop
   `gobby projects --github-repo` / `--github-url`.
6. **No dormant schema** — drop the tables/columns. Existing linkage and
   delivery-PR data is discarded (0.5.0, no backward compatibility).
7. **Worktree off `0.5.0`, no merge** — implement on a dedicated git worktree
   branched from `0.5.0`. Commits stay on that branch. This plan does not
   merge to `0.5.0`, open a PR, or run `gobby-merge`.

The web Integrations tab is Slack/Telegram comms, not this bridge. Leave it.

## Execution
`kind: framing`

Create one linked worktree from the main checkout at current `0.5.0` (for
example `wt-retire-linear-github-bridge`). All edits, commits, and targeted
tests run there.

- Do not merge that branch into `0.5.0` as part of this plan. No Graphite/GitHub
  PR, no `gobby merge`, no fast-forward back to the main checkout.
- Do not start or restart the shared daemon from the worktree
  (`gobby start` refuses a linked-worktree source tree). The live daemon stays
  on the main checkout.
- Do not apply the destructive schema campaign to the live hub. Isolated
  pytest uses `gobby_test`. Schema files land as commits on the worktree
  branch only.
- Canonical plan body remains `.gobby/plans/retire-linear-github-issue-bridge.md`
  once writable in the worktree.

## Constraints
`kind: framing`

- Do not remove or disable the `github` / `linear` MCP templates or the generic
  MCP proxy.
- Do not remove local git merge (`gobby-merge` git operations, `git_output`).
- GitHub API branch-protection probing goes; a git push dry-run may remain.
- `integration_workspace_mutex` is dispatch/workspace merge, not issue sync.
  Leave it.
- Schema drops are a `--destructive` gcore campaign (pattern:
  `crates/gcore/assets/schema/migrations/426_retire_legacy_wiki.sql`). Writers
  must already be gone. Update every derived carrier in the plan-coverage table.
- Load the `rust` skill before gcore schema/grant edits.
- Shared templates under `src/gobby/install/shared/` require regenerating
  `src/gobby/install/bundled_content_manifest.json` in the same commit.
- `src/gobby/ai/embedding_switch_runner.py` is 841 lines; removing the GitHub
  issue builder must shrink it. `projects.py` (765) and `source_control.py`
  (690) also shrink.

Rejected alternatives: keep triage; leave schema columns unused; keep
`gobby github pr` / `link`; keep GitHubMCPHelper for a future UI; keep
Gobby-owned PR delivery while removing issue sync.

## P1: Stop the live inbound loop
`kind: framing`

**Goal:** The daemon no longer runs Linear sync, GitHub issue sync, or triage
recovery in the background. Later leaves can delete services without racing a
live coordinator.

### 1.1 Remove ExternalIssueSyncCoordinator
`kind: deliverable`
[category: code]
[domain: backend]

Targets:
- `src/gobby/sync/external_coordinator.py::*` — operation: delete — scope-reason: retire the inbound Linear/GitHub coordinator
- `src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`
- `src/gobby/runner_lifecycle_periodic.py::_has_enabled_external_issue_integration`
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: cancel `_external_issue_sync_task` only
- `src/gobby/runner.py::*` — scope-reason: drop coordinator attributes on GobbyRunner
- `tests/sync/test_external_coordinator.py::*` — operation: delete — scope-reason: coordinator tests have no remaining subject

**Research context:** `start_periodic_tasks` constructs
`ExternalIssueSyncCoordinator` when `_has_enabled_external_issue_integration`
is true (`runner_lifecycle_periodic.py` ~345–366) and includes
`_external_issue_sync_task` in the periodic tuple. Shutdown sets
`_external_issue_sync_shutdown` and cancels the task
(`runner_lifecycle_shutdown.py` ~226–256). `runner.py` declares
`_external_issue_sync_task`, `_external_issue_sync_shutdown`, and
`external_issue_sync_coordinator`. The coordinator runs `_run_linear` and
`_run_github` (sync + `GitHubIssueTriageService` recovery). CLI/MCP import
paths still exist until 1.2–1.4; they become manual-only for one commit.

Delete the coordinator module and its tests. Remove the start gate, task
creation, shutdown cancel, and runner attributes. After this leaf, enabling
`linear_sync_enabled` or triage config must not spawn a background loop.

**Acceptance:**

- 1.1.1 - Daemon periodic startup does not import or construct `ExternalIssueSyncCoordinator`. symbol: `start_periodic_tasks`. test: `tests/sync/test_external_coordinator.py` (file deleted; remaining runner lifecycle tests still pass).
- 1.1.2 - Runner shutdown has no `_external_issue_sync_*` handles. file: `src/gobby/runner_lifecycle_shutdown.py`.
- 1.1.3 - `gcode grep -w ExternalIssueSyncCoordinator src` is empty. behavior: "no coordinator symbol in src" in `src/gobby/sync/external_coordinator.py`.

**Depends on:** none

## P2: Linear issue bridge
`kind: framing`

**Goal:** Gobby no longer imports, creates, or syncs Linear issues as tasks.
The `linear` MCP server template remains.

Granularity: one Linear-bridge product. Splitting CLI from `LinearSyncService`
would leave `gobby linear` calling deleted modules. Seven production files is
one outcome.

### 2.1 Delete Linear sync, GraphQL, and CLI
`kind: deliverable`
[category: code]
[domain: backend]

Targets:
- `src/gobby/sync/linear.py::*` — operation: delete — scope-reason: LinearSyncService facade
- `src/gobby/sync/linear_task_ops.py::*` — operation: delete — scope-reason: import/push/pull
- `src/gobby/sync/linear_project_ops.py::*` — operation: delete — scope-reason: Linear project discovery
- `src/gobby/sync/linear_support.py::*` — operation: delete — scope-reason: MCP parse and title/ref helpers
- `src/gobby/integrations/linear.py::*` — operation: delete — scope-reason: LinearIntegration availability wrapper
- `src/gobby/integrations/linear_graphql.py::*` — operation: delete — scope-reason: GraphQL fallback
- `src/gobby/cli/linear.py::*` — operation: delete — scope-reason: entire `gobby linear` group
- `src/gobby/cli/__init__.py::cli`
- `src/gobby/cli/init.py::_maybe_run_linear_setup`
- `src/gobby/cli/init.py::init`
- `src/gobby/integrations/__init__.py::*` — scope-reason: drop LinearIntegration export
- `src/gobby/sync/__init__.py::*` — scope-reason: drop LinearSyncService export
- `src/gobby/servers/routes/projects.py::get_integrations_status`
- `src/gobby/servers/routes/projects.py::update_project`
- `src/gobby/servers/routes/projects.py::ProjectUpdate`
- `tests/sync/test_linear_sync.py::*` — operation: delete — scope-reason: Linear sync suite
- `tests/cli/test_linear_coverage.py::*` — operation: delete — scope-reason: Linear CLI suite
- `tests/external_integrations/test_linear.py::*` — operation: delete — scope-reason: LinearIntegration suite
- `tests/integrations/test_integrations_linear_graphql.py::*` — operation: delete — scope-reason: GraphQL client suite

**Research context:** `LinearSyncService` (`sync/linear.py`) mixes
`LinearTaskOpsMixin` + `LinearProjectOpsMixin`, calls Linear MCP
(`list_issues`, project ops) and GraphQL via `linear_graphql.py`. CLI
(`cli/linear.py`) exposes teams/status/link/setup/import/sync/sync-all/create.
`gobby init --linear-setup` calls `_maybe_run_linear_setup` →
`_run_linear_setup`. `ProjectUpdate` accepts `linear_team_id`,
`linear_project_id`, `linear_sync_enabled`; `update_project` persists them;
`get_integrations_status` instantiates `LinearSyncService` and reads
`ExternalIssueSyncStatusStore`. `create_linear_sync_handler` exists only for
tests; it is not registered on the live cron executor.

Delete the Linear modules and CLI group. Unregister `linear` from
`cli.add_command`. Remove `--linear-setup` / `--linear-team-id` /
`--linear-project-id` from `init`. Stop accepting Linear fields on
`ProjectUpdate` and remove the Linear section from
`get_integrations_status` (leave GitHub/triage until 4.1). Stop exporting
`LinearIntegration` / `LinearSyncService`.

Do not drop SQL columns here (7.1). Do not delete `linear.yaml`.

**Acceptance:**

- 2.1.1 - `gobby linear` is not a CLI command. file: `src/gobby/cli/__init__.py`. test: `tests/cli/test_linear_coverage.py` deleted; remaining CLI discovery tests do not list `linear`.
- 2.1.2 - `gobby init` has no Linear setup flags or `_maybe_run_linear_setup`. symbol: `init`.
- 2.1.3 - Project HTTP update rejects or omits `linear_team_id` / `linear_project_id` / `linear_sync_enabled`. symbol: `ProjectUpdate`.
- 2.1.4 - `gcode grep -w LinearSyncService src` is empty. behavior: "LinearSyncService gone from src".
- 2.1.5 - MCP template `src/gobby/install/shared/mcp/templates/linear.yaml` is unchanged and still installed. file: `src/gobby/install/shared/mcp/templates/linear.yaml`.

**Depends on:** 1.1

## P3: GitHub issue identity
`kind: framing`

**Goal:** Gobby no longer imports GitHub issues as tasks, links
`github_issue_number`, or closes linked issues after merge. GitHub PR
publication is 3.2.

Granularity: one GitHub-issue-identity product (CLI + MCP tools + sync + merge
step). `GitHubSyncService` mixes import/sync with `create_pr_for_task`; the
CLI group is deleted entirely, so the whole service goes. Delivery already
opens PRs in `_delivery.py`.

### 3.1 Delete GitHub issue import, sync, CLI, and close-linked merge step
`kind: deliverable`
[category: code]
[domain: backend]

Targets:
- `src/gobby/cli/github.py::*` — operation: delete — scope-reason: entire `gobby github` group
- `src/gobby/sync/github.py::*` — operation: delete — scope-reason: GitHubSyncService import/sync/PR-from-CLI
- `src/gobby/sync/github_issue_sync.py::*` — operation: delete — scope-reason: background GitHub issue reconciliation
- `src/gobby/sync/github_validation.py::*` — operation: delete — scope-reason: issue-number normalization for import
- `src/gobby/sync/task_github_import.py::*` — operation: delete — scope-reason: GitHubIssueImporter
- `src/gobby/mcp_proxy/tools/task_github.py::*` — operation: delete — scope-reason: import_github_issues and link_task_to_github_issue
- `src/gobby/cli/__init__.py::cli`
- `src/gobby/cli/tasks/main.py::import_github`
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::create_task_ops_registry`
- `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py::close_linked_github_issue`
- `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py::create_stage_ops_registry`
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`
- `src/gobby/install/shared/workflows/agents/merge-worker.yaml`
- `src/gobby/sync/__init__.py::*` — scope-reason: drop GitHubSyncService export
- `tests/sync/test_github_sync.py::*` — operation: delete — scope-reason: GitHubSyncService suite
- `tests/sync/test_github_issue_sync.py::*` — operation: delete — scope-reason: GitHub issue sync suite
- `tests/mcp_proxy/tools/test_task_github.py::*` — operation: delete — scope-reason: GitHub task MCP tools
- `tests/cli/test_github_coverage.py::*` — operation: delete — scope-reason: github CLI suite
- `tests/cli/test_tasks_cli.py::*` — scope-reason: remove import_github CLI tests only
- `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::*` — scope-reason: remove close_linked_github_issue tests
- `tests/agents/test_merge_lifecycle.py::*` — scope-reason: drop close_linked tool from merge allowlists
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: drop close_linked from orchestrator contract
- `tests/workflows/test_handler_route_lint.py::*` — scope-reason: drop close_linked handler route
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: drop import/link/close tool names from allowlists

**Research context:** `create_github_registry` registers
`import_github_issues` (via `gh` CLI) and `link_task_to_github_issue`;
`create_task_ops_registry` merges it. `close_linked_github_issue` in
`_stage_ops.py` calls `GitHubIssueTriageService.close_linked_issue_after_merge`
when `task.github_repo` and `task.github_issue_number` are set. Merge
orchestrator/worker YAML require that tool after `record_merge_result`.
`GitHubSyncService.create_pr_for_task` is only used by `gobby github pr`
and is deleted with that CLI; GitHub PR publication in `_delivery.py` is 3.2.
`cli/tasks/main.py::import_github` uses `GitHubIssueImporter`. Sweep string
sites for `import_github_issues`, `link_task_to_github_issue`,
`close_linked_github_issue`, `gobby github`.

Delete the GitHub CLI module and unregister it. Delete import/sync modules and
the task-ops GitHub registry. Remove `close_linked_github_issue` from stage
ops and from merge YAML steps/allowlists. Remove `import_github` from the
tasks CLI. Keep `github.yaml`.

**Acceptance:**

- 3.1.1 - `gobby github` is not a CLI command. file: `src/gobby/cli/__init__.py`.
- 3.1.2 - `gobby-tasks-ops` no longer registers `import_github_issues`, `link_task_to_github_issue`, or `close_linked_github_issue`. symbol: `create_task_ops_registry`. test: `tests/mcp_proxy/tools/test_task_github.py` deleted.
- 3.1.3 - Merge orchestrator/worker do not call `close_linked_github_issue`. file: `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`. test: `tests/agents/test_merge_orchestrator_contract.py`.
- 3.1.4 - MCP template `src/gobby/install/shared/mcp/templates/github.yaml` is unchanged. file: `src/gobby/install/shared/mcp/templates/github.yaml`.
- 3.1.5 - `gcode grep -w GitHubSyncService src` is empty. behavior: "GitHubSyncService gone from src".

**Depends on:** 1.1

Granularity for 3.2: one GitHub PR-client product (open/reuse PR, delivery
campaigns, `pull_request` mode, merge publication, Trace). Splitting would
leave `pull_request` build mode calling deleted tools.

### 3.2 Remove GitHub PR delivery and stored PR identity
`kind: deliverable`
[category: code]
[domain: fullstack]

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_delivery.py::*` — operation: delete — scope-reason: open_delivery_pr, record_pr_state, GitHub MCP/REST PR create
- `src/gobby/storage/delivery.py::*` — operation: delete — scope-reason: TaskDeliveryStateManager / PR campaigns
- `src/gobby/build/delivery.py::*` — scope-reason: remove pull_request campaign recording and github_repo resolution; delete the file if nothing remains
- `src/gobby/config/build.py::*` — scope-reason: remove DeliveryMode `pull_request`; drop the knob if only `auto` remains
- `src/gobby/build/options.py::*` — scope-reason: delivery_mode fields
- `src/gobby/cli/build.py::*` — scope-reason: --delivery-mode pull_request
- `src/gobby/cli/profiles.py::*` — scope-reason: profile delivery_mode
- `src/gobby/cli/_build_options.py::*` — scope-reason: delivery_mode plumbing
- `src/gobby/cli/_build_daemon.py::*` — scope-reason: delivery_mode payload
- `src/gobby/mcp_proxy/tools/build.py::*` — scope-reason: delivery_mode arg
- `src/gobby/mcp_proxy/tools/profiles.py::*` — scope-reason: delivery_mode arg
- `src/gobby/servers/routes/build.py::*` — scope-reason: delivery_mode on HTTP build
- `src/gobby/servers/routes/profiles.py::*` — scope-reason: delivery_mode on profiles
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::create_task_ops_registry`
- `src/gobby/mcp_proxy/tools/merge_github_protection.py::*` — scope-reason: keep git_output and git dry-run; delete GitHub token/API protection helpers
- `src/gobby/mcp_proxy/tools/merge_branch_protection_tool.py::probe_branch_protection`
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`
- `src/gobby/install/shared/workflows/agents/merge-worker.yaml`
- `src/gobby/install/shared/registry/build_profiles.yaml`
- `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py::*` — scope-reason: drop github_pr_number from merge-result recording if present
- `web/src/components/activity/taskdetail/TaskDetailTrace.tsx::TaskDetailTrace`
- `tests/mcp_proxy/tools/tasks/test_delivery_state_tools.py::*` — operation: delete — scope-reason: PR delivery tools
- `tests/mcp_proxy/tools/test_probe_branch_protection.py::*` — scope-reason: GitHub API cases go; git dry-run may remain
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: drop open_delivery_pr / record_pr_state / GitHub PR tools
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: drop open_delivery_pr / record_pr_state
- `tests/docs/test_cross_repo_submit_profile_docs.py::*` — scope-reason: open_delivery_pr citations
- `src/gobby/dispatch/AGENTS.md` — drop open_delivery_pr as the PR publication path

**Research context:** `create_delivery_registry` exposes `get_delivery_state`,
`record_pr_state`, and `open_delivery_pr`. The latter pushes a branch and
opens/reuses a GitHub PR via MCP (`list_pull_requests` /
`create_pull_request`) or REST (`_create_pull_request_rest` +
`_github_token`). `DeliveryMode = Literal["auto", "pull_request"]`;
`record_build_delivery_campaign` no-ops unless `pull_request`.
`task_delivery_campaigns` / `task_delivery_units` store `github_pr_number`
and `pr_url`. Merge orchestrator `pr` stage calls `open_delivery_pr`,
`record_pr_state`, and `github:get_pull_request_reviews`.
`probe_branch_protection` hits GitHub protection APIs then git dry-run;
`git_output` is shared by other merge tools and must stay. Task Trace
renders `https://github.com/{repo}/pull/{n}`. After this leaf Gobby does
not publish GitHub PRs. If `DeliveryMode` would have one value, remove the
option from CLI/MCP/HTTP/profiles rather than leave a dead enum.

**Acceptance:**

- 3.2.1 - `gobby-tasks-ops` does not register `open_delivery_pr` or `record_pr_state`. symbol: `create_task_ops_registry`. test: `tests/mcp_proxy/tools/tasks/test_delivery_state_tools.py` deleted.
- 3.2.2 - No Gobby code creates a GitHub pull request via MCP or REST. behavior: "no GitHub PR create in src".
- 3.2.3 - Merge orchestrator/worker do not call `open_delivery_pr`, `record_pr_state`, or `github:get_pull_request_reviews`. file: `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`. test: `tests/agents/test_merge_orchestrator_contract.py`.
- 3.2.4 - `probe_branch_protection` does not call GitHub HTTP APIs; `git_output` remains for merge tools. symbol: `probe_branch_protection`. file: `src/gobby/mcp_proxy/tools/merge_github_protection.py`.
- 3.2.5 - Build/profile surfaces have no `pull_request` delivery mode. file: `src/gobby/config/build.py`.
- 3.2.6 - Task Trace does not render a GitHub PR link. symbol: `TaskDetailTrace`. test: `web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx`.

**Depends on:** 1.1

## P4: GitHub issue triage
`kind: framing`

**Goal:** No webhook intake, LLM verdict, or task creation from GitHub issues.
Qdrant writes to `gobby_github_issues` stop; the managed embedding kind is
removed in 6.1.

### 4.1 Delete triage service, webhook, storage, and project config HTTP
`kind: deliverable`
[category: code]
[domain: backend]

Targets:
- `src/gobby/github_triage/service.py::*` — operation: delete — scope-reason: GitHubIssueTriageService
- `src/gobby/github_triage/delivery.py::*` — operation: delete — scope-reason: webhook delivery processor
- `src/gobby/github_triage/issue_index.py::*` — operation: delete — scope-reason: issue snapshot/hash/Qdrant ids
- `src/gobby/github_triage/mcp_call.py::*` — operation: delete — scope-reason: triage GitHub MCP calls
- `src/gobby/github_triage/task_description.py::*` — operation: delete — scope-reason: triage task text
- `src/gobby/github_triage/__init__.py::*` — operation: delete — scope-reason: package export
- `src/gobby/storage/github_triage.py::*` — operation: delete — scope-reason: GitHubTriageStore
- `src/gobby/storage/external_issue_sync.py::*` — operation: delete — scope-reason: last status-store reader removed with integrations/status
- `src/gobby/servers/routes/github_triage.py::*` — operation: delete — scope-reason: `/api/github/webhooks` triage router
- `src/gobby/servers/_app_routes.py::*` — scope-reason: stop including triage router
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: drop create_github_triage_router export
- `src/gobby/servers/routes/projects.py::get_github_triage_config`
- `src/gobby/servers/routes/projects.py::update_github_triage_config`
- `src/gobby/servers/routes/projects.py::GitHubTriageConfigUpdate`
- `src/gobby/servers/routes/projects.py::get_integrations_status`
- `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py::*` — scope-reason: confirm no remaining GitHubIssueTriageService import after 3.1
- `tests/github_triage/test_github_triage_service.py::*` — operation: delete — scope-reason: triage service suite
- `tests/github_triage/test_issue_index.py::*` — operation: delete — scope-reason: issue index suite
- `tests/docs/test_github_issue_triage_docs.py::*` — operation: delete — scope-reason: triage docs contract
- `tests/servers/routes/test_projects_routes.py::*` — scope-reason: remove integrations/status and triage config tests

**Research context:** Webhook router prefix `/api/github/webhooks` calls
`GitHubIssueTriageService.accept_webhook_delivery`. Project routes expose
`GET/PUT` triage config and `GET /integrations/status`. Tables:
`project_github_triage_configs`, `gh_issues_triaged`, `gh_triage_deliveries`,
`gh_triage_build_dispatches` (dropped in 7.1). After 1.1 the coordinator
recovery scan is already gone; this leaf removes the remaining writers.
`close_linked_github_issue` must already be gone (3.1) because it imported
the triage service.

Delete the package, store, webhook router, triage config endpoints, and the
now-empty `/integrations/status` endpoint. Update app route registration.

**Acceptance:**

- 4.1.1 - `POST /api/github/webhooks/triage/{project_id}` is not registered. file: `src/gobby/servers/_app_routes.py`.
- 4.1.2 - Project HTTP has no triage config or integrations/status routes. symbol: `create_projects_router`. test: `tests/servers/routes/test_projects_routes.py`.
- 4.1.3 - `gcode grep -w GitHubIssueTriageService src` is empty. behavior: "triage service gone from src".
- 4.1.4 - No Python writer upserts `gh_issues_triaged` or `gobby_github_issues`. behavior: "no triage/Qdrant issue writers in src".

**Depends on:** 1.1, 3.1

## P5: Dead GitHub-MCP source-control display
`kind: framing`

**Goal:** Source-control HTTP no longer calls GitHub MCP. Live UI/gclient keep
git-local status/branches/worktrees/clones.

### 5.1 Remove GitHubMCPHelper and GitHub issue/PR/CI routes
`kind: deliverable`
[category: code]
[domain: fullstack]

Targets:
- `src/gobby/integrations/github_helper.py::*` — operation: delete — scope-reason: GitHubMCPHelper and parse_github_mcp_result
- `src/gobby/integrations/github.py::*` — operation: delete — scope-reason: GitHubIntegration availability wrapper
- `src/gobby/integrations/mcp_result.py::*` — operation: delete — scope-reason: only consumers were helper and Linear support
- `src/gobby/servers/routes/source_control_github.py::*` — operation: delete — scope-reason: `_call_github_mcp` / `_get_github`
- `src/gobby/integrations/__init__.py::*` — scope-reason: drop GitHubIntegration export; package keeps rtk
- `src/gobby/servers/routes/source_control.py::create_source_control_router`
- `src/gobby/servers/routes/source_control.py::list_pull_requests`
- `src/gobby/servers/routes/source_control.py::get_pull_request`
- `src/gobby/servers/routes/source_control.py::get_pr_checks`
- `src/gobby/servers/routes/source_control.py::list_issues`
- `src/gobby/servers/routes/source_control.py::get_issue`
- `src/gobby/servers/routes/source_control.py::list_cicd_runs`
- `src/gobby/servers/routes/source_control.py::list_branch_commits`
- `web/src/hooks/useSourceControl.ts::*` — scope-reason: remove GitHub PR/issue/CI polling; keep git-local fetchers and WorktreeInfo
- `web/src/hooks/__tests__/useSourceControl.test.ts::*` — scope-reason: drop GitHub endpoint assertions
- `web/eslint.config.js` — drop stale `components/source-control/*` paths if still listed
- `tests/integrations/test_github_helper.py::*` — operation: delete — scope-reason: helper suite
- `tests/integrations/test_github_integration.py::*` — operation: delete — scope-reason: GitHubIntegration suite
- `tests/external_integrations/test_github.py::*` — operation: delete — scope-reason: GitHubIntegration availability suite
- `tests/mcp_proxy/services/test_scope_resolution_consumers.py::*` — scope-reason: remove GitHubIntegration/GitHubMCPHelper consumers
- `tests/sync/test_mcp_result.py::*` — operation: delete — scope-reason: mcp_result module deleted

**Research context:** `web/src/components/source-control/` is already gone.
`useSourceControl` still polls `/api/source-control/prs`, `issues`, `cicd`
(`GITHUB_POLL_MS = 30000`) but the only production import is
`import type { WorktreeInfo }` from BranchIndicator. Live fetches:
BranchIndicator → status/worktrees/branches/checkout; IsolationTargetSelector
→ worktrees/clones; gclient → status/worktrees. `list_branch_commits` tries
`GitHubMCPHelper.list_commits` then `git log`. After this leaf, commits if
kept are git-only. PR/issue/CI routes have no live UI; delete them.
`parse_github_repo` dies with the helper. GitHub repo parsing in
`gobby.build.delivery` is removed in 3.2 with `pull_request` delivery.

Remove GitHub MCP routes and the helper/integration modules. Keep
`get_status`, `list_branches`, `checkout_branch`, worktrees, clones. Strip
GitHub polling from `useSourceControl` (or delete unused GitHub state). Do
not break BranchIndicator's type import.

**Acceptance:**

- 5.1.1 - `/api/source-control/prs`, `issues`, and `cicd/runs` are not registered. symbol: `create_source_control_router`.
- 5.1.2 - `list_branch_commits` does not import `GitHubMCPHelper` or call GitHub MCP. symbol: `list_branch_commits`.
- 5.1.3 - `gcode grep -w GitHubMCPHelper src` and `gcode grep -w GitHubIntegration src` are empty. behavior: "GitHub MCP helper/integration gone from src".
- 5.1.4 - BranchIndicator, IsolationTargetSelector, and gclient still use git-local source-control HTTP. file: `web/src/components/chat/BranchIndicator.tsx`.
- 5.1.5 - `useSourceControl` does not fetch `/api/source-control/prs`, `issues`, or `cicd`. file: `web/src/hooks/useSourceControl.ts`. test: `web/src/hooks/__tests__/useSourceControl.test.ts`.

**Depends on:** 3.1, 3.2, 4.1

## P6: Embeddings unwind
`kind: framing`

**Goal:** Embedding switch, purge, and vectorstore manage memories and tool
embeddings only. Leftover `gobby_github_issues` Qdrant aliases/physicals are
dropped, not left as a third managed kind.

### 6.1 Remove gobby_github_issues as a managed embedding kind
`kind: deliverable`
[category: code]
[domain: backend]

Targets:
- `src/gobby/memory/collection_names.py::EMBEDDING_COLLECTION_KINDS`
- `src/gobby/memory/collection_names.py::CollectionNameResolver`
- `src/gobby/memory/vectorstore.py::*` — scope-reason: drop github_issue kind mapping
- `src/gobby/storage/embedding_generation_state.py::*` — scope-reason: drop github_issue collection map
- `src/gobby/ai/embedding_switch_runner.py::EmbeddingSwitchRunner`
- `src/gobby/ai/embedding_switch_runner.py::_build_github_issue_collection`
- `src/gobby/ai/embedding_switch_runner.py::_project_github_issue_change`
- `src/gobby/ai/embedding_switch_runner.py::_upsert_github_issue_record`
- `src/gobby/ai/embedding_switch_runner.py::MissingGitHubIssueSourceTextError`
- `src/gobby/projects/purge.py::*` — scope-reason: stop treating gobby_github_issues as a managed kind; delete leftover collections
- `tests/ai/test_embedding_switch.py::*` — scope-reason: two-kind contract
- `tests/ai/test_embedding_switch_runner.py::*` — scope-reason: remove github issue builder assertions
- `tests/ai/test_embedding_switch_daemon_lifecycle.py::*` — scope-reason: two-kind aliases
- `tests/projects/test_purge_components.py::*` — scope-reason: purge leftover github issue collections

**Research context:** `EMBEDDING_COLLECTION_KINDS` is
`("memories", "tool_embeddings", "gobby_github_issues")`. Switch runner
builds/flips/GCs all three; `_project_github_issue_change` and
`_build_github_issue_collection` upsert triage records. Project purge tests
assert `gobby_github_issues@staged` cleanup. After 4.1 there is no issue
source text; keeping the kind would fail switches or write empty collections.

Remove the kind from the tuple and every kind map. Delete GitHub issue
builder/projection methods. Switch GC or purge must delete existing
`gobby_github_issues` aliases and `gobby_github_issues@*` physicals so they
do not linger unowned. Do not add a config flag to keep an empty collection.

**Acceptance:**

- 6.1.1 - `EMBEDDING_COLLECTION_KINDS` is exactly `("memories", "tool_embeddings")`. symbol: `EMBEDDING_COLLECTION_KINDS`. test: `tests/ai/test_embedding_switch.py`.
- 6.1.2 - EmbeddingSwitchRunner has no GitHub issue builder or projection. symbol: `EmbeddingSwitchRunner`. test: `tests/ai/test_embedding_switch_runner.py`.
- 6.1.3 - Purge/switch GC deletes leftover `gobby_github_issues` aliases and physicals. file: `src/gobby/projects/purge.py`. test: `tests/projects/test_purge_components.py`.
- 6.1.4 - `gcode grep -F gobby_github_issues src` is empty except any one-shot deletion identifier. behavior: "github issues not a managed embedding kind".

**Depends on:** 4.1

## P7: Schema and models
`kind: framing`

**Goal:** Hub schema and Python/web models no longer carry Linear/GitHub
issue or PR identity.

Granularity: one destructive schema campaign plus its mandated carriers and
the Python/web field deletions that would break at apply time. Splitting
SQL from models leaves a daemon that SELECTs dropped columns.

### 7.1 Drop issue-bridge tables/columns and model fields
`kind: deliverable`
[category: code]
[domain: fullstack]

Targets:
- `crates/gcore/assets/schema/migrations/438_retire_linear_github_issue_bridge.sql`
- `crates/gcore/assets/schema/baseline.sql::*` — scope-reason: drop retired objects from canonical baseline
- `crates/gcore/assets/schema/catalog.manifest.json`
- `crates/gcore/assets/schema/seed.manifest.json`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: if the new migration must be listed
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: drop GRANTs for retired tables
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: schema contract
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: derived schema identity
- `src/gobby/storage/schema_expected_identity.json`
- `src/gobby/storage/projects.py::Project`
- `src/gobby/storage/projects.py::*` — scope-reason: remove linear_* and github_repo/github_url from row mapping, to_dict, insert/update
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: drop github_issue_number, github_pr_number, github_repo, linear_issue_id, linear_team_id
- `src/gobby/storage/tasks/_creation.py::*` — scope-reason: stop inserting dropped columns
- `src/gobby/storage/tasks/_updates.py::*` — scope-reason: stop updating dropped columns
- `src/gobby/storage/tasks/_manager.py::*` — scope-reason: drop create/update parameters
- `src/gobby/sync/tasks.py::*` — scope-reason: JSONL backup/restore omits dropped fields
- `src/gobby/cli/projects.py::*` — scope-reason: drop --github-repo/--github-url and Linear flags
- `src/gobby/servers/routes/projects.py::ProjectUpdate`
- `web/src/hooks/useProjects.ts::*` — scope-reason: drop linear_* and github_repo from Project type
- `web/src/types/tasks.ts::*` — scope-reason: drop github_issue_number, github_pr_number, github_repo, linear fields
- `web/src/hooks/__tests__/useProjects.test.tsx::*` — scope-reason: fixture fields
- `web/src/__tests__/App.test.tsx::*` — scope-reason: project fixture GitHub/Linear fields

**Research context:** Drop tables `external_issue_sync_status`,
`project_github_triage_configs`, `gh_issues_triaged`,
`gh_triage_deliveries`, `gh_triage_build_dispatches`,
`task_delivery_campaigns`, `task_delivery_units`. Drop columns
`projects.linear_team_id`, `linear_project_id`, `linear_synced_at`,
`linear_sync_enabled`, `projects.github_repo`, `projects.github_url`;
`tasks.linear_issue_id`, `tasks.linear_team_id`, `tasks.github_issue_number`,
`tasks.github_pr_number`, `tasks.github_repo`. Migration must be
`-- gobby:destructive`, schema-qualified, `IF EXISTS`, `RESTRICT`, matching
426. Seed rows for `_orphaned` / `_migrated` / `_global` / `_personal`
currently list github/linear columns. Trace PR UI is already gone in 3.2;
this leaf drops the remaining model fields. `gobby projects update` no
longer sets GitHub identity.

Apply after 2.1–6.1 so no SELECT/INSERT remains on dropped objects. Write
the destructive migration and identity carriers on the worktree branch.
Do not run `gobby schema apply --destructive` against the live hub from
this plan. Isolated tests use `gobby_test` only.

**Acceptance:**

- 7.1.1 - Destructive migration drops the listed tables and columns and no others. file: `crates/gcore/assets/schema/migrations/438_retire_linear_github_issue_bridge.sql`.
- 7.1.2 - Derived schema carriers match the new baseline (catalog, grants, schema_contract, gdaemon cli_contract, schema_expected_identity). file: `src/gobby/storage/schema_expected_identity.json`.
- 7.1.3 - Task create/update/JSONL have no GitHub or Linear identity fields. file: `src/gobby/storage/tasks/_models.py`.
- 7.1.4 - Project model, HTTP, and CLI have no `linear_*`, `github_repo`, or `github_url`. symbol: `Project`. file: `src/gobby/cli/projects.py`.

**Depends on:** 2.1, 3.1, 3.2, 4.1, 5.1, 6.1

## P8: Docs and skill references
`kind: framing`

**Goal:** Operator and agent docs describe MCP servers, not Gobby-owned
GitHub/Linear issue or PR identity.

### 8.1 Rewrite integration docs; delete triage guide
`kind: deliverable`
[category: docs]

Targets:
- `docs/guides/integrations.md`
- `docs/guides/github-issue-triage.md::*` — operation: delete — scope-reason: triage product removed
- `docs/guides/http-endpoints.md::*` — scope-reason: remove integrations/status, triage webhook, GitHub PR/issue/CI source-control routes
- `docs/guides/cli-commands.md::*` — scope-reason: remove gobby github / gobby linear / projects --github-repo
- `src/gobby/install/shared/skills/gobby/references/integrations/overview.md`
- `src/gobby/install/shared/skills/gobby/references/integrations/external.md`
- `src/gobby/install/shared/skills/gobby/references/integrations/webhooks.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/pr-delivery.md::*` — operation: delete — scope-reason: Gobby no longer opens GitHub PRs
- `src/gobby/install/shared/skills/gobby/references/tasks/backups.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate after shared skill edits

**Research context:** `integrations.md` currently documents GitHub/Linear
issue sync as the product. `external.md` tells agents to call
`import_github_issues`. `pr-delivery.md` tells agents to call
`open_delivery_pr`. `backups.md` lists those tools next to restore.
`overview.md` loads `external.md` for "GitHub/Linear setup, import,
synchronization". Webhooks reference inbound GitHub intake. After this leaf,
Linear/GitHub are MCP servers the agent may call directly; Gobby does not
mirror issues or open PRs.

Rewrite or delete those pages. Point remaining "integrations" capability at
MCP server setup + comms/webhooks. Regenerate the bundled content manifest.

**Acceptance:**

- 8.1.1 - `docs/guides/integrations.md` does not document issue import/sync/triage, PR delivery, or `gobby github` / `gobby linear`. file: `docs/guides/integrations.md`.
- 8.1.2 - `docs/guides/github-issue-triage.md` and `references/source-control/pr-delivery.md` are deleted. file: `docs/guides/github-issue-triage.md`.
- 8.1.3 - Skill references do not name `import_github_issues`, `link_task_to_github_issue`, `close_linked_github_issue`, or `open_delivery_pr`. file: `src/gobby/install/shared/skills/gobby/references/integrations/external.md`.
- 8.1.4 - Bundled content manifest is regenerated. file: `src/gobby/install/bundled_content_manifest.json`.

**Depends on:** 2.1, 3.1, 3.2, 4.1, 5.1

## Verification
`kind: verification`

Per-leaf targeted pytest (never full suite):

```bash
DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" \
  GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/sync tests/github_triage tests/integrations tests/external_integrations \
  tests/mcp_proxy/tools/test_task_github.py tests/mcp_proxy/tools/tasks \
  tests/mcp_proxy/tools/test_probe_branch_protection.py \
  tests/cli tests/servers/routes/test_projects_routes.py \
  tests/ai/test_embedding_switch.py tests/ai/test_embedding_switch_runner.py \
  tests/projects/test_purge_components.py tests/agents/test_merge_orchestrator_contract.py \
  tests/workflows/test_handler_route_lint.py tests/workflows/test_task_enforcement_rules.py \
  -q --tb=short
```

Frontend: `useSourceControl` and TaskDetailTrace tests (no PR link).

Literal sweep after 7.1:

```bash
gcode grep -w LinearSyncService src
gcode grep -w GitHubSyncService src
gcode grep -w GitHubIssueTriageService src
gcode grep -w GitHubMCPHelper src
gcode grep -F import_github_issues src
gcode grep -F open_delivery_pr src
gcode grep -F gobby_github_issues src
```

Keep: `linear.yaml`, `github.yaml`, local git merge, `git_output`.

Schema: `uv run gobby plans validate` is not a schema apply. Migration SQL
and identity files must match in-tree. Do not apply to the live hub.

## Implementation order
`kind: framing`

Create the `0.5.0` worktree first. Then 1.1 (stop loop) → 2.1, 3.1, and 3.2
in parallel → 4.1 → 5.1 and 6.1 → 7.1 → 8.1. Stop with the branch unmerged.
Docs may be drafted with 2.1–5.1 but land after those surfaces are gone so
they do not describe deleted tools.
