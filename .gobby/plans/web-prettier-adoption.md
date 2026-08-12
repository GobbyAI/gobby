# Adopt Prettier for the Web UI

**Plan ID:** web-prettier-adoption

## Overview
`kind: framing`

Make Prettier the deterministic formatter for Gobby's web workspace, including
Tailwind class ordering, a one-time full baseline, and identical enforcement in
CI and both pre-push suites. Remove confirmed dead page surfaces and decompose
the three production components that Prettier defaults would otherwise push to
or above the repository's 1,000-line ceiling before applying the baseline.

## Constraints
`kind: framing`

- Finish and commit the current semantic configuration work before starting
  this plan; implementation begins from a clean worktree.
- Exact-pin `prettier@3.9.6` and `prettier-plugin-tailwindcss@0.8.1`.
- Use Prettier core defaults. Configure the Tailwind 4 stylesheet at
  `./src/styles/index.css`, sort `className` plus `cn`, `clsx`, `cva`, and
  `twMerge` strings, and preserve duplicate classes during this migration.
- Format hand-maintained TS, TSX, JS, CSS, HTML, and package/config JSON in the
  web workspace. Exclude dependency locks, generated and transcript fixtures,
  copied assets, dependencies, coverage, and build/test outputs.
- Preserve the generated fixture's `.gitattributes` classification and
  `.prettierignore` exclusion.
- Use the `decompose-monolith` direct-extraction workflow. Every touched
  hand-maintained production source must finish below 1,000 physical lines;
  exactly 1,000 is a violation.
- Keep dead-code removal, each structural extraction, formatter tooling, the
  mechanical baseline, and enforcement reviewable as dependent changes. The
  baseline contains formatting-only changes.
- Keep historical plans and review records unchanged. Update current source,
  active validation manifests, and current contributor guidance.
- Do not run the repository-wide Python pytest suite.

## P1: Remove Dead Page Surfaces
`kind: framing`

**Goal**: Delete page components with zero production mounts and remove their
now-unused satellites before formatting the surviving UI.

### 1.1 Delete FilesPage and AgentPortfolioPage with their dead satellites [category: refactor]
`kind: deliverable`

Targets:
- `web/src/components/FilesPage.tsx::*` — scope-reason: delete the entire zero-mount legacy file surface
- `web/src/components/agents/AgentPortfolioPage.tsx::*` — scope-reason: delete the entire zero-mount legacy agent portfolio
- `web/src/components/agents/categoryColors.ts::*` — scope-reason: delete the palette module whose sole production consumer is AgentPortfolioPage
- `web/src/components/__tests__/FilesPage.focusRing.test.tsx::*` — scope-reason: delete tests for the removed FilesPage surface
- `web/src/components/__tests__/FilesPage.test.tsx::*` — scope-reason: delete tests for the removed FilesPage surface
- `web/src/components/__tests__/FilesPage.truncated.test.tsx::*` — scope-reason: delete tests for the removed FilesPage surface
- `web/src/components/agents/__tests__/AgentPortfolioPage.test.tsx::*` — scope-reason: delete tests for the removed AgentPortfolioPage surface
- `web/src/components/chat/__tests__/ToolCallCard.interactive.test.tsx::*` — scope-reason: remove FilesPage from the active Markdown host census
- `web/src/__tests__/styleRatchet.test.ts::*` — scope-reason: remove the dead agent surface from the active styling census
- `web/src/__tests__/cssTokenIntegrity.test.ts::*` — scope-reason: remove the retired category token prefix from dynamic-token validation
- `web/src/components/shared/codeBlockTheme.ts::*` — scope-reason: remove the stale FilesPage reference from the file-viewer theme contract
- `web/src/styles/tokens.css`
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: remove both zero-mount surfaces from the active visual coverage manifest
- `docs/guides/frontend-style-guide.md`

Delete `FilesPage`, `AgentPortfolioPage`, and their dedicated tests. Delete
`categoryColors.ts` and both theme variants of the now-unreferenced
`--category-*` token family. Tighten the CSS-token and style-ratchet censuses,
remove both surfaces from the visual coverage manifest, remove `FilesPage` from
the Markdown-host list, update the file-viewer comment, and replace the stale
style-guide naming example with a live component.

Keep `FilesTab` and `AgentsTab` as the sole live file and agent surfaces. Use a
production-import search, rather than test presence, as the deletion proof.

**Acceptance:**

- 1.1.1 - No production source imports or renders either removed page, and the live activity tabs remain mounted. behavior: `FilesTab and AgentsTab production-mount contract`.
- 1.1.2 - Removed page sources, their dedicated tests, and the dead category-color module no longer exist. file: `web/src/components/FilesPage.tsx`.
- 1.1.3 - Both `--category-*` theme blocks and their dynamic-token exemption are removed without weakening validation for other token families. test: `web/src/__tests__/cssTokenIntegrity.test.ts`.
- 1.1.4 - Active style and visual manifests contain no removed-page entries or stale source paths. test: `web/tests/style-surfaces.spec.ts`.

## P2: Decompose Formatter-Ceiling Components
`kind: framing`

**Goal**: Preserve behavior while extracting cohesive rendering responsibilities
from the three files projected to exceed the production-source ceiling under
the selected formatter.

### 2.1 Decompose AgentEditForm by panel, read-only, and provider responsibilities [category: refactor] (depends: P1)
`kind: deliverable`

Targets:
- `web/src/components/agents/AgentEditForm.tsx::*` — scope-reason: reduce the form to stateful orchestration and surviving edit sections
- `web/src/components/agents/AgentEditForm.types.ts`
- `web/src/components/agents/AgentEditPanel.tsx`
- `web/src/components/agents/AgentReadOnlyDetails.tsx`
- `web/src/components/agents/AgentProviderSettings.tsx`
- `web/src/components/activity/agents/AgentsTabData.ts::*` — scope-reason: move AgentFormData type authority to the dedicated types module
- `web/src/components/agents/__tests__/AgentEditForm.test.tsx::*` — scope-reason: preserve the complete public form behavior through direct extraction
- `web/src/components/agents/__tests__/AgentEditors.test.tsx::*` — scope-reason: update the AgentFormData type import and preserve editor integration coverage

Use direct extraction; all consumers migrate atomically. Move shared form,
read-only item, selector, and prop types to `AgentEditForm.types.ts`, and update
consumers to import the type authority directly without a compatibility
re-export. Move dialog/focus/layout ownership to `AgentEditPanel.tsx`. Move the
installed-definition presentation to pure `AgentReadOnlyDetails.tsx`. Move
provider, model, reasoning, branch, surfaces, and their local custom-input state
to `AgentProviderSettings.tsx`.

Leave `AgentEditForm` as the explicit owner of the complete form value and
save/cancel/YAML orchestration. Pass narrow values and operations into extracted
renderers; avoid generic utility modules, circular imports, or forwarding
facades. Establish a green `AgentEditForm`/`AgentEditors` test baseline before
the first extraction and rerun it after each slice.

**Acceptance:**

- 2.1.1 - Existing create, edit, read-only, YAML, provider/model/reasoning, branch, surface, rules, skills, variables, restrictions, and steps behavior remains green. test: `web/src/components/agents/__tests__/AgentEditForm.test.tsx`.
- 2.1.2 - `AgentFormData` has one type owner and all direct consumers use it without compatibility exports. file: `web/src/components/agents/AgentEditForm.types.ts`.
- 2.1.3 - Each extracted module has one named responsibility, dependencies are acyclic, and shared mutable state has one explicit owner. behavior: `AgentEditForm direct-extraction module graph`.
- 2.1.4 - Every touched production TS/TSX file is below 1,000 physical lines after Prettier defaults are applied. behavior: `production source line ceiling`.

### 2.2 Decompose ToolCallCard content and interaction renderers [category: refactor] (depends: P1)
`kind: deliverable`

Targets:
- `web/src/components/chat/ToolCallCard.tsx::*` — scope-reason: retain public grouping orchestration while extracting two cohesive renderer families
- `web/src/components/chat/ToolCallCardContent.tsx`
- `web/src/components/chat/ToolCallCardInteractions.tsx`
- `web/src/components/chat/MessageItem.tsx::*` — scope-reason: verify the sole production consumer retains the stable ToolCallCards import surface
- `web/src/components/chat/__tests__/ToolCallCard.interactive.test.tsx::*` — scope-reason: preserve approval and AskUserQuestion interaction behavior across extraction
- `web/src/components/chat/__tests__/ToolCallCard.render.test.tsx::*` — scope-reason: preserve every result and header rendering branch across extraction
- `web/src/components/chat/__tests__/ToolCallCard.test.ts::*` — scope-reason: preserve generic card rendering and grouping behavior

Use direct extraction while keeping `ToolCallCards` at its existing public
module path. Move argument, location, error, and result presentation into
`ToolCallCardContent.tsx`. Move approval and `AskUserQuestion` state,
normalization, and rendering into `ToolCallCardInteractions.tsx`; it may depend
on the content renderer for argument presentation. Keep single-card/group
headers, status icons, grouping, expansion orchestration, and the public memoized
entry point in `ToolCallCard.tsx`.

Reuse the existing helper, style, and result-block modules. Preserve callback
return semantics, pending/completed transitions, malformed-result handling,
keyboard behavior, hidden-tool filtering, and grouped rendering. The dependency
direction is main coordinator to interactions/content, with interactions allowed
to depend on content and no reverse edges.

**Acceptance:**

- 2.2.1 - Approval decisions, answered-question normalization, keyboard expansion, and live response callbacks retain current behavior. test: `web/src/components/chat/__tests__/ToolCallCard.interactive.test.tsx`.
- 2.2.2 - Bash, MCP, image, protocol, ACP, malformed, null, raw-output, grouped, and generic result cases retain current rendering. test: `web/src/components/chat/__tests__/ToolCallCard.render.test.tsx`.
- 2.2.3 - `MessageItem` continues importing only `ToolCallCards` from the existing public module path. file: `web/src/components/chat/MessageItem.tsx`.
- 2.2.4 - Every touched production TS/TSX file is below 1,000 physical lines after Prettier defaults are applied. behavior: `production source line ceiling`.

### 2.3 Decompose FilesTab tree and action surfaces [category: refactor] (depends: P1)
`kind: deliverable`

Targets:
- `web/src/components/activity/FilesTab.tsx::*` — scope-reason: retain file lifecycle and transport state while extracting tree and action rendering
- `web/src/components/activity/FilesTab.types.ts`
- `web/src/components/activity/FilesTabTree.tsx`
- `web/src/components/activity/FilesTabActionSurfaces.tsx`
- `web/src/components/activity/__tests__/FilesTab.test.tsx::*` — scope-reason: preserve file lifecycle, accessibility, layout, and action behavior across extraction
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: point source-level tree typography assertions at the new rendering owner
- `web/src/components/chat/__tests__/ToolCallCard.interactive.test.tsx::*` — scope-reason: point the Markdown host census at the extracted file-view rendering owner if needed

Use direct extraction and keep `FilesTab` at its current public module path.
Move shared file-entry/context-menu types to `FilesTab.types.ts`. Move recursive
tree rows, file/folder icons, rename-input presentation, and git-status badges
to `FilesTabTree.tsx`. Move context-menu and move/create/delete confirmation
presentation to `FilesTabActionSurfaces.tsx`.

Keep project-keyed lifecycle, request cancellation, tree/file fetching, selected
file, editable-content state, persistence, and all mutation operations in
`FilesTab.tsx`; extracted renderers receive narrow state and callbacks. Update
source-level typography and Markdown-host assertions to follow the new owner
without weakening their checks. Preserve tree ARIA structure, keyboard behavior,
focus, drag/drop, layout switching, stale-request cancellation, editing, and
error behavior.

**Acceptance:**

- 2.3.1 - Layout, project reset/cancellation, tree accessibility, file loading/editing, delete, rename, move, create, drag/drop, and failure behavior remain green. test: `web/src/components/activity/__tests__/FilesTab.test.tsx`.
- 2.3.2 - Extracted tree and action modules are presentation-only; `FilesTab` remains the single owner of transport and mutable file lifecycle state. behavior: `FilesTab direct-extraction ownership contract`.
- 2.3.3 - Source-level typography and Markdown host audits follow the extracted rendering owner and retain their original assertions. test: `web/src/components/activity/__tests__/typographyLadder.test.ts`.
- 2.3.4 - Every touched production TS/TSX file is below 1,000 physical lines after Prettier defaults are applied. behavior: `production source line ceiling`.

## P3: Adopt and Enforce Prettier
`kind: framing`

**Goal**: Install deterministic formatter tooling, apply one isolated baseline,
and make the read-only check part of every existing frontend gate.

### 3.1 Configure pinned Prettier and Tailwind formatting [category: config] (depends: P2)
`kind: deliverable`

Targets:
- `web/package.json::*` — scope-reason: add formatter scripts and exact dev-dependency pins across package metadata
- `web/package-lock.json`
- `web/prettier.config.mjs`
- `web/.prettierignore`
- `docs/guides/frontend-style-guide.md`

Add exact dev dependencies for Prettier and the Tailwind plugin and refresh the
npm lockfile. Add `format` (`prettier --write .`) and `format:check`
(`prettier --check .`) scripts. Use an ESM Prettier config with the Tailwind
plugin loaded last, `tailwindStylesheet: './src/styles/index.css'`,
`tailwindFunctions: ['cn', 'clsx', 'cva', 'twMerge']`, and
`tailwindPreserveDuplicates: true`. Set no core style overrides so Prettier
defaults are authoritative.

Expand `.prettierignore` to cover `node_modules`, `dist`, `dist-setup`, `.vite`,
`coverage`, Playwright/test outputs, `package-lock.json`, copied VAD/Ghostty
assets, transcript JSON fixtures, and
`src/api/runtimeConfigCodecVectors.gen.ts`. Keep the hand-maintained public audio
worklet in scope. Document formatter ownership, scripts, exclusions, and the
rule that version upgrades require an intentional formatting baseline.

**Acceptance:**

- 3.1.1 - Formatter and plugin are exact-pinned at the confirmed versions in both npm dependency metadata and lock state. file: `web/package.json`.
- 3.1.2 - `npm run format` writes the configured workspace and `npm run format:check` performs the same read-only check. behavior: `web formatter script contract`.
- 3.1.3 - Tailwind 4 custom utilities are loaded from the canonical stylesheet and helper-built class strings participate in sorting while duplicates are preserved. file: `web/prettier.config.mjs`.
- 3.1.4 - Generated, copied, dependency-lock, transcript-fixture, dependency, coverage, and build/test outputs are excluded, including the generated runtime codec fixture. file: `web/.prettierignore`.

### 3.2 Apply the isolated repository-wide formatting baseline [category: refactor] (depends: 3.1)
`kind: deliverable`

Target: `web/prettier.config.mjs`

Run the new formatter once across its complete configured scope after dead-code
removal and all three decompositions are green. Treat the resulting source
rewrite as a mechanical baseline: make no behavioral fixes, lint-rule changes,
or dependency changes in this step. Review the diff for unexpected parser or
Tailwind transformations, especially arbitrary-variant escaping and source-text
tests, then run the formatter again to prove idempotence.

Land this rewrite as its own commit so later blame can ignore it and semantic
changes remain reviewable. Verify the generated runtime codec fixture and all
ignored paths remain byte-identical to their pre-baseline state.

**Acceptance:**

- 3.2.1 - `npm run format:check` succeeds over the complete configured scope immediately after the baseline. behavior: `Prettier full-baseline check`.
- 3.2.2 - A second `npm run format` produces no tracked diff. behavior: `Prettier idempotence`.
- 3.2.3 - The baseline changes only formatter-owned files and leaves every ignored/generated artifact byte-identical. behavior: `formatter exclusion integrity`.
- 3.2.4 - Every formatter-touched hand-maintained production source remains below 1,000 physical lines. behavior: `production source line ceiling`.

### 3.3 Add format checking to CI and both pre-push suites [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `.github/workflows/ci.yml`
- `pre-push-test-short.sh::*` — scope-reason: add a reported frontend format-check stage to the short suite
- `pre-push-test.sh::*` — scope-reason: add a recorded frontend format-check stage to the full suite
- `tests/ci/test_postgres_test_stack.py::*` — scope-reason: pin the format-check command and reporting contract in both scripts and frontend CI

Add a named `npm run format:check` step to the existing frontend lint CI job
after `npm ci` and before ESLint. Add a dedicated frontend-format section to
both pre-push scripts before TypeScript/ESLint validation. The short script must
pipe output to its own timestamped report, capture the pipeline status, and
increment `FAILED` on failure. The full script must write its own timestamped
report, capture the command exit code, and call `record_command_result` with a
stable `frontend-format` key.

Extend the existing CI/pre-push contract test module to assert the command is
present in all three gates and that both scripts preserve their established
failure aggregation and reporting behavior. Do not execute either pre-push
suite from this task because the full script invokes the prohibited full pytest
suite.

**Acceptance:**

- 3.3.1 - Frontend CI installs locked dependencies and fails when `npm run format:check` reports drift. file: `.github/workflows/ci.yml`.
- 3.3.2 - Both pre-push scripts execute the same read-only check, preserve later validations, and report a formatter failure through their existing aggregate result. test: `tests/ci/test_postgres_test_stack.py`.
- 3.3.3 - `bash -n pre-push-test-short.sh pre-push-test.sh` succeeds. behavior: `pre-push shell syntax`.

## Q1: Verification
`kind: verification`

Run focused tests after each direct extraction, then run the complete frontend
validation only after the baseline and enforcement work:

```bash
cd web
npm run format:check
npm run lint
npm run type-check
npm test
npm run build
cd ..
bash -n pre-push-test-short.sh pre-push-test.sh
GOBBY_TEST_PROTECT=1 uv run pytest tests/ci/test_postgres_test_stack.py -v
git diff --check
```

Also verify with a fresh projected-line-count pass that every formatter-touched
hand-maintained production TS, TSX, JS, CSS, MJS, and CJS file is below 1,000
physical lines. Review the baseline diff separately from semantic/structural
commits and confirm ignored artifacts are unchanged.

## V1 Plan Changelog
`kind: verification`

Lightweight draft; no enhancement or adversarial review rounds requested.
