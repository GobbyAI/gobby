Plan artifact: `.gobby/plans/minimal-sandbox-fixes.md`

# Minimal sandbox fixes with post-restart regression checks

**Plan ID:** minimal-sandbox-fixes

## F1 Scope and execution
`kind: framing`

Implement existing #22028 first, then #22027. Coordinator authorization is an
audited assertion. Deliberate evasion and trustworthy human approval belong to
a separate security review. No approval UI, grant table, migration, command
bypass, environment escape, or session-variable exemption is introduced.
Both tasks remain open until the combined post-restart gate passes.

Implementation substeps (native tracker unavailable in this provider):
- [x] Diagnose live native cmd alias failure; normalization repair passes 653 focused tests.
- [x] Fix 71 encountered baseline type errors in normalization tests; scoped mypy, test-type and quality audits pass with zero findings.
- [x] Commit alias repair 4b5262b8b4, restart at 21:37 UTC and pass 20 live hook cases.
- [x] Repair no-isolation spawn replacing an explicit worktree with the main checkout; focused regression reproduced the defect, all 8 project-scope tests now pass.
- [x] Update stale spawn-failure reason and batch grant-summary expectations found by broader focused checks.
- [ ] Commit workspace repair and repeat affected live checks after restart.
- [x] Materialize and validate this plan (base validation passed).
- [x] Implement and automatically verify #22028; live smoke gate remains open.
- [x] Repair baseline provider tests that outlive their launch mocks (11 pass).
- [x] Fix the 20 encountered untyped factory test signatures and the new batch input annotation.
- [x] Implement and automatically verify #22027; final live rule gate remains open.
- [x] Coordinator repaired grant inspection and run-local zsh heredoc temporary paths (622ef8c992; 73 focused tests pass).
- [x] Coordinator extracted sandbox run environment (efe1644; policy 812 lines, new module 49 lines; 84 focused tests pass).
- [x] File deeper security review #22103.
- [ ] Commit both fixes and coordinate integration and restart.
- [ ] Complete real managed runtime, delegation, resume and sandbox probes.
- [ ] Record five continuous clean minutes after all smoke work completes.
- [ ] Close both tasks with linked commits after the combined live gate passes.

Other sessions own terminal/gclient changes. Ask integration is on a separate
branch and will reconcile these fixes before its final native proof. Obtain a
fresh safe restart checkpoint from #12261 and other affected sessions.

## P1 Sandbox fixes
`kind: framing`

### A1 Propagate external write roots (#22028)
`kind: deliverable`

Targets:
- `src/gobby/agents/external_write_grants.py::*` — scope-reason: validate and audit external grants
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: integrate grant preflight and extract context assembly
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::*` — scope-reason: assemble resolved launch requests
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::*` — scope-reason: assemble request context and resume metadata
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: expose MCP and batch grant inputs
- `src/gobby/servers/routes/agent_spawn.py::*` — scope-reason: expose HTTP and batch grant inputs and results
- `src/gobby/agents/resume_executor.py::*` — scope-reason: revalidate grants before resume allocation
- `src/gobby/mcp_proxy/tools/agents_payloads.py::*` — scope-reason: expose recorded external grants in run inspection
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: constrain zsh heredoc temporary files to the current run
- `src/gobby/agents/sandbox_run_environment.py::*` — scope-reason: own run paths and subprocess environment redirects
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: propagate run-local TMPPREFIX through Codex shell overrides
- `tests/agents/test_external_write_grants.py::*` — scope-reason: verify grant authority and path boundaries
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py::*` — scope-reason: verify integration and repair asynchronous test lifecycle
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: verify MCP grant propagation
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py::*` — scope-reason: assert persisted spawn failure diagnostics
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: assert batch grant summary
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py::*` — scope-reason: preserve requested existing worktree in launch and sandbox policy
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: verify HTTP grant propagation
- `tests/agents/test_resume_executor.py::*` — scope-reason: verify resume grant propagation
- `tests/mcp_proxy/tools/test_agent_capture_results.py::*` — scope-reason: verify recorded grant inspection
- `tests/agents/test_spawn_executor_support.py::*` — scope-reason: verify Codex shell temporary environment
- `tests/agents/test_sandbox_policy.py::*` — scope-reason: consume the extracted run environment definitions
- `tests/integration/sandbox/test_srt_host_runtime.py::*` — scope-reason: verify real managed SRT heredoc writes
- `docs/guides/sandboxing.md`

Add optional extra_write_paths and write_paths_reason to managed MCP and
HTTP/batch inputs. Nonempty paths require a nonblank reason. Validate before
allocating resources: existing absolute directories, canonical symlink and
traversal resolution, stable deduplication, no filesystem/home root or overlap
with protected credential/daemon roots. Use recorded caller session/run
relationships. Coordinators may assert new roots; managed children explicitly
delegate only recorded roots or contained subdirectories. Never inherit grants
automatically. Merge into sandbox configuration preserving worktree, runtime
cache and sensitive-path policy. Record requested/canonical roots, reason,
asserting session, parent run and timestamp in run metadata and results.
Resume preserves the grant and revalidates existence, canonical identity and
protected restrictions before launch, explicitly failing on changes.

Split request assembly from `_implementation.py` into `_request.py` and
context assembly into `_runtime.py`, reusing
the implementation on the concurrent Ask integration branch,
to keep changed production files below 1,000 lines. Preserve strict Ask policy
when its integration lands; grants cannot widen its validated scratch policy.

Split run-path and environment definitions from
`src/gobby/agents/sandbox_policy.py` into
`src/gobby/agents/sandbox_run_environment.py`; the coordinator owns this
extraction (858 lines before the split on commit 622ef8c992).

**Acceptance:**
- A1.1 - All managed input surfaces propagate explicit audited roots; omission grants nothing additional. file: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`.
- A1.2 - Invalid paths, missing reasons, aliases, symlinks, traversal, protected overlap, sibling-prefix confusion, child narrowing/widening and absent inheritance are tested. file: `tests/agents/test_external_write_grants.py`.
- A1.3 - Resume fails explicitly on changed grants and preserves valid grants. file: `src/gobby/agents/resume_executor.py`.
- A1.4 - Run inspection and rendered sandbox policy expose the same effective grant; live writes succeed only within granted roots. behavior: "Explicit external workspace grants" in `docs/guides/sandboxing.md`.

### A2 Block direct provider launches (#22027) (depends: A1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/provider_launch_guard.py::*` — scope-reason: implement bounded literal execution classification
- `src/gobby/hooks/_normalization_tools.py::*` — scope-reason: normalize cmd for every command-pattern rule while preserving raw input
- `tests/hooks/test_normalization.py::*` — scope-reason: verify canonical command alias and raw-input preservation
- `src/gobby/workflows/safe_evaluator.py::*` — scope-reason: register shell classifier for rules
- `src/gobby/install/shared/workflows/rules/worker-safety/block-direct-provider-launch.yaml::*` — scope-reason: install the default and worker safety guard
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate bundled rule content hashes
- `tests/hooks/test_provider_launch_guard.py::*` — scope-reason: verify classifier and real engine rule selection
- `docs/guides/sandboxing.md`

Add a default-enabled before_tool rule for all observed shell calls, including
top-level sessions. Reuse shell normalization/scanning for codex, claude, droid,
grok, qwen and agy including absolute executables. Allow literal help/version
and supported authentication-status forms; deny interactive, prompt, exec,
resume and unknown forms. Cover chains, pipelines, environment assignments,
common execution wrappers, literal shell -c payloads, command substitutions
and executable heredocs. Quoted documentation is data. Denials explain the
managed spawn path and its external-root arguments and use existing rule-event
auditing. Preserve daemon-managed launches. Document limits for arbitrary
programs, dynamic construction, renamed executables and rule tampering.

**Acceptance:**
- A2.1 - Classification tests cover providers, wrappers, administration, ordinary subprocesses and quoted documentation. file: `tests/hooks/test_provider_launch_guard.py`.
- A2.2 - Installed default-enabled rule rejects prohibited launches through the live hook path while managed spawning succeeds. file: `src/gobby/install/shared/workflows/rules/worker-safety/block-direct-provider-launch.yaml`.
- A2.3 - Guard limits and managed-spawn recovery are documented. behavior: "Direct provider launches" in `docs/guides/sandboxing.md`.

## V1 Validation and post-restart gate
`kind: verification`

Run focused tests with isolated DATABASE_URL and GOBBY_TEST_PROTECT=1, lint,
format, type checks and test audits. Never run the full pytest suite.

1. Integrate fixes into main checkout, coordinate quiet restart, restart and
   confirm daemon health and the installed DB rule's enabled state.
2. Exercise prohibited hook requests without executing them; verify allowed
   administration and ordinary shell calls.
3. Launch a real managed agent in a disposable worktree with a disposable
   external root. Verify startup, tracking, transcript, visible grant and completion.
4. Create .vite-temp within the grant; reject ungranted sibling, protected paths
   and a symlink escape. Verify a managed spawn without extra roots.
5. Exercise narrowed child delegation and resume with identical restrictions.
6. Observe daemon and relevant managed runtime logs continuously for at least
   five minutes after workload completion. Expected negative-test denials must
   be identifiable; unexplained errors, exceptions, sandbox failures and
   lifecycle warnings fail the gate.

Record restart time, commands/results, run IDs and observation interval. After
related repairs repeat affected tests and restart the clean-log window; restart
the daemon again for loaded code/configuration repairs. Fix related regressions.
File unrelated findings with diagnostics, reproduction and impact; unexplained
findings cannot be classified away. Commit before close; link evidence and
commits when closing both existing tasks. File a deeper security review covering
process evasion, same-user credentials, UI automation, approval provenance and
policy tampering.
