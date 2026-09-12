# Verification record

This records development evidence for Gobby task #22225. It is not a claim
that the complete browser and device acceptance matrix has passed.

## Automated checks

Run the npm commands from `gobby-annotate/`.

| Command                                     | Recorded result       | Coverage                                                                                      |
| ------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------- |
| `npm run build`                             | Passed                | Bundled Chrome, Safari and standalone MCP outputs                                             |
| `npm run typecheck`                         | Passed                | Strict TypeScript across workspace and tests                                                  |
| `npm run lint`                              | Passed                | Workspace JavaScript and TypeScript                                                           |
| `npm run format:check`                      | Passed                | Workspace formatting                                                                          |
| `npm test`                                  | 42 tests passed       | Capture validation, selection, screenshots, drafts, exports, MCP and CLI                      |
| `npm test -- packages/mcp/test/cli.test.ts` | 2 tests passed        | Inspection, validated extraction, existing destination and concurrent destination reservation |
| `npm run test:browser`                      | 1 scenario passed     | Actual Chromium extension, native screenshots and downloaded ZIP decoding                     |
| `npm run test:safari`                       | 3 native tests passed | Chunked staging, interrupted transfers and byte-identical shared-core bundle round trip       |

The Chromium scenario verifies repeated activation, reload, small viewports,
theme changes, dragging, scaled same-origin frame geometry after scrolling,
open shadow roots, inaccessible frame hosts, simulated touch rectangles,
native screenshots without annotation UI, comment editing and five-note export.
It injects failed IndexedDB transactions for comments and batch names, verifies
that closing, switching modes, collapsing, deactivating or exporting cannot
discard unsaved input, and confirms retry persists it. The collapsed control's
rendered bounds are checked against the 44-pixel target minimum.
An actual second tab also edits the same note: the first tab retains its
conflicting text, explicit discard reloads the saved version, and subsequent
editing succeeds.
It uses a disposable extension copy with test-only host permission because
headless automation cannot click the browser action. This does not validate
the production browser-action `activeTab` permission grant.

The 320×280 editor check simulates limited visible space. It does not substitute
for a physical software keyboard or real touch testing.

Both unsigned Safari build commands passed with no diagnostics:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -quiet \
  -project safari/GobbyAnnotate.xcodeproj -scheme 'GobbyAnnotate (macOS)' \
  -destination 'platform=macOS,arch=arm64' -configuration Debug \
  -derivedDataPath safari/build CODE_SIGNING_ALLOWED=NO build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -quiet \
  -project safari/GobbyAnnotate.xcodeproj -scheme 'GobbyAnnotate (iOS)' \
  -destination 'generic/platform=iOS Simulator' -configuration Debug \
  -derivedDataPath safari/build CODE_SIGNING_ALLOWED=NO build
```

From the repository root, the isolated integration and template checks passed
19 tests:

```sh
DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" \
  GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/mcp_proxy/test_annotate_mcp.py tests/mcp_proxy/test_mcp_templates.py -q --tb=short
```

The broader affected routing checks passed 380 tests:

```sh
DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" \
  GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/hooks/test_inline_mcp_dispatcher.py \
  tests/mcp_proxy/services/test_tool_proxy_coverage.py \
  tests/mcp_proxy/test_mcp_proxy_runtime_config_resolution.py \
  tests/mcp_proxy/test_metrics_events.py tests/mcp_proxy/test_plans_tools.py \
  tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py \
  tests/mcp_proxy/tools/test_agents_spawn_evaluation.py \
  tests/servers/routes/mcp_endpoints/test_execution_offload.py \
  tests/servers/test_mcp_programmatic_boundary.py tests/servers/test_mcp_routes.py \
  tests/workflows/pipeline/test_handlers.py \
  tests/workflows/test_skill_loaded_call_tool_path.py -q --tb=short
```

These exercise registered internal identities versus external server routing.
The annotation test starts its own daemon and temporary capture root; it checks
installed templates and skill records, project-scoped server creation, schema
discovery, note/image reads, reconnect and daemon restart. Its task scenarios
exercise API contracts for standalone tasks, epics, stable labels, revisions and
partial imports. They do not constitute an independent agent executing the skill.

## Plan validation

Plan target parsing also passed 33 focused tests after correcting support for
spaces in Xcode paths. The following checks passed:

```sh
DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/plans/test_symbol_targets.py -q --tb=short
uv run mypy src/gobby/plans/symbol_targets.py src/gobby/mcp_proxy/tools/internal.py
uv run ruff check src/gobby/plans/symbol_targets.py tests/plans/test_symbol_targets.py
uv run ruff format --check src/gobby/plans/symbol_targets.py tests/plans/test_symbol_targets.py
uv run gobby test-quality audit tests/plans/test_symbol_targets.py \
  --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
uv run gobby test-types audit tests/plans/test_symbol_targets.py \
  --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json
```

File-based plan validation exits successfully but retains a consumer-coverage
warning for six unchanged callers of `InternalRegistryManager.is_internal`.
Those paths were included in the routing verification above. The current plan
contract requires them as edit targets; doing so also demands unrelated splits
for large callers. This contract conflict remains unresolved; the plan is not
being represented as warning-free or expansion-ready.

## Live managed attachment evidence

The built npm package was installed globally, its executable resolved through
the normal subprocess environment, and the installed template and skill were
read back. The project-scoped server was created through `add_mcp_server` with
the bundled template. Gobby launched and connected it.

A real Chromium export was placed in the configured project capture root.
Gobby's proxy discovered schemas and retrieved an annotation and its native
1440×900 screenshot. Normal disable/re-enable reconnected the server and the
annotation remained readable. No private launch path or live test tasks were used.

## Remaining acceptance work

Pre-commit review covered 84 of 93 staged files (90.3%). Seven derived logo PNGs,
the generated npm lockfile, and full generated Xcode project scaffolding were
excluded from line review. The Xcode target settings and resource/source phases
were inspected, and both platforms build. Review fixes cover autosave recovery,
collapsed target size, timestamp precision and export identity after replacement.
No critical or high findings remain from that review.

- Install signed Safari builds on macOS, iPhone and iPad; exercise native capture,
  staging and actual Save/Share delivery. This needs signing configuration and
  suitable devices.
- Verify production Chrome browser-action activation and site permission prompts.
- Exercise actual Gobby tier preview, physical touch, rotation and software
  keyboards at the planned viewport sizes.
- Exercise navigation during capture, storage failure and cancelled export in
  installed browsers, in addition to the automated transaction tests.
- Run an independent annotation-skill import against isolated task state.

PNG validation checks framing, checksums, header fields and dimensions; it is
not a full raster decompression check. Capture roots and extraction parents
are local user-owned storage, not a sandbox against another local process
maliciously replacing ancestor directories during filesystem operations.
