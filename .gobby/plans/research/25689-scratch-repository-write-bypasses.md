## Research Findings

### Research Questions

- Which calls created `cherrytest/repo3` and `cpx-probe-tmp` in gobby#19401's scratchpad?
- Which comparable bypasses can YAML close, and which require code?
- What is the smallest complete repair and the approved route for Git probes?
- What implementation follow-up belongs under #25662 (Friction: non-blocking)?

### Domain Context

- Task #25689, “How do seats create scratch git repos despite block-temp-scratchpad-writes?” Researcher gobby#19655; reviewer gobby#19656. Research only: no implementation, repository creation, network clone, process termination or daemon changes.
- **Two independent gaps exist.** The installed temporary-write rule is enabled but is not selected by the installed `default` definition used by gobby#19401. Standalone `git init`, `git clone` and `git worktree add` also classify as `execute` without destination paths, outside the rule's `write` guard.
- Both successful incident calls included `mkdir`, which currently makes the whole call `write` with the scratch destination. Classification alone therefore does **not** explain their admission. Present definition selection explains their shape; historical runtime variables/exclusions are unavailable, so the historical rule decision remains an inference.
- Existing managed clone/worktree tools supply the approved mechanism. Memory `a6ed309d-3e34-58fb-9ff9-c41cbc9ef831` records Josh's 2026-11-04 ruling: agents must not create repositories in scratchpads **or agent workspaces**; use `gobby-clones:create_clone` or `gobby-worktrees:create_worktree`. #25652 was closed won't-fix because later restrictions on the experiment were correct. Writable workspace folders remain appropriate for ordinary probe files.

### Evidence & Sources

**Creation trace — validation criterion 1**

Let `S` mean `/private/tmp/claude-501/-Users-josh-Projects-gobby/18b6d896-29ef-483f-86f3-110271af4374/scratchpad`. The original calls used literal absolute paths; this abbreviation only shortens the report.

1. **`S/cherrytest/repo3` came from direct Bash.** Parent transcript `/Users/josh/.claude/projects/-Users-josh-Projects-gobby/18b6d896-29ef-483f-86f3-110271af4374.jsonl`, source line 3544, tool ID `toolu_01Ez6nSChr82CEZSDGCCoQQT`:

   ```sh
   mkdir -p S/cherrytest/repo3 && git -C S/cherrytest/repo3 init -q -b base && echo ok
   ```

   Result: `ok`, `is_error: false`. Retrieved through `gobby-sessions:get_session_messages(session_id="gobby#19401", offset=180, limit=14, full_content=false)`, followed by `gobby-results:get_tool_result`, result `21ef49c5-2ce4-4c43-851c-9460d7c61e22`, offset 154500. Its rendered block preserves the original source line and paired result.

2. **The script hypothesis is disproved for `repo3`.** A structured `Write` successfully created `S/cherrytest/run.sh` at parent line 3517 (`toolu_015yYGaTsRg3xpqJdeFMWXkF`). Its `bash .../run.sh .../repo` invocation at line 3521 was blocked by `block-unresolved-scope-shell-write`. Rewritten script invocations at lines 3537 and 3540 were also blocked before execution, including `chmod +x ... && .../run.sh`. The direct `mkdir && git init` call then succeeded. The subsequent config/write/commit chain (`toolu_01F4Gbma4835sHHmzsNkni5s`) was refused by code-review enforcement; the parent later described an empty initialized repo. Script-file writes demonstrate the selection gap, but the script did not create `repo3`.

3. **`S/cpx-probe-tmp` came from a workflow mechanics-lens subagent's direct Bash.** Subagent transcript `/Users/josh/.claude/projects/-Users-josh-Projects-gobby/18b6d896-29ef-483f-86f3-110271af4374/subagents/workflows/wf_11ed3ba1-ae0/agent-ab51b8855278399e1.jsonl`, line 1901, timestamp `2026-11-04T10:34:14.524Z` (04:34 CT), tool ID `toolu_01AvvofUKnzKqqcGWFMSH4Ab`:

   ```sh
   mkdir S/cpx-probe-tmp && git -C S/cpx-probe-tmp init -q -b base && printf ... | git -C S/cpx-probe-tmp fast-import --quiet && git -C S/cpx-probe-tmp log --all --oneline && git --version
   ```

   The Orchestrator's readback abbreviates the `printf` payload; this is not claimed as a verbatim full command. The creation primitive, destination and tool ID are exact. Line 1903 reports successful execution: `31324d6 P change`, `b16198e A fix`, `ea4db7e base`, `git version 2.54.0 (Apple Git-157)`. The checkout/cherry-pick call at line 1907 (`toolu_01PjVEoWRvy2dAtXK6v3qs6x`) was blocked by `require-code-review-skill`. Removal at line 1912 (`toolu_01TgEymMKb7EpZonaHHP4WjT`, `rm -r -f S/cpx-probe-tmp && echo cleaned`) was blocked by `no-recursive-rm-interactive`. This repository contained imported commits and remained after cleanup was refused.

   Provenance: Orchestrator gobby#19309 read the primary subagent transcript and sent extract message `b2044a02-43c6-4302-afd4-71cd21304f1c`. Its search covered all this session's subagent transcripts and found these as the only calls operating on that repo. My direct transcript filesystem read was denied by SRT; I did not bypass it. Parent transcript corroboration identifies the mechanics lens and leftover `.git` (MCP result `cc2533d0-7c33-48b8-8561-1889ae27f0ff`, original lines 4279–4292 and handoff notes).

**Installed state and canonical semantics — validation criterion 2**

- Orchestrator installed-row readback `3391aa08-5a1e-4448-b72e-7a6d1f524d18`: temporary-write rule `enabled=true`, `enabled_pinned=true`, source `installed`, global scope, tags `[roles, runbook, enforcement, gobby]`, no stored version. Its condition is:

  ```python
  event.data.get('canonical_tool_kind') == 'write'
  and temp_scratchpad_write(event.data, tool_input)
  and not write_paths_within(event.data, tool_input, temp_write_admitted_dirs(variables))
  ```

  Bundled equivalent: `src/gobby/install/shared/workflows/rules/roles/block-temp-scratchpad-writes.yaml:1–26`, version `2.0.0`. Its update timestamp followed the operator-sanctioned deletion toggle and does not establish earlier enablement.
- Confirmation `24cff20d-8618-42b3-800e-43eeb0c5243a`: installed `default` version `1.3.0` includes `tag:default`, `tag:task-closure-general`, `tag:task-closure-coding`, `tag:task-security`, `tag:plan-directive-policy`, and two unrelated named rules. None matches this rule, which has no `mandatory` key. The session currently has `_agent_type=default`; historical values are unavailable. Cleanup and review rules have `default` tags, consistent with their observed blocks. Bundled selectors: `src/gobby/install/shared/workflows/agents/default.yaml:119–130`.
- `src/gobby/workflows/selectors.py`, functions `resolve_rules_for_agent`, `rule_matches_agent`, `_rule_excluded_by_agent`: mandatory rules apply regardless of selection/exclusion; otherwise explicit rules and selector matches govern activation. `src/gobby/workflows/definitions.py:539` declares `mandatory: bool = False` and documents universal applicability. This existing YAML mechanism avoids per-definition patches.
- `src/gobby/workflows/condition_helpers_paths.py:290–325`: `temp_scratchpad_write` checks resolved destinations against `/tmp`, `/private/tmp`, the live tempdir and `/private/var/folders`; unresolved `$TMPDIR` only matches with `canonical_repo_mutation_scope_unknown=true`. `_temp_write_targets` prefers `canonical_write_file_paths`, otherwise `canonical_file_paths`. Missing destinations yield no positive match.
- Same file, lines 27–69 and 98–112: `temp_write_admitted_dirs` combines own/epic workspace folders and the seat's SRT socket root. `write_paths_within` admits a nonempty set only when every resolved path lies in those roots. The socket-root exception admits directory writes generally, not only socket creation.
- Same file, lines 130–286: `_workspace_write_refused` rejects `.git` components; `_tree_workspace_write_refused` adds bulk copy/extraction checks. `agent_workspace_write_violation` checks structured writes, canonical `write`, explicit commits and unresolved execution paths. An `execute` Git creation with no paths bypasses its ordinary write-path checks.

| Bypass class | Evidence and consequence | YAML or code? |
| --- | --- | --- |
| Definition-selection omission | Installed tags/selectors mismatch; observed structured `Write` and `mkdir` succeed. Applies to supported writers generally. | YAML: existing `mandatory: true` matches the all-agent contract. Adding `default` alone leaves other nonmatching definitions uncovered. |
| Standalone Git creation | `_normalization_git.py:427–651` returns `None` for `init`, `clone`, `worktree`; fallback `classify_search_segment` at lines 52–61 returns `execute` for non-`grep` Git. `_normalization_base_commands.py:391–405` adds `canonical_git_mutation` without destination paths. | Code must provide operation/destination facts. Accepting `execute` in YAML cannot recover absent targets. A literal regex is partial mitigation. |
| Cwd, option and destination variants | Same missing operation branch covers bare/mirror clones, implicit clone targets, cwd init, `git -C`, alternate `--git-dir`/`--work-tree`, `--separate-git-dir`, and worktree destinations. `git_command_parts` is at `_normalization_git.py:148–195`. These variants were source-reviewed; not all were runtime-probed. | Code: parse creation operations and resolve both worktree and Git metadata targets; unknown scope must remain unknown. Policy belongs in YAML. |
| Mixed command hides another writer | `_normalization_metadata.py`, `_merge_shell_segment_metadata`, merges known paths and chooses `write` when any segment writes. The incident's path comes from `mkdir`; an admitted sibling write beside unclassified Git creation can expose only the admitted path. | Code: preserve each creation segment's destinations/unknown-scope evidence across merging. |
| Workspace/socket-root exceptions | Temp rules intentionally admit these writable roots, but the separate operator repository ban still applies there. | Separate creation-operation YAML policy, backed by code facts. Preserve legitimate file/socket access. |
| Shell script and inline shell | `_normalization_canonical.py:823–995` recursively classifies readable shell bodies. Unreadable/over-budget script bodies and dynamic programs can remain `execute` with script-execution facts; known bodies inherit missing Git facts. The incident's script runs were blocked. | Code fixes known bodies transitively. Opaque execution needs existing conservative admission/runtime confinement, not a script-path allowlist. |
| Interpreters, indirect subprocesses, binaries | `_normalization_base_commands.py` inline-interpreter branch permits indeterminate Python as low-confidence `execute`; Node/Ruby mutation can lack destinations. `_normalization_file_commands.py` defaults unknown programs to `execute`. A subprocess/library can create metadata without visible Git words. | No general YAML-only proof. Stronger guarantees inside writable roots require an explicit runtime boundary. This is a residual class, not the observed cause. |
| Copy/extract/manual metadata | Selected temp rules cover recognized writes to forbidden roots; workspace `.git` and tree-write checks already cover recognized seat-folder writes. Bare repositories need not use a `.git` directory. | YAML selection closes recognized temp writes. Retain existing metadata/tree checks; arbitrary opaque assembly remains a runtime limitation. |
| Provider/wrapper propagation | Canonical facts must survive nested shells, subagents and provider-native execution cells, rather than matching only the displayed `Bash` name. | YAML applicability plus code/integration tests. No incident on another provider was established. |

Additional source: `_normalization_interpreter_segments.py`, `_classify_stdin_python`, applies interpreter evidence to heredocs. Sources were read through `gcode symbol-at`, `outline` and scoped searches. Hash-verified `gcode evidence --request-json` was denied by shell admission even with a literal absolute workdir; the Orchestrator approved source-location fallback in message `b2044a02`. No hashes are invented.

**Recommendation and follow-up — validation criteria 3–4**

- **Use the existing universal rule mechanism.** The Orchestrator should make the temp-write rule mandatory, retain legitimate admitted directories, bump its bundled version for expanded applicability, and verify the installed row. This closes the observed `mkdir`/structured-write shapes when their targets are forbidden. #25662's description routes YAML-only changes to the Orchestrator's live-fix path.
- **Extend the existing Git classifier, then declare a creation ban.** Recognize `init`, `clone`, `worktree add`; emit a creation-operation fact, every resolved worktree/metadata destination, and unknown-scope evidence when resolution fails. Preserve them across compound/script/provider normalization. A mandatory YAML rule should refuse seat-issued shell repository creation everywhere, including workspace/socket roots; managed MCP creation stays sanctioned. Keep bundled definition/rule names out of Python/Rust. Reuse the existing parser and selector engine.
- **Keep the scope protections.** Do not disable review, unresolved-scope or cleanup protections for probes. Keep workspace metadata/tree checks. The classifier repair must not be advertised as proof against arbitrary programs assembling bare repositories inside writable directories.
- **Approved probe route:** use source/manpages/read-only commands first. When a real repository is necessary, use the existing Gobby managed clone/worktree tools under their managed roots and the seat's authority. An agent-workspace repository is **not** allowed by the recorded ruling. A researcher lacking provisioning capability requests it through Triage. Provisioning does not authorize writing other sessions' resources.
- **Proposed code task under #25662:** “Classify repository creation and enforce managed-only Git probes.” Scope: canonical creation operation/destinations; mandatory creation policy and versioned guidance; focused regressions for standalone/mixed calls, default cwd/destinations, alternate Git dirs, bare/mirror forms, option ordering, wrappers, readable scripts, subagents, provider execution cells, workspace/socket roots, unknown scopes, and sanctioned managed-tool success. Preserve ordinary Git operations in assigned managed checkouts and existing copy/metadata restrictions. Document opaque execution explicitly. This is a proposal; this research task creates no implementation or follow-up task.

**Checks and bounded criteria review**

- Read #25689 with `brief=false` and #25662's routing contract. Criterion 1: exact creation calls/tool IDs/transcript locations above. Criterion 2: bypass matrix and YAML/code ownership above. Criterion 3: concrete fix and managed probe route above. Criterion 4: proposed #25662 follow-up, with no source changes.
- Ran an inert `uv run python -c` classifier probe importing `_normalize_shell_tool_metadata`, cwd/base `/Users/josh/Projects/gobby`. Command strings were data, never executed. Standalone `git init /tmp/research-never-created`, `git clone --no-checkout /Users/josh/Projects/gobby /tmp/research-never-created`, `git -C /Users/josh/Projects/gobby worktree add --detach /tmp/research-never-created HEAD`, and `git -C /tmp/research-never-created fast-import --quiet` all returned `canonical_tool_kind='execute'`, `canonical_git_mutation=True`, and no canonical file/write paths. `mkdir -p /tmp/research-never-created && git -C /tmp/research-never-created init -q -b base` returned `kind='write'` and that canonical path. Exit 0. No repo or network operation occurred.
- Additional variant probes were blocked before execution and provide no runtime evidence. No pytest run was appropriate for a report-only change. Validation consists of transcript/source review, inert classification evidence, criteria coverage and report whitespace validation.
- Denials reported to Triage: bootstrap message before research-skill loading; transcript SRT `PermissionError: Operation not permitted`; serve-step `query_readonly` refusal (abandoned); code-index teaching gate (loaded); literal-workdir admission failures including evidence requests. An inert variant probe containing a foreign-workspace string was refused by workspace-read scope. Report writing initially required plan-overview loading. No denied operation was bypassed. The Orchestrator tracks the read gap as #25691.

### Risks

- Present installed state cannot reconstruct historical `_agent_type`, exclusions, rule enablement or SRT grants. No historical exemption is asserted; the current selection mismatch and correctly selected cleanup/review blocks give a consistent explanation.
- The authorized Orchestrator supplied the primary cpx transcript extract. Its fast-import payload was abbreviated; the report marks that abbreviation and preserves exact creation identity/location/results.
- Universal applicability expands effective enforcement. Verify synchronized DB rows and focused provider/subagent behavior; legitimate workspace and socket writes must remain usable.
- Static classification cannot identify arbitrary programs' future writes or every bare-repository layout. `src/gobby/agents/srt_settings.py:49–115` maps path grants/denials into SRT; it does not establish a content-based repository prohibition inside arbitrary writable roots. A stronger adversarial guarantee needs a specified execution/filesystem boundary, rather than more keyword rules.
