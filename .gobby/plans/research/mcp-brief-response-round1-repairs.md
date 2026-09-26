# mcp-brief-response-contract: round-1 repair record

Plan: `.gobby/plans/mcp-brief-response-contract.md`, authored by the Researcher (gobby#14550)
under #22909 (closed at 918baca183; candidate sha256
`2a93b33d851c569addc670eaf3dbdf81bedad3d6efbc7789fdb29c60afccdfb3`). Adversary (gobby#14579)
round 1: message `045318d9-d46b-48f8-9bd2-da4798ec9550`, NEEDS REVIEW, findings BR-01..BR-07.
The PD assigned the repair to the Plan Writer (gobby#14578); it runs under #22930 on branch
`task-22930-brief-response-round1`. No evidence round was minted (LOAD HOLD), so no checkpoint
handshake applies; the repaired candidate goes to the PD first, then to the Adversary for a
fresh round.

## Facts verified with gcode before repair

- `_agent_result_payload` (`src/gobby/mcp_proxy/tools/agents_payloads.py:133-214`) adds
  `prompt` at `:168` only when `include_prompt` is true;
  `tests/mcp_proxy/tools/test_agents.py::TestGetAgentResult::test_prompt_is_opt_in` (`:242`)
  pins default omission.
- `FoundWorkEntry.as_note` (`src/gobby/sessions/handoff_records.py:29`) is
  `Found work: {finding} ({disposition} {ref})`; `normalize_found_work` accepts any non-blank
  finding, so a note may span lines. `build_handoff_payload` (`:74-144`) renders found-work
  notes first under `## Notes` as `- ` bullets. `ConsumedHandoff` (`src/gobby/sessions/handoff.py:117`)
  carries only `markdown` and `found_work`; there are no structured sections to project from.
- `tests/workflows/test_workflows_dry_run.py` holds only unit tests of the dry-run evaluator;
  no `test_bundled_agent_definitions_dry_run` exists. The bundled-definition loader
  (`AGENTS_DIR`, `_load_yaml`) lives in `tests/workflows/test_workflows_agent_definitions.py`.
- `tests/servers/routes/test_rules_routes.py` exists (class-based, `test_list_all_rules` at
  `:119`); `tests/mcp_proxy/test_instructions.py` exists
  (`test_instructions_name_gated_skill_file_tools` at `:161`). Neither was a Target.
- Timing sites beyond the call/list/schema routes: `_timeout_response_payload`
  (`src/gobby/servers/routes/mcp/endpoints/execution.py:110`), the unknown-tool error envelope
  in `call_mcp_tool` (`:620`), the `mcp_proxy` handler (`:777`), `search_mcp_tools`
  (`discovery.py:468`), `set_mcp_server_enabled` (`server.py:471`), `get_mcp_status`
  (`registry.py:99`).
- Isolated fixtures the leaf tests already build: `create_memory_registry(lambda:
  mock_memory_manager)` and `MockMemory` (`tests/mcp_proxy/tools/test_memory_tools.py:35,115`);
  `test_memory_review.py::_registry` (`:55`); `create_task_registry(mock_task_manager)` and
  `sample_task` (`tests/mcp_proxy/tools/conftest.py:98,106`); `_make_mock_agent_run` and
  `_make_runner_with_run_storage` (`tests/mcp_proxy/tools/test_agents.py:59,119`);
  `register_crud_tools` over a mock session manager
  (`tests/mcp_proxy/tools/test_sessions_query_tools.py:24,46`); `session_manager` over
  `temp_db` and `_payload` (`tests/sessions/test_handoff_found_work.py:68,109`);
  `rule_tools` over `temp_db` (`tests/mcp_proxy/tools/test_rule_tools.py:25-76`). `temp_db`
  is `tests/conftest.py:371`, the isolated test hub.
- `docs/contracts/plan-coverage.md:44`: verification sections summarize end-to-end checks and
  carry no acceptance items; `:522`: `manual` is valid only for direct tasks.

## Dispositions

- BR-01 fixed. 1.13 is no longer a `category: manual` deliverable. It is a `category: test`
  leaf (paired brief/full parity check, one test module) so it expands 1:1, and the
  post-restart live check plus the seven-day re-measurement moved to a new `## V1:
  Verification` (`kind: verification`) section that assigns them to the PD's direct-task path
  (`category: manual` direct task for the Researcher or the PD). Both measurement obligations
  are preserved.
- BR-02 fixed. 1.8's brief allowlist now keeps `prompt` whenever `include_prompt` is true,
  citing `agents_payloads.py:168`; acceptance 1.8.6 names
  `test_prompt_opt_in_survives_brief` beside `test_prompt_is_opt_in` and covers all four
  flag combinations.
- BR-03 fixed. 1.10 specifies structural removal: inside the `## Notes` region only, remove
  the bullet block (the `- ` line plus continuation lines up to the next bullet or heading)
  whose text equals `"- " + entry.as_note()` for each returned entry; no prefix matching, no
  matching outside the region, an emptied `## Notes` heading is dropped, everything else is
  byte-identical, and the `agent_run_id` path is unchanged. Acceptance 1.10.4 covers
  multiline, look-alike text outside Notes, no found work, full mode, and the `agent_run_id`
  path; 1.10.1 now says "block", not "line".
- BR-04 fixed. 1.7.4 owns a new
  `tests/workflows/test_workflows_agent_definitions.py::test_no_bundled_definition_prescribes_full_get_task`
  and that file is a `::*` Target; 1.11 adds `tests/servers/routes/test_rules_routes.py::*`
  and 1.12 adds `tests/mcp_proxy/test_instructions.py::*` as Targets with scope reasons; every
  "(or the existing ...)" placeholder is gone; the 1.7, 1.11, and 1.12 verification lines name
  the files.
- BR-05 fixed (with BR-01). The 1.13 test leaf defines representative paired inputs per tool
  from the isolated fixtures above, the measurement envelope (the tool's returned dict as
  `json.dumps(value, default=str)`, before the proxy envelope), shape and semantic parity
  (`must_match` handles and outcomes equal, brief keys within full keys plus declared derived
  keys, no Decision Record 5 field in brief), and a stated size criterion (`max_ratio` 0.5 for
  the body-dropping tools, 0.99 for the duplicate-dropping ones). Mutation and consume paths
  (`create_memory`, `review_task_memories`, `close_task`, `wait_for_agent`, `get_handoff`) run
  only on those fixtures; the V1 live check calls read-only tools only, with named arguments.
  The seven-day report is a separate observational step in V1, not a gate.
- BR-06 fixed. Both Constraints cross-references now say 1.8.
- BR-07 fixed. 1.1.3 drives every envelope: `call_mcp_tool` success, timeout, and
  unknown-tool error paths, the `mcp_proxy` handler, `list_mcp_tools`, `get_tool_schema`,
  `search_mcp_tools`, `set_mcp_server_enabled`, and `get_mcp_status`, asserting an integer
  `response_time_ms` on each.

Disagreements carried to the PD: none.
