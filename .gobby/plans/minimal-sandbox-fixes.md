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
- [x] Commit workspace repair 323753556f; restart22:02 UTC; both narrowed and no-grant children pass all checks.
- [x] Implement readiness gating for startup terminal monitors after real missing-terminal run c132c69d falsely completed and emitted subscriber warnings; 39 focused tests pass.
- [x] Update stale eligible-session status assertion found by lifecycle tests.
- [x] Commit readiness repair f9e41419b0 after 203 focused tests and clean audits; restart and repeat 20 live hook checks.
- [x] Gate tmux maintenance on startup readiness: commit 1189323508; 184 focused tests passed.
- [x] Align five stale tmux maintenance test status lists with existing interrupted/input/approval states.
- [x] Real native resume dcffee01 to 350a7c32 preserved session and exact grant after restart23:37:57 UTC.
- [x] Preserve resumed claims from cancelled-original recovery: commit 316041f7bf; 161 focused tests and actual resumed writes passed.
- [x] Repair two existing untyped task-recovery fixture signatures encountered by audit.
- [ ] Resolve shared editable-environment interference with owner #12261; daemon startup recovered after main uv invocation restored main imports.
- [x] Materialize and validate this plan (base validation passed).
- [x] Implement and automatically verify #22028; live smoke gate remains open.
- [x] Repair baseline provider tests that outlive their launch mocks (11 pass).
- [x] Fix the 20 encountered untyped factory test signatures and the new batch input annotation.
- [x] Implement and automatically verify #22027; final live rule gate remains open.
- [x] Coordinator repaired grant inspection and run-local zsh heredoc temporary paths (622ef8c992; 73 focused tests pass).
- [x] Coordinator extracted sandbox run environment (efe1644; policy 812 lines, new module 49 lines; 84 focused tests pass).
- [x] File deeper security review #22103.
- [x] Commit both fixes and coordinate integration and restart.
- [x] Complete real managed runtime, delegation, resume and sandbox probes.
- [ ] Repair ASGI chat shutdown ordering and missing standalone-listener cleanup, verify focused lifecycle tests, coordinate restart with browser repairs, and repeat affected live gate.
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
- `src/gobby/agents/tmux/pane_monitor.py::*` — scope-reason: defer terminal death classification until startup recovery completes
- `src/gobby/sessions/liveness_monitor.py::*` — scope-reason: preserve parked sessions during startup recovery
- `src/gobby/servers/_app_lifecycle.py::*` — scope-reason: wire existing startup readiness into terminal monitors
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: defer missing-terminal expiration until restart recovery finishes
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: wire startup readiness into tmux maintenance
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: stop ASGI chat sessions before HTTP lifespan closes hook workers
- `tests/test_asgi_chat_shutdown.py::*` — scope-reason: verify ASGI cleanup without a standalone listener and lifecycle ordering
- `tests/test_runner_shutdown.py::*` — scope-reason: align shutdown fixtures with initialized drain state and empty HTTP connection sets
- `src/gobby/agents/task_recovery.py::*` — scope-reason: preserve claims belonging to parked and resumed daemon-stop runs
- `tests/agents/test_task_recovery.py::*` — scope-reason: verify repeated recovery preserves resumed task ownership and mutex
- `tests/test_runner_maintenance_tmux_repair.py::*` — scope-reason: verify missing sockets survive startup recovery
- `tests/sessions/test_liveness_monitor.py::*` — scope-reason: align eligible terminal-owner status expectation
- `tests/test_terminal_startup_readiness.py::*` — scope-reason: verify monitors wait for startup reconciliation
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

## V2 Live execution evidence
`kind: verification`

Installed direct-launch rule `2c26ef1c-810d-4e12-9859-4a719c31e4ce` was enabled
after the September 10 restart at 23:51:02 UTC. Live hook checks passed all 20
cases (12 blocked, 8 allowed) during 23:52:54–23:55:08 UTC. Exact commands and
results: `/tmp/gobby-grant-smoke-wECAsK/live_hook_probe.py` and adjacent JSON.
Prohibited provider commands were never executed.

Managed worktree `842deb44-49ac-46bd-85e0-c7d5ea20c0f6` is at
`/Users/josh/.gobby/worktrees/gobby/sandbox-grant-final-smoke`. External grant:
`/private/tmp/gobby-grant-smoke-wECAsK/granted`. Narrowed child
`5d1aaa72-db31-4212-bf73-ee93f0a1477a` and omitted-root child
`957d8c87-fabc-42c7-b36e-db5945c41c0e` passed; exact evidence messages are
`cd793dab-051f-424f-a68a-c79828f774a1` and
`608afb58-f92a-45f1-ab90-b8335fac2e34`.

Actual native resume used these operator commands from main:

```sh
UV_NO_SYNC=1 uv run gobby stop --wait
tmux -L gobby kill-session -t '=gobby-49fba611-c75b-476d-aa20-f81a9cf880a5'
UV_NO_SYNC=1 uv run gobby start --verbose
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:60889/
```

Stop succeeded after 11.4 seconds; startup health passed and HTTP returned 200.
Successor started September 11 at 00:06:10.715085 UTC:
`d039f166-87af-44b6-819f-64f958971b20` became
`650cfdb5-2cda-4389-9cec-0c8ac8d126cc`. Both retain child session
`5ef3b16c-4002-4ba5-aed3-20064666df44` (#12742), native session
`01a08dbd-477e-7c81-885a-52481ce0ef2e`, and identical external grant metadata.
HTTP run inspection verified `resumed_from_run_id` and SRT enforcement.
Runtime policy hashes differ because run-local paths are renewed.

Initial exact script/results: message `9f66641e-030f-41ad-99f0-895fdbc2b7d5`.
Resumed exact script/results: message `f80cc6a2-5407-4e2d-afe7-241dd3113221`.
The identical login-zsh Python heredoc used fresh UUID filenames, exclusive
creation, JSON readback, and cleanup. Worktree and grant/.vite-temp succeeded.
Ungranted sibling, grant/escape symlink, ~/.ssh, and direct ~/.gobby writes
all failed EPERM. TMPPREFIX equaled TMPDIR + '/zsh'; resumed temp directory
was `gobby-5s5muzpu`. The script's hardcoded native ID had a 4776/477e typo,
corrected by the worker and verified against run metadata. Successor completed
with dirty_paths=[]; final handoff `265584a8-a596-4459-9cc5-60b9dd988d67`.
Both tasks remained open.

FAILED log window: September 11 00:08:18 UTC for 300.006 seconds, 978,319
appended bytes. Raw logs and per-file offsets/inodes/SHA256 are retained in
`/tmp/gobby-grant-smoke-wECAsK/observation-000818/summary.json` and adjacent
captures. Capture 18 contains a browser-disconnect ASGI exception; this interval
does not satisfy the clean-log gate. Session #12736 owns handshake and wake
repairs under #22142. Root owns shutdown ordering: ASGI mode skipped chat
cleanup and Uvicorn could close the hook worker before chat SESSION_END.
Two focused tests reproduced skipped cleanup before repair. No clean-log claim
is made yet.
