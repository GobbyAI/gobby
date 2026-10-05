# Security boundaries beyond managed-launch shell detection (2026-10-05)

Research record for #22103 (Review security boundaries beyond managed-launch shell
detection), under #22949 (Lane 7 - Planning/research). It is a threat model with
decision options. It proposes no patch and adds no control.

## Scope and method

Governing scope:

- the #22103 task text and validation criteria;
- `.gobby/plans/completed/minimal-sandbox-fixes.md` (F1 at lines 7-12, the #22103
  hand-off at line 193);
- `docs/guides/sandboxing.md:78-84`, which states that the launch guard "does not
  replace the managed OS sandbox or provide a hostile-process security boundary".

Two attacker models stay separate throughout:

- **Model A, inside SRT.** A managed terminal agent, or a web-chat tool process,
  including one steered by prompt injection. It runs under the sandbox-runtime
  policy that Gobby renders. Nothing here claims that SRT fails to enforce its
  own policy; every Model A finding concerns what that policy grants.
- **Model B, same-user host process.** An interactive CLI session, anything it
  launches, or any other process running as the user. It has no OS sandbox.

Session classes:

| Class | OS sandbox | Gobby controls |
| --- | --- | --- |
| Interactive CLI | none | hooks and rules |
| Managed terminal agent | SRT over the whole provider process tree | hooks, rules, signed grant, run-scoped API token, agent definition |
| Web chat | SRT by default, network off (`web_chat_sandbox_config`, `daemon_owned_sandbox_config` at `src/gobby/agents/sandbox.py:100-113`, hash `39c7f28771b2`) | hooks, rules |

Evidence labels:

- **stub**: a probe below calls in-repo functions on synthetic input;
- **static**: a source or template read, cited with a `gcode evidence` excerpt
  hash (first 12 hex digits) or, for unindexed files, `path:lines`;
- **live**: observed at runtime.

This spike made no live observation. The one live datum it cites comes from
`.gobby/plans/research/srt-violation-false-positives-2026-10-01.md`. No probe
reads a secret or calls the daemon. One subprocess runs: for each provider,
`probe_credential_paths.py` reaches `_git_metadata_write_paths`, which runs a
read-only `git rev-parse --git-dir --git-common-dir` in the probe's temporary
workspace (`src/gobby/agents/sandbox.py:371-389`, hash `e3f89142190c`). That workspace is not a
repository, so the call adds no paths. Evidence hashes were
taken at 7f681fe8ac.

Rule claims describe bundled templates. Installed DB rows are authoritative, and
this spike did not read them, because that needs a daemon call.

#23519 (Shared-token cutover: gdaemon validates keys and Python trusts only the
front door) is in flight. It moves API-key validation to the gdaemon front door.
Scenario 5's route analysis of `auth_service.py` should be rechecked after it
lands.

### Probes

All four run from the repository root with `uv run python <path>`. Each finishes
in a few seconds.

| Probe | Scenarios |
| --- | --- |
| `.gobby/plans/research/security-boundaries-22103/probe_process_evasion.py` | 1, 3 |
| `.gobby/plans/research/security-boundaries-22103/probe_credential_paths.py` | 2, 3, 5 |
| `.gobby/plans/research/security-boundaries-22103/probe_ui_automation.py` | 3 |
| `.gobby/plans/research/security-boundaries-22103/probe_control_plane.py` | 4, 5 |

`probe_credential_paths.py` classifies each path using the precedence that
sandbox-runtime documents and `sandbox_policy.py` restates: reads outside
`denyRead` are allowed, `allowRead` wins over `denyRead`, and `denyWrite` wins
over `allowWrite`. It computes verdicts from names only and opens no candidate
file.

## 1. Process evasion

**Threat.** An agent starts a provider CLI (codex, claude, droid, grok, qwen or
agy) outside `gobby-agents:spawn_agent`. The run escapes managed lifecycle,
attribution and agent-run controls. In an interactive session it also runs with
no OS sandbox.

**Probe (stub).** `probe_process_evasion.py` passes 12 commands through the rule
predicate `blocks_direct_provider_launch`. The `Write` and browser rows pass a
`command` key, but the predicate rejects non-shell tool names before it reads the
input, so the input shape does not affect them. Output:

```text
BLOCK  Bash                              literal launch
BLOCK  Bash                              nested shell
BLOCK  Bash                              env wrapper
allow  Bash                              variable indirection
allow  Bash                              eval of built text
allow  Bash                              renamed copy
allow  Bash                              script file
allow  Bash                              interpreter subprocess
allow  Bash                              tmux keystrokes
allow  Bash                              AppleScript terminal
allow  Write                             non-shell tool
allow  mcp__claude-in-chrome__computer   browser automation
```

**Current protection (static).**

- The bundled `block-direct-provider-launch` template sets priority 10, selectors
  default and worker-safety, and no bypass.
- Its predicate covers shell tools and literal syntax only
  (`src/gobby/hooks/provider_launch_guard.py:122-128`, hash `27a42043a197`).
- Under SRT, descendants of the sandboxed provider run under the same Seatbelt
  profile. That is sandbox-runtime and macOS platform design, which this spike
  did not verify in Gobby. `render_srt_settings` sets
  `enableWeakerNestedSandbox: False` (`src/gobby/agents/srt_runtime.py:513-540`,
  hash `17c1b972a7e2`).

**Limit.** Nine of the twelve cases pass. The guide already documents this
(`docs/guides/sandboxing.md:78-84`).

**Impact.**

- Interactive (Model B): an untracked provider runs with the user's full rights.
- Managed (Model A): an evaded provider stays inside the same SRT policy and gains
  no new filesystem or network rights. It escapes Gobby attribution, transcript
  capture and agent-run limits.

**Decision options.**

1. Keep the guard as hygiene and document interactive sessions as an
   operator-trust surface (status quo).
2. Run Gobby-launched interactive sessions under SRT too. That confines those
   process trees at the cost of interactive friction. Same-user host processes
   that Gobby did not launch keep full user authority.
3. Enforce by executable identity, for example an SRT exec deny on provider
   binaries inside managed runs. It does not help Model B.

**Recommendation.** Option 1. SRT already bounds the managed case. Whether
interactive sessions stay operator-trusted is a product decision for Josh
(decision D1 below).

## 2. Same-user credential access

**Threat.**

- Model A: a managed agent reads user credentials and moves them out through
  model context sent to the provider API, an allowed domain, or a loopback
  service.
- Model B: a host process reads anything the user can read, including
  `~/.gobby/bootstrap.yaml`, which holds the hub `database_url`.

**Probe (stub).** `probe_credential_paths.py` computes the managed policy for a
temporary workspace with `compute_sandbox_paths` and `render_srt_settings`.
Output:

```text
== provider=claude
allowAppleEvents=False allowLocalBinding=True allowUnixSockets=[] allowedDomains=4
path                                     read   write
~/.ssh/id_ed25519                        ALLOW  deny
~/.aws/credentials                       ALLOW  deny
~/.config/gh/hosts.yml                   ALLOW  deny
~/.netrc                                 ALLOW  deny
~/.docker/config.json                    ALLOW  deny
~/.cargo/credentials.toml                deny   deny
~/.gobby/bootstrap.yaml                  deny   deny
~/.gobby/local_cli_token                 deny   deny
~/.claude/settings.json                  ALLOW  ALLOW
~/.claude.json                           ALLOW  deny
~/.codex/auth.json                       ALLOW  deny
~/.codex/config.toml                     ALLOW  deny
~/.codex/hooks.json                      ALLOW  deny
~/Library/Keychains/login.keychain-db    ALLOW  deny
== provider=codex
allowAppleEvents=False allowLocalBinding=True allowUnixSockets=[] allowedDomains=6
path                                     read   write
~/.ssh/id_ed25519                        ALLOW  deny
~/.aws/credentials                       ALLOW  deny
~/.config/gh/hosts.yml                   ALLOW  deny
~/.netrc                                 ALLOW  deny
~/.docker/config.json                    ALLOW  deny
~/.cargo/credentials.toml                deny   deny
~/.gobby/bootstrap.yaml                  deny   deny
~/.gobby/local_cli_token                 deny   deny
~/.claude/settings.json                  ALLOW  deny
~/.claude.json                           ALLOW  deny
~/.codex/auth.json                       ALLOW  ALLOW
~/.codex/config.toml                     ALLOW  ALLOW
~/.codex/hooks.json                      ALLOW  ALLOW
~/Library/Keychains/login.keychain-db    ALLOW  deny
== web chat default: enabled=True backend=srt allow_network=False extra_deny_read=[]
== no-secret-read: when="variables.get('is_spawned_agent')" tools=['Bash']
BLOCK  cat ~/.ssh/id_ed25519
allow  cat ~/.s''sh/id_ed25519
allow  cd ~ && cat ".ss"h/id_ed25519
allow  python3 -c "print(open('/Users/x/.'+'ssh/id_ed25519').read())"
allow  cat ~/.docker/config.json
```

**Current protection (static).**

- `denyRead` holds the Gobby credential roots and gcode-runtime
  (`src/gobby/agents/sandbox_policy.py:281-289`, hash `db1910a06a13`;
  assembly in `src/gobby/agents/sandbox.py:329-343`, hash `4fabd149e783`),
  plus the toolchain credential files.
- `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube` and `~/.config/gcloud` are in
  `sensitive_write_roots` only (`sandbox_policy.py:312-332`, hash `4de7f7889865`).
- Reads outside `denyRead` are default-allowed. `gobby_read_exceptions` states
  "Gobby home stays readable apart from sensitive_roots()"
  (`sandbox_policy.py:335-341`, hash `0902372d7a74`). Memory b79d6e48 records the
  precedence.
- Egress is `strictAllowlist` with 4 domains for claude and 6 for codex in this
  environment, plus loopback through `allowLocalBinding` (`srt_runtime.py:513-540`).
- The bundled `no-secret-read` rule is a Bash regex for spawned agents only
  (`src/gobby/install/shared/workflows/rules/worker-safety/no-data-exfiltration.yaml:48-58`,
  hash `d98ed9c4abd4`). Its header calls it defense-in-depth.
- The managed Postgres role writes only `code_*` tables and three
  `project_checkouts` columns (`crates/gcore/assets/schema/baseline.sql:6182-6234`,
  `:6350-6354`).

**Live.** The 2026-10-01 violation sample shows a denied `file-read-metadata
~/.gobby/bootstrap.yaml`, so `denyRead` on a Gobby root held at runtime. No live
read of `~/.ssh` under SRT was observed. The ALLOW verdicts rest on the policy
model above.

**Limit.**

- Model A can read SSH keys, cloud and gh credentials, `~/.netrc`, Docker
  credentials and other providers' auth files. A claude run can read
  `~/.codex/auth.json`.
- The regex blocks one spelling. Quoting, interpreters, and the provider's native
  file-read tool bypass it, because the rule matches Bash only.
- The provider API is always on the egress allowlist, so anything read into
  model context leaves the machine.
- Model B has no protection. `no-secret-read` does not apply to interactive
  sessions (`when: is_spawned_agent`), and they run without SRT.
  `bootstrap.yaml` is mode 0600 and owned by the same uid, so Model B can read
  the hub DSN and reach every Gobby table.

**Impact.**

- Model A: user credentials can be disclosed to the model provider or to an
  allowed domain. SSH and cloud keys reach systems beyond Gobby.
- Model B: full control of the hub database, which includes every rule, task,
  approval token and session.

**Decision options.**

1. Model A: add the user credential stores to `denyRead`, granting only
   required children (for example `~/.ssh/known_hosts`). The stores are the
   `sensitive_write_roots` home entries plus `~/.config/gh`, `~/.netrc` and
   `~/.docker/config.json`. Add other providers' auth roots per provider. This
   follows memory b79d6e48's parent rule. Cost: SSH git and the gh CLI inside
   managed runs need explicit grants.
2. Model A: move to default-deny reads under home with allowlists. It is the
   largest change, and support in the SRT pin is unverified.
3. Model B: treat same-user as outside Gobby's boundary and document it.
   Alternatively, remove the DSN from a user-readable file, which is partly the
   direction of #23519.

**Recommendation.** Option 1 for managed agents and web chat, after a bounded
live confirmation run that shows a managed shell can read a canary file under a
write-only root (decision D2). For Model B, option 3's documented limit.

## 3. UI automation

**Threat.** An agent drives terminals, the desktop or a browser instead of the
filesystem. It can type an approval or a provider launch into another session,
or read another session's screen, including secrets and approval text shown
there.

**Probe (stub and static).**

- `probe_ui_automation.py` lists every bundled rule naming `send_keys` or
  `capture_output`, and searches all templates for raw channels.
- `probe_process_evasion.py` covers the tmux, AppleScript and browser rows.
- `probe_credential_paths.py` prints the managed SRT AppleEvents and socket
  settings.

```text
OPERATOR_TOOLS=['capture_output', 'send_keys']
rule block-cross-project-send-keys: mcp_tools=['gobby-sessions:send_keys'] when="... not send_keys_target_in_scope(...)"
rule block-web-chat-send-keys: mcp_tools=['gobby-sessions:send_keys'] when="event.metadata.get('session_type') == 'web_chat'"
rule templates naming 'capture_output': none
rule templates naming 'tmux send-keys': none
rule templates naming 'osascript': none
rule templates naming 'claude-in-chrome': none
rule templates naming 'computer-use': none
```

**Current protection (static).**

- `send_keys` reaches only the caller's own session, its project and its agent
  tree (`block-cross-project-send-keys.yaml:4-22`, hash `cb5a1dc41d61`).
- Web chat cannot call `send_keys`.
- The tool refuses autonomous-agent callers
  (`src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py:42-72`, hash
  `822be12d423a`).
- Operator-tool enforcement blocks `send_keys` and `capture_output` for spawned
  agents and subagents (`src/gobby/workflows/engine/enforcement_checks.py:434-450`,
  hash `30b6b07e049b`; `src/gobby/workflows/enforcement/blocking.py:84-94`, hash
  `ecee06c96e01`).
- Managed SRT sets `allowAppleEvents=False` and `allowUnixSockets=[]`, so a
  managed process cannot script apps or reach the tmux server socket (stub).

**Limit.**

- `capture_output` takes a session id and authorizes neither the caller nor the
  target (`src/gobby/mcp_proxy/tools/sessions/_terminal.py:736-784`, hash
  `7cab131062b4`). No rule template names it. An interactive caller can read
  any live session's pane or transcript tail, across projects.
- Web chat is blocked from `send_keys`, and no rule template blocks
  `capture_output` for it. Whether a web-chat session can reach the tool depends
  on its tool exposure and caller classification, which this pass did not
  trace.
- Interactive sessions can use raw `tmux send-keys`, `osascript` and browser
  automation without any guard.

**Impact.** An interactive caller can read another project's screen. A
web-chat caller can as well, if the tool reaches it. Model B can inject keystrokes into same-project sessions, including
coordinator seats, and can automate the desktop.

**Decision options.**

1. Scope `capture_output` like `send_keys`: same project or agent tree, and
   blocked for web chat.
2. Accept raw tmux, AppleScript and browser control in interactive sessions as
   operator trust (decision D1).

**Recommendation.** Option 1 as found work: the `send_keys` scope rule exists,
and its read-side twin was left out. Option 2 for the raw channels.

## 4. Approval provenance

**Threat.** An agent produces, forges or replays an approval that a human was
meant to give. This covers pipeline approval gates, external write grants, and
"Josh approved" relayed in messages.

**Probe (stub).** `probe_control_plane.py`:

```text
== agent API-token route matrix
ALLOW  POST /api/mcp/tools/call
deny   PUT /api/rules/block-direct-provider-launch/toggle
deny   PUT /api/rules/bulk-toggle
deny   POST /api/pipelines/approve/tok-stub
== approval provenance
approve_pipeline(executor: ..., token: str, approved_by: str | None = None) -> dict[str, typing.Any]
PipelineExecution.to_dict() carries resume_token: True
```

**Current protection (static).**

- A gate issues a `secrets.token_urlsafe(24)` bearer token
  (`src/gobby/workflows/pipeline/gatekeeper.py:94-150`, hash `1c51bb0c4c09`).
  The token reaches the REST `/run` caller, CLI output, the configured webhook,
  and the `approval_required` event.
- That event goes only to the WebSocket broadcast. Only completed, failed and
  cancelled events reach session dispatch (`src/gobby/runner_broadcasting.py:372-412`,
  hash `aace5ba2f94a`). The WebSocket accepts only the local CLI bearer
  (`src/gobby/servers/auth_service.py:251-252`, hash `cd3d74d1ef9f`).
- The MCP run path swallows `ApprovalRequired` and returns no token
  (`src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py:176-178`, hash
  `37470f080f41`). `get_pipeline_status` omits tokens (`:819-831`, hash
  `d007263b9346`). An MCP-only agent therefore has no route to a token.
- Tokens are single-use. Approval consumes the token and moves the step out of
  `WAITING_APPROVAL` in one guarded `UPDATE`, which also clears
  `approval_token`. A second use fails with "Invalid or already used approval
  token" (`gatekeeper.py:170-185`, hash `8c292d81c08b`;
  `src/gobby/storage/pipeline_steps.py:155-203`, hash `8d4c4731b5a1`). Bounded
  coverage: `TestGetByToken.test_consume_step_approval_is_single_use` and
  `test_consume_step_approval_rejects_non_waiting_step`
  (`tests/storage/test_pipeline_storage.py:754-810`, hash `ef99e04f585c`).
  Replay is closed. The limits below concern a stolen unused token and false
  provenance.
- The REST approve route is outside the agent token matrix (stub above).
- `authorize_write_grant` records an asserting session. Managed children may
  only narrow a recorded grant (`src/gobby/agents/external_write_grants.py:65-106`,
  hash `816122721162`).

**Limit.**

- No approval surface proves a human. Holding the token is the authority, and
  `approved_by` is caller text (`_pipeline_execution.py:576-600`, hash
  `f8ce40f3f25c`).
- Model B can read `approval_token` rows through the hub DSN (scenario 2) and
  approve under any name.
- `PipelineExecution.to_dict` serializes `resume_token`
  (`src/gobby/workflows/pipeline_state.py:109-125`, hash `18d3fcbbfd31`). This
  pass found no call site, so the exposure is latent.
- An external write grant is the asserting session's claim. A root session can
  assert any root with a nonblank reason.
- An `InterSessionMessage` records `from_session` only, with no human-origin
  field (`src/gobby/storage/inter_session_messages.py:58-80`, hash
  `37fc9048e21e`). A relayed approval is the relaying seat's assertion.

**Impact.** Model B, or a compromised coordinator session, can pass gates and
grant external write roots under any recorded name. The audit trail then shows
free text.

**Decision options.**

1. Derive `approved_by` from the authenticated request principal and drop
   `resume_token` from `to_dict`. The token model stays.
2. Require an operator credential, through the web UI or CLI, for gates that
   guard irreversible effects. This establishes authorized operator access and
   stops Model A. It proves no human presence against Model B, which shares the
   user account and its credentials. Presence under Model B needs a channel
   outside that account, such as an OS user-presence prompt or a second device.
3. Document approvals as coordination signals, not security controls.

**Recommendation.** Option 1 now. Josh decides option 2 per gate class
(decision D3).

## 5. Policy tampering

**Threat.** An agent disables or rewrites the controls that constrain it: rule
rows, agent definitions, pipeline gates, or the provider hook configuration that
loads Gobby.

**Probe (stub and static).** The route matrix above, plus:

```text
toggle_rule(def_manager: 'RuleDefinitionManager', name: 'str', enabled: 'bool') -> 'dict[str, Any]'
TASK_MUTATION_TOOLS_BY_SERVER servers=['gobby-tasks', 'gobby-tasks-ops']
backend-developer.step_workflow.steps[implement]: allowed_tools=all blocked_mcp_tools=['gobby-tasks:de_escalate_task', 'gobby-tasks:reopen_task', 'gobby-agents:spawn_agent', 'gobby-agents:kill_agent']
control tools blocked by any bundled agent: []
block-docker-policy-edits: attribution and cross-session commit guards stay active. toggle_rule is an ordinary MCP
block-docker-policy-edits: toggle_rule('block-docker-policy-edits', enabled=false) or gobby rules toggle.
```

Scenario 2's probe also shows `~/.claude/settings.json` writable for a managed
claude run, and `~/.codex/config.toml` and `hooks.json` writable for a managed
codex run.

**Current protection (static).**

- The agent token matrix states "Operator mutation and configuration routes stay
  excluded" and omits the REST rule and approve routes
  (`src/gobby/servers/auth_service.py:81-95`, hash `1322aeae2e42`).
- Agent definitions can block MCP tools per step.
- The managed DB role cannot write rule, task or workflow tables (scenario 2).

**Limit.**

- The same matrix allows `POST /api/mcp/tools/call`, which reaches
  `gobby-workflows:toggle_rule`, `update_rule`, `delete_rule` and
  `approve_pipeline`.
- `toggle_rule` takes no caller identity
  (`src/gobby/mcp_proxy/tools/workflows/_rules.py:185-207`, hash `d42952fab3b0`).
  Its MCP registration adds no principal check
  (`src/gobby/mcp_proxy/tools/workflows/__init__.py:288-295`, hash `d1085d24c8c7`).
- The auth service's effect gate is a takeover drain fence, not caller
  authorization (`src/gobby/servers/lease_fence.py:44-45`, hash `ee693b82cbef`;
  `auth_service.py:375-380`, hash `61566633efe5`).
- No bundled agent definition blocks these tools. The spawned-agent mutation list
  covers only `gobby-tasks` and `gobby-tasks-ops`. The backend-developer
  `implement` step allows all tools and blocks four
  (`backend-developer.yaml:217-222`, hash `ad336f5a47b2`).
- `block-docker-policy-edits` names `toggle_rule` as the accepted escape hatch,
  and its reason text tells the blocked agent how to disable it
  (`block-docker-policy-edits.yaml:1-27`, hash `f31ef9899d17`).
- `provider_write_exceptions` makes the whole provider root writable
  (`sandbox_policy.py:60-73`, hash `48f4f5f4b5f6`; `:441-450`, hash
  `506bb7b31d0f`). That root holds the hook configuration Gobby installs, and
  for Codex also the hook trust state. Codex records a trusted hash per command
  hook. Gobby computes those hashes and writes `hooks.state.<key>.trusted_hash`
  into `config.toml` (`src/gobby/cli/installers/codex.py:239-314`, hash
  `1c9fd41182da`; `:359-393`, hash `b0f11249d3a4`). A managed codex run can
  write both `hooks.json` and that trust state. Claude had no comparable native
  trust record in this pass. Gobby's own `validate_settings.py` is a manual
  validator and does not run at session start
  (`src/gobby/install/shared/hooks/validate_settings.py:1-23`, hash
  `c7e8b1fd1c48`).
- Agent-definition writes already refuse any request principal, so agent API
  tokens cannot modify agent definitions
  (`src/gobby/mcp_proxy/tools/workflows/__init__.py:226-237`, hash
  `9b1bdaeda621`). Rule and pipeline mutations have no such guard.

**Impact (static and stub, not exercised).**

- Unless installed workflow or rule policy blocks it, a Model A agent can
  disable a rule for every session through permitted MCP dispatch. This is a
  static path: no live rule toggle was measured, and installed policy was not
  read. This pass also did not check whether a later template sync restores a
  toggled rule.
- A Model A agent can remove Gobby's hooks, or add a hook command and, for
  Codex, the matching trust state. Whether and when the provider then runs that
  hook on the host, including in unsandboxed interactive sessions, is
  provider-dependent and static here. SRT enforces its policy as written; the
  policy grants a write to configuration that the host executes.

**Decision options.**

1. Make rule and pipeline mutations operator-only. Extend the principal-based
   guard that already protects agent-definition writes to `toggle_rule`,
   `update_rule`, `delete_rule` and `approve_pipeline`. A spawned-agent tool
   list alone would miss agent-token callers that are not spawned agents. This
   matches the matrix's stated intent.
2. Keep `toggle_rule` as the escape hatch, but require operator approval and an
   audit record, and stop naming the escape hatch in block reasons.
3. Protect hook configuration and hook trust state. Either `denyWrite` the
   hook-bearing and trust-bearing files under the writable provider roots
   (`denyWrite` wins), or give each managed run its own provider config home.
   Before choosing, verify which files each provider rewrites at runtime.
4. Add a session-start hook-integrity check. It detects drift but does not
   prevent it.

**Recommendation.** Options 1 and 3 as found work: both close gaps against
intent the code already states. Option 4 is optional hardening.

## Found work for the Orchestrator

These are coverage defects, as opposed to documented limits. Per the task text,
they are routed here for disposition, not patched.

| ID | Defect | Scenario | Severity |
| --- | --- | --- | --- |
| FW1 | A managed provider run can write the hook configuration, and for Codex the hook trust state, that later sessions of the same provider may execute unsandboxed (provider-dependent, static) | 5 | high |
| FW2 | Agent-token callers reach `gobby-workflows` rule and pipeline mutations through `/api/mcp/tools/call`, contrary to the matrix's stated exclusion; the principal guard on agent-definition writes is not applied to them (static, subject to installed policy) | 5 | high |
| FW3 | `capture_output` has no caller or target scope; web-chat reach is untraced | 3 | medium |
| FW4 | `approved_by` is caller text, and `PipelineExecution.to_dict` serializes `resume_token` | 4 | low |

Decisions for Josh:

- **D1.** Are interactive sessions an operator-trust surface (scenarios 1, 2B
  and 3), or should they run under SRT? SRT would confine Gobby-launched
  interactive process trees only. Other same-user processes stay outside every
  Gobby boundary either way.
- **D2.** Should user credential stores join managed `denyRead`, after a bounded
  live canary confirmation (scenario 2A)?
- **D3.** Which pipeline gate classes require operator-credential approval
  (scenario 4)? That covers Model A and assumes an operator-trusted host.
  Presence against Model B needs a channel outside the user account and is a
  separate decision.

## Criteria check

| Area | Probe | Protection and limit | Impact | Decision |
| --- | --- | --- | --- | --- |
| Process evasion | 1 | yes | yes | option 1, D1 |
| Same-user credential access | 2 | yes | yes | option 1 after canary, D2 |
| UI automation | 3 | yes | yes | FW3, D1 |
| Approval provenance | 4 | yes | yes | option 1, D3 |
| Policy tampering | 5 | yes | yes | FW1, FW2 |
