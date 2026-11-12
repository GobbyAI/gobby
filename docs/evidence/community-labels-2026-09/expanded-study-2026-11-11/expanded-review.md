# Expanded community-name review

This is a new, representative 20-item review. Your previous 45 ratings are preserved.
The candidates cover groups of 3–4, 5–19, 20–49, 50–99 and 100+ files.
Names are navigation aids: judge whether they help you find the code shown.
No community number or directory name is the proposed label.

For each item, fill the matching row in `expanded-review.csv`:

- **Accuracy:** 2 = describes the main responsibility; 1 = partly right or overly broad; 0 = wrong or misleading.
- **Findability:** 2 = you would confidently look here for the responsibility named; 1 = some help; 0 = no help or sends you to the wrong code.
- **Keep this name?** yes/no: would you keep this exact name for the files currently shown? This direct answer supplies carry truth without converting a partial score into a yes.
- Use `skip` when the context is insufficient; a skip never counts as an approval. A short comment or suggested name is welcome.

Some names intentionally accompany different files. Judge only the displayed files; you do not need to infer their history.
This worksheet does not change the original 85% acceptance bar or enable a live decision check.

## R01 — Agent dispatch, resume and runtime cleanup

**Proposed name:** Agent dispatch, resume and runtime cleanup

**Files in this case:** 45. Representative examples:

- `src/gobby/storage/tasks/_dispatch_mutex.py`
- `src/gobby/storage/daemon_resume_keys.py`
- `src/gobby/agents/runtime_cleanup.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py`
- `src/gobby/storage/agent_resume.py`
- `src/gobby/agents/resume_finalization.py`
- `src/gobby/agents/sandbox_reaper.py`
- `src/gobby/agents/srt_process_cleanup.py`

**Concrete code entry points:**

```text
src/gobby/storage/tasks/_dispatch_mutex.py:18-25 [class] MutexRenewal sig=class MutexRenewal(StrEnum):
src/gobby/storage/tasks/_dispatch_mutex.py:28-47 [class] DispatchMutex sig=class DispatchMutex:
  src/gobby/storage/tasks/_dispatch_mutex.py:38-47 [method] DispatchMutex.from_row sig=def from_row(cls, row: Mapping[str, Any]) -> DispatchMutex:
src/gobby/storage/tasks/_dispatch_mutex.py:50-56 [function] _coerce_timestamp sig=def _coerce_timestamp(value: datetime | str | None) -> datetime:
src/gobby/storage/tasks/_dispatch_mutex.py:59-293 [class] TaskDispatchMutexManager sig=class TaskDispatchMutexManager:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R02 — Git worktree operations and merge resolution

**Proposed name:** Git worktree operations and merge resolution

**Files in this case:** 102. Representative examples:

- `src/gobby/clones/git.py`
- `src/gobby/worktrees/git/__init__.py`
- `src/gobby/storage/merge_resolutions.py`
- `src/gobby/mcp_proxy/tools/worktrees/__init__.py`
- `src/gobby/mcp_proxy/services/argument_validation.py`
- `src/gobby/agents/frozen_workspace.py`
- `src/gobby/cli/merge.py`
- `src/gobby/clones/__init__.py`

**Concrete code entry points:**

```text
src/gobby/clones/git.py:27-29 [function] project_clone_directory sig=def project_clone_directory(project_name: str) -> Path:
src/gobby/clones/git.py:32-40 [class] CloneStatus sig=class CloneStatus:
src/gobby/clones/git.py:43-50 [class] GitOperationResult sig=class GitOperationResult:
src/gobby/clones/git.py:53-812 [class] CloneGitManager sig=class CloneGitManager:
  src/gobby/clones/git.py:62-74 [method] CloneGitManager.__init__ sig=def __init__(self, repo_path: str | Path) -> None:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R03 — Git worktree operations and merge resolution

**Proposed name:** Git worktree operations and merge resolution

**Files in this case:** 102. Representative examples:

- `web/src/components/ui/Button.tsx`
- `web/src/components/ui/controlStyles.ts`
- `web/src/components/ui/Input.tsx`
- `web/src/components/ui/FormField.tsx`
- `gobby-annotate/packages/extension/src/CapturePanel.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts`
- `web/src/components/ProjectSelector.tsx`
- `web/src/components/ValidationDetectionEditor.tsx`

**Concrete code entry points:**

```text
web/src/components/ui/Button.tsx:55-62 [function] handleClick sig=const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R04 — Development proxy logging and Vite configuration

**Proposed name:** Development proxy logging and Vite configuration

**Files in this case:** 3. Representative examples:

- `web/src/lib/devProxyLogger.ts`
- `web/src/lib/__tests__/devProxyLogger.test.ts`
- `web/vite.config.ts`

**Concrete code entry points:**

```text
web/src/lib/devProxyLogger.ts:5-27 [function] proxyAwareErrorLogger sig=function proxyAwareErrorLogger(
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R05 — Metric snapshots and SQL dialect handling

**Proposed name:** Metric snapshots and SQL dialect handling

**Files in this case:** 5. Representative examples:

- `gobby-annotate/packages/extension/src/CapturePanel.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts`
- `web/src/components/ProjectSelector.tsx`
- `web/src/components/ValidationDetectionEditor.tsx`
- `web/src/components/__tests__/toolCallStatus.test.ts`

**Concrete code entry points:**

```text
gobby-annotate/packages/extension/src/CapturePanel.tsx:5-21 [function] SaveStatus sig=function SaveStatus({ status }: { status: string }) {
gobby-annotate/packages/extension/src/CapturePanel.tsx:22-95 [function] CapturePanel sig=function CapturePanel({
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R06 — Design concept and composition selection

**Proposed name:** Design concept and composition selection

**Files in this case:** 3. Representative examples:

- `src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs`
- `src/gobby/install/shared/skills/impeccable/scripts/lib/composition-catalog.mjs`
- `src/gobby/install/shared/skills/impeccable/scripts/concept-seed.mjs`

**Concrete code entry points:**

```text
src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs:44-51 [function] normalizeConceptForm sig=function normalizeConceptForm(value) {
src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs:53-145 [function] validateConceptEntry sig=function validateConceptEntry(concept, { existingForms = new Map(), axes = null } = {}) {
src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs:150-160 [function] conceptContentHash sig=function conceptContentHash(concept) {
src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs:162-185 [function] readConceptCatalog sig=function readConceptCatalog(catalogPath, reviewsPath) {
src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs:187-374 [function] validateConceptCatalog sig=function validateConceptCatalog(catalog, reviewData, {
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R07 — Metric snapshots and SQL dialect handling

**Proposed name:** Metric snapshots and SQL dialect handling

**Files in this case:** 4. Representative examples:

- `src/gobby/storage/sql_dialect.py`
- `src/gobby/storage/metric_snapshots.py`
- `src/gobby/servers/routes/metrics.py`
- `tests/storage/test_metric_snapshots.py`

**Concrete code entry points:**

```text
src/gobby/storage/sql_dialect.py:16-21 [function] dialect_of sig=def dialect_of(db: object) -> StorageDialect:
src/gobby/storage/sql_dialect.py:24-25 [function] is_postgres sig=def is_postgres(db: object) -> bool:
src/gobby/storage/sql_dialect.py:28-41 [function] table_column_names sig=def table_column_names(db: Any, table_name: str) -> set[str]:
src/gobby/storage/sql_dialect.py:44-52 [function] json_text_expr sig=def json_text_expr(db: object, column: str, *path: str) -> str:
src/gobby/storage/sql_dialect.py:55-59 [function] json_array_contains_condition sig=def json_array_contains_condition(
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R08 — Local paths, durable files and hub backups

**Proposed name:** Local paths, durable files and hub backups

**Files in this case:** 64. Representative examples:

- `gobby-annotate/packages/extension/src/CapturePanel.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts`
- `web/src/components/ProjectSelector.tsx`
- `web/src/components/ValidationDetectionEditor.tsx`
- `web/src/components/__tests__/toolCallStatus.test.ts`
- `web/src/components/activity/ActivityFilterDropdown.tsx`
- `web/src/components/activity/FilesTab.types.ts`
- `web/src/components/activity/FilesTabActionSurfaces.tsx`

**Concrete code entry points:**

```text
gobby-annotate/packages/extension/src/CapturePanel.tsx:5-21 [function] SaveStatus sig=function SaveStatus({ status }: { status: string }) {
gobby-annotate/packages/extension/src/CapturePanel.tsx:22-95 [function] CapturePanel sig=function CapturePanel({
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R09 — Communication identities, storage and chat transport

**Proposed name:** Communication identities, storage and chat transport

**Files in this case:** 25. Representative examples:

- `src/gobby/config/communications.py`
- `src/gobby/storage/communications.py`
- `src/gobby/communications/identities.py`
- `src/gobby/communications/responder.py`
- `src/gobby/communications/chat_backend.py`
- `src/gobby/communications/chat_transport.py`
- `src/gobby/communications/commands.py`
- `src/gobby/communications/group_policy.py`

**Concrete code entry points:**

```text
src/gobby/config/communications.py:8-15 [class] ChannelDefaults sig=class ChannelDefaults(BaseModel):
src/gobby/config/communications.py:18-32 [class] CommunicationsConfig sig=class CommunicationsConfig(BaseModel):
  src/gobby/config/communications.py:27-32 [method] CommunicationsConfig.validate_webhook_base_url sig=def validate_webhook_base_url(cls, value: str) -> str:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R10 — Agent dispatch, resume and runtime cleanup

**Proposed name:** Agent dispatch, resume and runtime cleanup

**Files in this case:** 45. Representative examples:

- `gobby-annotate/packages/extension/src/CapturePanel.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts`
- `web/src/components/ProjectSelector.tsx`
- `web/src/components/ValidationDetectionEditor.tsx`
- `web/src/components/__tests__/toolCallStatus.test.ts`
- `web/src/components/activity/ActivityFilterDropdown.tsx`
- `web/src/components/activity/FilesTab.types.ts`
- `web/src/components/activity/FilesTabActionSurfaces.tsx`

**Concrete code entry points:**

```text
gobby-annotate/packages/extension/src/CapturePanel.tsx:5-21 [function] SaveStatus sig=function SaveStatus({ status }: { status: string }) {
gobby-annotate/packages/extension/src/CapturePanel.tsx:22-95 [function] CapturePanel sig=function CapturePanel({
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R11 — Plan parsing, review evidence and task expansion

**Proposed name:** Plan parsing, review evidence and task expansion

**Files in this case:** 99. Representative examples:

- `src/gobby/plans/parser.py`
- `src/gobby/plans/review_evidence_models.py`
- `src/gobby/storage/plans.py`
- `src/gobby/tasks/expansion/_common.py`
- `src/gobby/plans/review_evidence_io.py`
- `src/gobby/cli/_plan_daemon_tasks.py`
- `src/gobby/cli/_plan_validation_output.py`
- `src/gobby/cli/plan.py`

**Concrete code entry points:**

```text
src/gobby/plans/parser.py:48-53 [function] resolve_plan_id sig=def resolve_plan_id(plan_id: str | None) -> str:
src/gobby/plans/parser.py:56-61 [function] extract_section_dependencies sig=def extract_section_dependencies(title: str) -> tuple[str, ...]:
src/gobby/plans/parser.py:64-66 [function] strip_section_dependencies sig=def strip_section_dependencies(title: str) -> str:
src/gobby/plans/parser.py:69-74 [class] Kind sig=class Kind(StrEnum):
src/gobby/plans/parser.py:77-81 [class] ArtifactKind sig=class ArtifactKind(StrEnum):
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R12 — Agent dispatch, resume and runtime cleanup

**Proposed name:** Agent dispatch, resume and runtime cleanup

**Files in this case:** 34. Representative examples:

- `src/gobby/storage/tasks/_dispatch_mutex.py`
- `src/gobby/storage/daemon_resume_keys.py`
- `src/gobby/agents/runtime_cleanup.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py`
- `src/gobby/storage/agent_resume.py`
- `src/gobby/agents/resume_finalization.py`
- `src/gobby/agents/sandbox_reaper.py`
- `src/gobby/agents/srt_process_cleanup.py`

**Concrete code entry points:**

```text
src/gobby/storage/tasks/_dispatch_mutex.py:18-25 [class] MutexRenewal sig=class MutexRenewal(StrEnum):
src/gobby/storage/tasks/_dispatch_mutex.py:28-47 [class] DispatchMutex sig=class DispatchMutex:
  src/gobby/storage/tasks/_dispatch_mutex.py:38-47 [method] DispatchMutex.from_row sig=def from_row(cls, row: Mapping[str, Any]) -> DispatchMutex:
src/gobby/storage/tasks/_dispatch_mutex.py:50-56 [function] _coerce_timestamp sig=def _coerce_timestamp(value: datetime | str | None) -> datetime:
src/gobby/storage/tasks/_dispatch_mutex.py:59-293 [class] TaskDispatchMutexManager sig=class TaskDispatchMutexManager:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R13 — Local paths, durable files and hub backups

**Proposed name:** Local paths, durable files and hub backups

**Files in this case:** 64. Representative examples:

- `src/gobby/paths.py`
- `src/gobby/utils/durable_file.py`
- `src/gobby/cli/installers/docker_guard.py`
- `src/gobby/cli/hub_backup/__init__.py`
- `src/gobby/cli/hub_backup/_manifest.py`
- `src/gobby/agents/grok_sandbox_home.py`
- `src/gobby/ai/decisions_shadow.py`
- `src/gobby/cli/files.py`

**Concrete code entry points:**

```text
src/gobby/paths.py:45-46 [class] FilesHomeError sig=class FilesHomeError(Exception):
src/gobby/paths.py:49-50 [class] FilesHomeNotOnThisDaemonError sig=class FilesHomeNotOnThisDaemonError(FilesHomeError):
src/gobby/paths.py:53-54 [class] FilesHomeUnsupportedPlatformError sig=class FilesHomeUnsupportedPlatformError(FilesHomeError):
src/gobby/paths.py:57-65 [function] get_package_root sig=def get_package_root() -> Path:
src/gobby/paths.py:68-95 [function] get_install_dir sig=def get_install_dir() -> Path:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R14 — Design concept and composition selection

**Proposed name:** Design concept and composition selection

**Files in this case:** 4. Representative examples:

- `src/gobby/install/shared/skills/impeccable/scripts/lib/roll-selection.mjs`
- `src/gobby/install/shared/skills/impeccable/scripts/lib/concept-catalog.mjs`
- `src/gobby/install/shared/skills/impeccable/scripts/lib/composition-catalog.mjs`
- `src/gobby/install/shared/skills/impeccable/scripts/concept-seed.mjs`

**Concrete code entry points:**

```text
src/gobby/install/shared/skills/impeccable/scripts/lib/roll-selection.mjs:56-58 [function] isGrain sig=function isGrain(value) {
src/gobby/install/shared/skills/impeccable/scripts/lib/roll-selection.mjs:60-62 [function] isPlatform sig=function isPlatform(value) {
src/gobby/install/shared/skills/impeccable/scripts/lib/roll-selection.mjs:70-74 [function] runSyncSelection sig=function runSyncSelection(generator, hash) {
src/gobby/install/shared/skills/impeccable/scripts/lib/roll-selection.mjs:81-85 [function] runAsyncSelection sig=async function runAsyncSelection(generator, hash) {
src/gobby/install/shared/skills/impeccable/scripts/lib/roll-selection.mjs:106-114 [function] challengerTickets sig=function challengerTickets(pool) {
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R15 — Git worktree operations and merge resolution

**Proposed name:** Git worktree operations and merge resolution

**Files in this case:** 77. Representative examples:

- `src/gobby/clones/git.py`
- `src/gobby/worktrees/git/__init__.py`
- `src/gobby/storage/merge_resolutions.py`
- `src/gobby/mcp_proxy/tools/worktrees/__init__.py`
- `src/gobby/mcp_proxy/services/argument_validation.py`
- `src/gobby/agents/frozen_workspace.py`
- `src/gobby/cli/merge.py`
- `src/gobby/clones/__init__.py`

**Concrete code entry points:**

```text
src/gobby/clones/git.py:27-29 [function] project_clone_directory sig=def project_clone_directory(project_name: str) -> Path:
src/gobby/clones/git.py:32-40 [class] CloneStatus sig=class CloneStatus:
src/gobby/clones/git.py:43-50 [class] GitOperationResult sig=class GitOperationResult:
src/gobby/clones/git.py:53-812 [class] CloneGitManager sig=class CloneGitManager:
  src/gobby/clones/git.py:62-74 [method] CloneGitManager.__init__ sig=def __init__(self, repo_path: str | Path) -> None:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R16 — Metric snapshots and SQL dialect handling

**Proposed name:** Metric snapshots and SQL dialect handling

**Files in this case:** 5. Representative examples:

- `src/gobby/storage/sql_dialect.py`
- `src/gobby/storage/metric_snapshots.py`
- `src/gobby/servers/routes/metrics.py`
- `tests/storage/test_metric_snapshots.py`
- `tests/storage/test_sql_dialect.py`

**Concrete code entry points:**

```text
src/gobby/storage/sql_dialect.py:16-21 [function] dialect_of sig=def dialect_of(db: object) -> StorageDialect:
src/gobby/storage/sql_dialect.py:24-25 [function] is_postgres sig=def is_postgres(db: object) -> bool:
src/gobby/storage/sql_dialect.py:28-41 [function] table_column_names sig=def table_column_names(db: Any, table_name: str) -> set[str]:
src/gobby/storage/sql_dialect.py:44-52 [function] json_text_expr sig=def json_text_expr(db: object, column: str, *path: str) -> str:
src/gobby/storage/sql_dialect.py:55-59 [function] json_array_contains_condition sig=def json_array_contains_condition(
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R17 — Project checkout registration and machine scope

**Proposed name:** Project checkout registration and machine scope

**Files in this case:** 100. Representative examples:

- `src/gobby/storage/projects.py`
- `tests/fixtures/isolated_checkout.py`
- `src/gobby/utils/project_context.py`
- `src/gobby/storage/project_checkouts.py`
- `src/gobby/storage/workspace_machine_scope.py`
- `src/gobby/agents/source_projection.py`
- `src/gobby/cli/init.py`
- `src/gobby/cli/projects.py`

**Concrete code entry points:**

```text
src/gobby/storage/projects.py:35-36 [class] IsolatedAgentProjectPathError sig=class IsolatedAgentProjectPathError(ValueError):
src/gobby/storage/projects.py:39-40 [class] NameAttachRejectedError sig=class NameAttachRejectedError(ValueError):
src/gobby/storage/projects.py:43-44 [class] AmbiguousProjectRefError sig=class AmbiguousProjectRefError(ValueError):
src/gobby/storage/projects.py:47-52 [function] personal_project_path sig=def personal_project_path(gobby_home: Path | None = None) -> Path:
src/gobby/storage/projects.py:55-113 [function] ensure_personal_project_identity sig=def ensure_personal_project_identity(
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R18 — Code index status, errors and embedding diagnostics

**Proposed name:** Code index status, errors and embedding diagnostics

**Files in this case:** 19. Representative examples:

- `crates/gcode/src/cli_error.rs`
- `crates/gcode/src/utils.rs`
- `crates/gcode/src/commands/status/current.rs`
- `crates/gcode/src/commands/status/shared.rs`
- `crates/gcode/src/commands/status.rs`
- `crates/gcode/src/commands/embeddings_doctor.rs`
- `crates/gcode/src/commands/evidence.rs`
- `crates/gcode/src/commands/status/invalidate.rs`

**Concrete code entry points:**

```text
crates/gcode/src/cli_error.rs:11-17 [class] CliError sig=pub struct CliError {
  crates/gcode/src/cli_error.rs:20-27 [method] CliError::grant sig=pub fn grant(error: GrantError) -> Self {
  crates/gcode/src/cli_error.rs:29-36 [method] CliError::project_required sig=pub fn project_required() -> Self {
  crates/gcode/src/cli_error.rs:46-62 [method] CliError::checkout_required sig=pub fn checkout_required(
  crates/gcode/src/cli_error.rs:67-86 [method] CliError::parent_index_required sig=pub fn parent_index_required(
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R19 — Local paths, durable files and hub backups

**Proposed name:** Local paths, durable files and hub backups

**Files in this case:** 48. Representative examples:

- `src/gobby/paths.py`
- `src/gobby/utils/durable_file.py`
- `src/gobby/cli/installers/docker_guard.py`
- `src/gobby/cli/hub_backup/__init__.py`
- `src/gobby/cli/hub_backup/_manifest.py`
- `src/gobby/agents/grok_sandbox_home.py`
- `src/gobby/ai/decisions_shadow.py`
- `src/gobby/cli/files.py`

**Concrete code entry points:**

```text
src/gobby/paths.py:45-46 [class] FilesHomeError sig=class FilesHomeError(Exception):
src/gobby/paths.py:49-50 [class] FilesHomeNotOnThisDaemonError sig=class FilesHomeNotOnThisDaemonError(FilesHomeError):
src/gobby/paths.py:53-54 [class] FilesHomeUnsupportedPlatformError sig=class FilesHomeUnsupportedPlatformError(FilesHomeError):
src/gobby/paths.py:57-65 [function] get_package_root sig=def get_package_root() -> Path:
src/gobby/paths.py:68-95 [function] get_install_dir sig=def get_install_dir() -> Path:
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___

## R20 — Design concept and composition selection

**Proposed name:** Design concept and composition selection

**Files in this case:** 4. Representative examples:

- `gobby-annotate/packages/extension/src/CapturePanel.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts`
- `web/src/components/ProjectSelector.tsx`
- `web/src/components/ValidationDetectionEditor.tsx`

**Concrete code entry points:**

```text
gobby-annotate/packages/extension/src/CapturePanel.tsx:5-21 [function] SaveStatus sig=function SaveStatus({ status }: { status: string }) {
gobby-annotate/packages/extension/src/CapturePanel.tsx:22-95 [function] CapturePanel sig=function CapturePanel({
```

Accuracy: ___ / Findability: ___ / Keep this name? ___ / Comment: ___
