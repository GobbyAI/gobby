# node-halves: Orchestrator return of 2026-10-14 (working context)

Plan: `.gobby/plans/node-halves.md`. Stamp b872dc8629 was returned by Orchestrator
gobby#15977 for five amendments. M1 was withdrawn at 6f2cbff (narrative only,
validation clean, plus a V1 line recording the return). Line numbers below are
for the file at 6f2cbff.

## Item 1: cross-plan edge table

Add one framing section, "Cross-plan edges set at expansion", holding a table.
Each row gives the node-halves leaf, the exact external ref and the edge type
(`blocks` or `blocked by`). It must cover every prose edge:
- Constraints edge list, lines 279-322.
- Decision 20: every leaf blocks hub-mode 5.1, the live proof.
- Decision 21: #21586 route leaves block 2.11. The Orchestrator wants those
  leaves by id; look up #21586's children with gobby-tasks.
- Decision 24: gmcp-stdio 1.6 (#23275) blocks 2.3 and 2.6, and gmcp-stdio 1.8
  blocks 2.4.
- Lines 152-155 and 194-196; read them first.
- #24151 (the hub STOP epoch) if a prose line makes it an edge.

Name hub-mode leaves by number, with #ids where tasks exist: 1.3 #24269,
1.4 #24270, 2.3 #24273, 2.4 #24274, 2.5 #24275, 2.6 #24276. hub-mode 1.2, 4.1,
4.2, 4.6 and 5.1 have no task ids yet.

Keep the prose Constraints list or point it at the table. Check whether the
parser accepts a new `## ...` framing section with a `kind: framing` line; if
it doesn't, put the table inside Constraints.

## Item 2: the node fails closed on its sandbox

- 2.7 dispatch (around line 1727): a payload whose `sandbox` is not
  `enabled` with backend `srt` answers `invalid_payload` with `"none"`. It uses
  the hub's message, "spawn_agent requires managed SRT (enabled=<e>,
  backend=<b>)", from `src/gobby/agents/sandbox_gate.py::require_managed_srt`,
  before anything is recorded or prepared. Add a 2.7 acceptance item with
  test `crates/gdaemon/tests/node_agents.rs::spawn_refuses_unmanaged_sandbox`.
- 2.5 chokepoint (around lines 1456-1460): remove the `enabled`-false
  identity wrap and the `provider-native` branch. `prepare_launch` always runs
  the `srt` steps, so no unwrapped launch exists. Edit 2.5.1 to drop "a
  disabled sandbox leaves the argv unchanged", delete 2.5.7 (provider-native
  claude), and update the 2.5 granularity count from seven items to six.
  Keep the Python research bullets as facts and add that the node does not
  port the disabled or provider-native paths.
- 2.4: drop the provider-native port (settings.rs's
  `ClaudeSandboxResolver.build_settings` and
  `preflight_provider_native_settings`, around line 1350), the corpus case
  "claude on provider-native" (around line 1360), the 2.4.2 provider-native
  mention (around line 1374) and 2.4.4's provider-native clause (around line
  1376).
- Add Decision 32: the node fails closed. The rationale is the Orchestrator
  return (2026-10-14), local `require_managed_srt`, and that spawned agents
  are always managed SRT.
- 3.1 contract doc: state that the node refuses anything that isn't enabled
  `srt`.

## Item 3: hub redaction of node pane text

The function is `src/gobby/utils/terminal_output.py::redact_terminal_output`.
Each route runs it on every node-supplied pane string first, before it stores,
logs or broadcasts anything:
- 3.3 report route, in `src/gobby/servers/routes/nodes.py`: run it before
  `record_remote_workspaces` and `put` on `agents[].pane.tail` and on the
  attention payload's `excerpt` and each `options` entry. The payload is
  pane-derived; see `src/gobby/agents/prompt_detector.py` `DetectedPrompt`.
  The fingerprint stays as sent.
- 3.5 prompt-delivery-failure route, `pane`. `_codex_prompt_failure_reason`
  already redacts (`spawn_executor_support.py:264`), but the route must log
  nothing raw.
- 3.6 terminate route, `pane_tail` (and `detail`). The redacted tail goes into
  the error text and into 3.3's store.

Add one acceptance item per route, each asserting that a secret-shaped string
in the tail reaches neither storage, the 3.3 store, the attention row nor a
broadcast. Tests:
- `tests/servers/test_node_routes.py::test_report_pane_text_redacted` (3.3)
- `tests/servers/test_node_run_routes.py::test_prompt_delivery_pane_redacted` (3.5)
- `tests/servers/test_node_run_routes.py::test_terminate_pane_tail_redacted` (3.6)

Check each section's Targets and test files, and its research and
implementation bullets.

## Item 4: test-backed doc checks

- 1.1.1: new test file `tests/contracts/test_hook_ingress_doc.py::test_doc_states_frozen_contract`.
  It reads `docs/contracts/hook-ingress.md` and asserts the named sections and
  literals: the 409 statuses, the three `retry_kind`s,
  `_gobby_delivery_receipt`, the delivery-receipt file keys, the 120-second
  grace, `.stop_replay_epoch`, and "Node forwarding". Add the file to 1.1's
  Targets.
- 3.1.4: `tests/hub/test_node_command_corpus.py::test_contract_doc_covers_version_2`.
  Every Python node-command model field and error code appears in the doc,
  and `remote_resume_unsupported` does not.
- 3.1.5: `tests/hub/test_node_command_corpus.py::test_fields_not_carried_matches_spawn_request`.
  The doc's "Fields not carried" section names exactly
  `{f.name for f in dataclasses.fields(SpawnRequest)} - SpawnPayload.model_fields`,
  each with a reason. `SpawnRequest` is the `@dataclass` in
  `src/gobby/agents/spawn_models.py`.
- Add the new tests to 3.1's Targets, as `path::test` entries like the others.

## Item 5: manifest domains

`src/gobby/plans/manifest_emitter.py::_implementation_domain_for` classifies
only `src/gobby/` as backend and `web/` and `crates/gclient/` as frontend. Any
other target, `crates/gdaemon/` included, falls back to keyword regexes. That
made 1.5 frontend and 2.6 fullstack.
- `derive_manifest_entries` accepts per-section routing decisions with
  `implementation_domain`, so ask adversary gobby#16791 to derive with 1.5 and
  2.6 set to backend, and every leaf backend.
- 1.1 is `[category: test]`, whose only agent is `developer`
  (`_AGENT_BY_CATEGORY`), and a test leaf cannot carry a domain. Planned
  change: recategorize 1.1 as `[category: code]`, backend, and ask the
  adversary to route `tdd: false`, because its tests pin an existing route.
  State this choice to #15977.
- A classifier gap to hand to the owner via #15977: `crates/gdaemon/` and the
  other Rust daemon crates should classify as backend.

## Finish

Write a V1 line for the amendments, validate bare, then commit in three calls
(add, ocr rule, commit). Send gobby#16791 the SHA for a fresh review and
re-derivation, including the routing decisions for item 5. At consensus, add
a V1 consensus entry and commit; gobby#16791 derives M1; commit the rendered
bytes unchanged. Send #15977 the new stamped SHA, rendered hash and leaf
count.
