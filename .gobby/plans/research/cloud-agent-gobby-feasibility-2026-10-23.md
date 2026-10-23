## Research Findings

### Research Questions

- Task #24826: assess Claude cloud sessions/routines, Codex cloud tasks, Grok/xAI agents, and comparable computer-use environments against **Q1** binary installation/persistence, **Q2** repository/user hooks, **Q3** stdio/remote MCP, **Q4** outbound/private networking and inbound hosting, **Q5** PostgreSQL, **Q6** secrets/server-hosting limits, and **Q7** node/hub feasibility and Gobby changes.
- Evidence checked 2026-10-23 UTC. Links are official documentation fetched during this session. “Unknown” means unestablished by reviewed evidence, not prohibited. Verdicts and proposed changes are engineering inferences, not passing installation tests.

### Domain Context

- **Recommendation:** keep the authoritative hub on operator-controlled infrastructure. Use provider computers as replaceable nodes or remote MCP clients. Start with remote MCP when only Gobby tools are needed; install a node when hooks, local execution or indexing require one. The provider evidence below supports this approach without requiring a separate cloud-hosted hub.
- Brief-supplied Gobby architecture: Python daemon on `127.0.0.1:60887`, PostgreSQL hub, MCP proxy, and Rust binaries `ghook`, `gdaemon`, `gcode` in `~/.gobby/bin`. Repository operating rules: [AGENTS.md](../../../AGENTS.md). Memory `9bf4bb37-a4c0-5973-bfaa-9aeea770cb21`, fetched in gobby#17363, specifies the intended Rust node with neither Python nor Docker. This is a target, not a claim of current cloud compatibility.
- **Josh's implementation gates:** research now; cloud node installation after the **hub-node milestone**; cloud hub installation **after 1.0**. Source: Orchestrator gobby#17281's clarification delivered to researcher gobby#17363 on 2026-10-23. No implementation tasks were created.
- A cloud VM's loopback address is its own machine. Access to the home daemon requires an authenticated network path. Backend-executed MCP needs connectivity from that backend, independently of a VM VPN.
- **Invalid cloud probe:** the Orchestrator's remote-isolation attempt ran in a local Git worktree on Josh's Mac: Darwin arm64, `~/.gobby` present, local daemon health HTTP 200. This provides no hosted-runtime evidence. Source: gobby#17281 messages to gobby#17363 on 2026-10-23. The Assistant reported that Agent `isolation:"remote"` is availability-gated and undocumented; a disabled gate is a hypothesis for the fallback, not a proven cause.
- **Pending first-hand probe:** the documented CLI entry is `claude --cloud "task"`; `--remote` is deprecated. It dispatches from the current branch to a cloud session, requiring account/environment/GitHub setup. Do not equate it with Agent isolation. A real account-backed probe needs Josh's go through the Assistant; none was launched. Codex and Grok remained docs-only, per Orchestrator direction. [Claude cloud dispatch](https://code.claude.com/docs/en/claude-code-on-the-web#from-terminal-to-cloud).

### Evidence & Sources

**Claude Code: Anthropic-hosted sessions**

1. **Q1/Q5:** setup runs as root on Ubuntu 24.04 x86_64; PostgreSQL 16 is preinstalled. Setup snapshots preserve installed files, not processes. Idle VMs pause and may be reclaimed. Linux Gobby binaries and session-local PostgreSQL are feasible; continuous uptime is unestablished. [Setup and caching](https://code.claude.com/docs/en/cloud-environments#setup-scripts).
2. **Q2:** single-repository sessions load `.claude/settings.json`; multi-repository sessions and project cloud threads skip repository hooks. Local user settings do not transfer. Self-hosted runners can seed user hooks. [Hook scope](https://code.claude.com/docs/en/cloud-environments#limitations-in-cloud-sessions).
3. **Q3:** single-repository sessions load committed `.mcp.json`. Claude supports stdio, HTTP and legacy SSE; HTTP is recommended. Remote Gobby is a plausible untested integration. [Cloud configuration](https://code.claude.com/docs/en/cloud-environments#what-carries-over-from-your-setup), [MCP transports](https://code.claude.com/docs/en/mcp#installing-mcp-servers).
4. **Q4:** None/Trusted/Full/Custom govern VM egress through an HTTP/HTTPS proxy. Connectors use Anthropic's backend. Public HTTPS hub access is plausible; home VPN, arbitrary TCP, inbound ports and reverse tunnels remain unknown. [Networking](https://code.claude.com/docs/en/cloud-environments#network-access).
5. **Q6:** commands can read ordinary environment variables; network secrets are substituted outside the VM. General unattended server-hosting entitlement is unknown. [Secrets](https://code.claude.com/docs/en/cloud-environments#add-network-secrets).
6. **Q7:** best documented hosted candidate for an ephemeral hook-enabled node, with the repository exceptions above. Use setup for binaries, per-session startup/reconnection, and an authenticated HTTPS hub channel. Durable hub hosting is unsuitable given pause/reclaim behavior; temporary development operation is plausible.

**Claude scheduled tasks, formerly routines**

1. **Q1–Q6:** a saved prompt/repository/connector configuration runs as cloud sessions on Anthropic or self-hosted infrastructure. The session/environment qualifications above apply. Scheduling establishes recurring execution, not permanent daemon uptime. [Scheduled tasks](https://code.claude.com/docs/en/routines), [Cloud session routing](https://code.claude.com/docs/en/claude-code-on-the-web).
2. **Q7:** disposable node/client per run; fresh run identities and reconnect behavior needed. Confirm the exact repository mode before relying on hooks or `.mcp.json`.

**Claude self-hosted cloud environments**

1. **Q1/Q4/Q5:** Team/Enterprise public beta executes cloud sessions on operator-controlled runners inside the organization's network. Anthropic communication originates outbound. Tools, PostgreSQL, durable disks, supervision and inbound services become operator infrastructure choices. [Self-hosted architecture](https://code.claude.com/docs/en/self-hosted-environments).
2. **Q2/Q3/Q6:** runner-seeded user/managed settings are supported; MCP offers local/remote transports. Git credentials come from the deployment; exact secret storage and persistence depend on its design. General server-hosting entitlement under the account agreement remains unknown. [Runner hooks](https://code.claude.com/docs/en/cloud-environments#setup-scripts-vs-sessionstart-hooks), [MCP](https://code.claude.com/docs/en/mcp), [Self-hosting](https://code.claude.com/docs/en/self-hosted-environments).
3. **Q7:** strongest route to cloud-session UX with full Gobby. Host the hub outside disposable session processes on stable controlled infrastructure.

**OpenAI: current Codex Cloud**

1. **Q1:** install scripts, start skills and published filesystems prepare tasks. Existing tasks retain their own installed tools/files; new tasks have separate workspaces. Saved VM state is recoverable up to seven days by default. Process survival through suspension is unknown. [Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments).
2. **Q2:** general Codex docs describe user/repository `hooks.json` and TOML hooks with trust requirements. They exclude command/local/plugin hooks under cloud orchestration and describe limited enterprise remote MCP hooks for Work Cloud with local access. Normal Codex Cloud execution of checked-in CLI hooks is **unknown**; local Codex support proves no hosted parity. [Hooks](https://learn.chatgpt.com/docs/hooks).
3. **Q3:** stdio/Streamable HTTP are documented for Codex hosts; hosted ChatGPT uses plugins. Loading arbitrary repo/user MCP config in current Codex Cloud is **unknown**, as is SSE there. A supported plugin/bridge to remote Gobby needs confirmation. [MCP surface boundaries](https://learn.chatgpt.com/docs/extend/mcp).
4. **Q4:** allowlisted internet and Tailscale VPN access to private HTTP/HTTPS services are documented. Home/public hub access is plausible; inbound hosting and arbitrary TCP are unknown. [Private networking](https://learn.chatgpt.com/docs/environments/cloud-environments#private-networking-vpn).
5. **Q5/Q6:** PostgreSQL installation is plausible from service setup, untested. Raw environment values and proxy-substituted HTTPS:443 network secrets are supported. General server-hosting entitlement is unknown. [Setup/secrets](https://learn.chatgpt.com/docs/environments/cloud-environments).
6. **Q7:** task-local node process candidate; hook/MCP integration unproven. Use install/start bootstrap and HTTPS bridge pending verification. Recoverable snapshots do not justify durable hub hosting.

**OpenAI: Codex Cloud Legacy**

1. **Q1/Q4/Q6:** setup scripts install packages; cached container state lasts up to 12 hours. Setup has internet; agent internet defaults off. Outbound traffic uses an HTTP/HTTPS proxy. Encrypted secrets are setup-only and removed before the agent phase; ordinary variables remain. [Legacy environment contract](https://learn.chatgpt.com/docs/environments/cloud-environment).
2. **Q2/Q3:** hosted CLI hooks and arbitrary stdio/HTTP/SSE MCP configuration remain **unknown** in reviewed cloud/general references. [Hooks](https://learn.chatgpt.com/docs/hooks), [MCP](https://learn.chatgpt.com/docs/extend/mcp).
3. **Q4/Q5/Q6:** inbound ports, tunnels, uninterrupted processes, PostgreSQL hosting and general server entitlement are **unknown**. Temporary PostgreSQL installation is only an inference from package setup. [Legacy setup](https://learn.chatgpt.com/docs/environments/cloud-environment).
4. **Q7:** conditional ephemeral client candidate. Runtime hub authentication cannot depend on setup-only secrets surviving. Prefer current Cloud where suitable; it is explicitly distinct from Legacy. [Current/Legacy distinction](https://learn.chatgpt.com/docs/environments/cloud-environments).

**xAI: Grok Bot persistent computer**

1. **Q1:** a persistent terminal/browser/filesystem is shared by all bots on one user account, including CLI credentials. [Shared computer](https://docs.x.ai/grok-bot/computer-and-apps). Enterprise Team Setup installs software on Debian-based Linux with sudo. Gobby binaries are plausible; daemon survival across sleep/recreation remains untested. [Setup prerequisites](https://docs.x.ai/grok-bot/private-networks#before-you-start).
2. **Q2:** Grok Bot execution of Claude/Codex/Grok Build hooks, and user-configurable equivalent hooks, is **unknown**. Grok Build separately advertises hooks/MCP; that establishes no Bot parity. [Bot overview](https://docs.x.ai/grok-bot/overview), [Grok Build](https://x.ai/build).
3. **Q3:** enterprise docs reference hosted connector calls and stdio servers on the Bot computer; connector OAuth tokens stay server-side. Custom remote Gobby configuration and exact HTTP/SSE support need confirmation. [MCP/tool export](https://docs.x.ai/grok-bot/teams-and-enterprises#conversation-content-export).
4. **Q4:** Enterprise network policy supports domains and raw IP ranges/ports; teams without a policy default to allow-all. [Network policy](https://docs.x.ai/grok-bot/security). Desktop egress routing and installed networking clients reach private services. Inbound ports/tunnels are **unknown**; the documented patterns address outbound access. [Private networks](https://docs.x.ai/grok-bot/private-networks).
5. **Q5:** PostgreSQL is plausible from Linux/sudo setup; package installation, supervision, durability and crash recovery have not been validated. [Installation capability](https://docs.x.ai/grok-bot/private-networks).
6. **Q6:** Enterprise Team Secrets are encrypted and supplied to setup/check script environments only. Persistent credential storage exposes them to the account's other bots. General server-hosting entitlement is unknown. [Team Secrets](https://docs.x.ai/grok-bot/teams-and-enterprises#team-secrets), [Computer boundary](https://docs.x.ai/grok-bot/computer-and-apps).
7. **Q7:** promising persistent process/client node, native enforcement unknown. Hub remains experimental pending uptime, PostgreSQL, ingress and entitlement proof. Enroll per computer/user; associate separate per-bot sessions.

**xAI API agents / Grok Build**

- **Q1/Q2/Q4/Q5/Q6:** the hosted API code tool documents Python execution. Customer-managed persistent VM installation, CLI hooks, inbound services, PostgreSQL and process/secrets lifecycles are **unknown**. [Code execution](https://docs.x.ai/developers/tools/code-execution).
- **Q3/Q7:** API remote MCP can call external servers with configured URLs/tools/authentication. Use it as a Gobby tool client; no in-sandbox node/hub capability is established. [Remote MCP](https://docs.x.ai/developers/tools/remote-mcp).
- Grok Build is a CLI on user-supplied compute with hooks/MCP. Node/hub hosting beside it is the ordinary self-hosted case: persistence, network, PostgreSQL and secrets depend on that machine, not a provider cloud entitlement. [Build](https://x.ai/build).

**Cursor Cloud Agents**

1. **Q1:** isolated Ubuntu VMs support setup, Dockerfiles and startup commands. Disk snapshots expire after 30 days of inactivity; uninterrupted background-process survival is unknown. [Setup](https://cursor.com/docs/cloud-agent/setup), [Retention](https://cursor.com/docs/cloud-agent/security-network#data-retention).
2. **Q2:** repository `.cursor/hooks.json` command hooks run once a writable environment exists. Tool-use, pre-compact and stop events are supported; hosted `sessionStart`/`sessionEnd` are absent. User hooks do not transfer; Enterprise team/managed hooks work. [Cloud hook support](https://cursor.com/docs/hooks#cloud-agent-support).
3. **Q3:** custom HTTP/stdio MCP are supported; SSE and `mcp-remote` are not. HTTP executes on the backend with credentials outside the VM; stdio runs inside it. Remote Gobby HTTP is a direct candidate. [Cloud MCP](https://cursor.com/docs/cloud-agent/capabilities#custom-mcp-servers).
4. **Q4:** unrestricted/allowlisted egress and userspace Tailscale/Cloudflare private-service access are documented. These outbound patterns do not establish inbound hosting. [Networking](https://cursor.com/docs/cloud-agent/security-network#network-access), [Private clients](https://cursor.com/docs/cloud-agent/setup#running-tailscale).
5. **Q5:** PostgreSQL is plausible through configurable Linux setup; durability/availability are unproven. [Environment setup](https://cursor.com/docs/cloud-agent/setup).
6. **Q6:** encrypted runtime secrets are environment variables: output is redacted, terminal users can still read them. Build secrets stay build-only. General persistent server entitlement is unknown. [Secrets](https://cursor.com/docs/cloud-agent/security-network#secret-protection).
7. **Q7:** viable node candidate with a Cursor event adapter and lifecycle bootstrap. Remote MCP is smaller when hooks are unnecessary. Durable hub operation needs a separate persistence/ingress contract.

**Devin hosted sessions**

1. **Q1:** configured Linux VM snapshots supply tools/dependencies. New sessions boot clean copies; changes do not flow back to the base snapshot. Gobby installation is plausible, process persistence across sessions unproven. [Environment lifecycle](https://docs.devin.ai/onboard-devin/environment#how-sessions-work).
2. **Q2:** hosted Claude/Codex/Grok hooks or equivalent configurable lifecycle hooks are **unknown** in reviewed hosted docs. CLI capabilities do not establish hosted parity. [Hosted environment](https://docs.devin.ai/onboard-devin/environment).
3. **Q3:** custom stdio, SSE and Streamable HTTP MCP support environment values, auth headers and OAuth. Remote Gobby is a direct candidate. [MCP configuration](https://docs.devin.ai/work-with-devin/mcp).
4. **Q4:** network profiles restrict hostnames/CIDRs; MCP has a separate access policy. VPN configuration supports private access. General inbound ports/tunnels into a session remain unknown. [Profiles](https://docs.devin.ai/product-guides/security-profiles), [VPN](https://docs.devin.ai/onboard-devin/vpn).
5. **Q5:** PostgreSQL is plausible through setup; lifespan/durability are untested. [Environment configuration](https://docs.devin.ai/onboard-devin/environment).
6. **Q6:** encrypted organization/personal/repository/session secrets are bound to commands as needed; do not assume every shell inherits them. [Secrets](https://docs.devin.ai/product-guides/secrets). Hosted app deployment is separate infrastructure for standalone apps, not proof of Gobby hub hosting support. General server entitlement is unknown. [Deployment scope](https://docs.devin.ai/product-guides/deployment-capabilities).
7. **Q7:** direct MCP client or ephemeral node candidate; full enforcement needs a documented event adapter. Use independent infrastructure for a production hub.

### Risks

- No hosted Gobby binary, hook, MCP, PostgreSQL or ingress endpoint was exercised. Unknown hook coverage prevents claiming full policy enforcement from successful MCP tool access alone.
- Product setup documentation does not establish an entitlement to unattended general-purpose server hosting. Applicable account terms, acceptable use and support stance remain **unknown per provider**. This report makes no categorical permission/prohibition claim.
- Snapshotting can clone enrollment material or stale identities. Proposed integration needs runtime enrollment, scoped/revocable credentials, reconnection and separate machine/run identities. Durable disk does not prove continuous service uptime.
- VM networking, backend MCP reachability and inbound hosting are separate checks. Prefer outbound authenticated HTTPS; verify proxies, streams, reconnect and token renewal. Avoid giving cloud nodes direct hub database access solely to obtain tools.
- Future verification should establish cloud identity first, then binary execution, every required marker hook, MCP, outbound hub authentication, idle/resume and independent-new-run behavior. Hub evaluation additionally needs backup/recovery and authenticated ingress. These are research proposals, not implementation tasks.

**Per-provider verdict table (Q7).** Node installations wait for **hub-node**; hub installations wait until **after 1.0**. These are inferred verdicts from the linked evidence, not verified installs.

| Environment | Node verdict | Hub verdict | Needed Gobby changes / missing proof |
| --- | --- | --- | --- |
| Claude hosted single-repo | Best documented ephemeral hook candidate | Temporary only; unsuitable as durable authority | Linux setup; per-session restart/enrollment; HTTPS proxy support; verify all events |
| Claude multi-repo/project thread | Process/client possible; repo hooks absent | Same hosted limitation | Bootstrap outside repo hooks; managed integration where available; exact MCP scope proof |
| Claude scheduled task/routine | Disposable node/client per run | Scheduling supplies no durable host | Cloud bootstrap; run identities; repository-mode checks |
| Claude self-hosted environment | Viable on controlled compute | Viable on stable operator host | Seed binaries/settings; supervision, durable PostgreSQL/storage and authenticated network |
| Current Codex Cloud | Process candidate; hook/MCP coverage unknown | Not recommended on task VM | Install/start bootstrap; supported tool bridge; prove hosted config/events and resume |
| Codex Cloud Legacy | Conditional ephemeral client | Not recommended | Runtime auth independent of setup secrets; egress policy; extension contract proof |
| Grok Bot | Promising persistent process/client; hooks unknown | Experimental/unknown | Bot event adapter; computer enrollment and bot sessions; uptime/database/ingress/entitlement proof |
| xAI API hosted tools | External MCP client; in-sandbox node unestablished | No established hosting capability | Remote Gobby MCP/auth integration; no binary install assumed |
| Grok Build on supplied compute | Ordinary self-hosted candidate | Ordinary self-hosted candidate | Machine-specific install, supervision and network; no Grok Bot parity assumed |
| Cursor Cloud Agents | Candidate with lifecycle gaps | Not recommended on task VM | Cursor adapter; bootstrap replacing session events; HTTP or stdio MCP |
| Devin hosted sessions | MCP client; ephemeral node plausible | Not recommended on session VM | Blueprint bootstrap; event adapter; command-scoped secrets; reconnect proof |

## Cloud probe 2026-10-23

**First-hand local blocker:** after Josh approved one Claude-only probe through the Assistant and Orchestrator, Gobby's PreToolUse rule `block-direct-provider-launch` denied `claude --cloud` before execution. **No cloud session ran.** Exact diagnostic:

```text
Direct provider launches bypass Gobby-managed sandbox policy, tracking, and lifecycle controls. Use gobby-agents:spawn_agent; for external workspace writes supply extra_write_paths and a nonblank write_paths_reason. Only literal help/version and supported authentication-status commands are allowed here. There is no per-command or session-variable bypass.
```

Source: researcher session `gobby#17363`, blocked tool invocation on 2026-10-23; Orchestrator messages in `gobby#17281` authorize recording the blocker and handing launch back to Josh. This establishes a local Gobby guard, not a Claude cloud limitation. Hosted Claude claims above remain documentation-based with their linked official sources; hardware, hooks, MCP, networking and persistence remain untested first-hand.

Per the Orchestrator, `spawn_agent` has no supported cloud target today; the earlier `isolation=remote` attempt fell back to a local worktree and supplies no cloud evidence.

**Operator copy-paste probe:** run once from the main checkout using Claude Code shell mode (`!`). The approved scope accepts cloning `origin/0.5.0` at `af9d6bad36`; push nothing. Codex and Grok remain docs-only. Exact prompt to submit:

```text
! claude --cloud "Read-only cloud environment feasibility probe for Gobby research #24826. Do not modify repository files, commit, push, open a PR, install packages, run uv sync, run gobby install, or configure Gobby as a hub or node. Do not read credentials, secrets, bootstrap files, or print environment variables. Stop if GitHub App setup, cloud environment configuration, org policy, or login is required; report exactly what is missing and never type into a credential prompt.

Report first-hand observations separately from documentation and assumptions. Record OS and kernel, CPU count and architecture, RAM, disk space and uptime. Establish that this is a cloud host before drawing conclusions. Report which hook events actually fire; check whether repository .claude/settings.json exists and summarize hook event names and command paths only. Check whether ghook resolves and whether ~/.gobby/bin/ghook exists. Do not execute hooks manually or expose settings secrets. List visible MCP server names without credential or configuration contents.

Use bounded unauthenticated HTTP HEAD requests to https://github.com, https://pypi.org, https://crates.io and http://127.0.0.1:60887/health. Record status or exact connection errors. Do not scan private networks or guess home-host addresses. Report whether any already documented home-host route is reachable only if it can be checked without credentials or private configuration; otherwise mark unknown. Remember cloud loopback is the cloud host, not the home daemon.

To test background execution and listening, you may create one nonsecret temporary marker in /tmp and one in HOME, and start one background process and one loopback-only HTTP listener on an unused high port, both with an automatic 120-second timeout. Do not kill other processes. Record marker paths, PID, port, start time and initial local connection result. If documented follow-up messages are supported, describe the supported follow-up mechanism and recheck the same markers, PID and listener after a follow-up; do not claim cross-turn persistence until tested. Report how the session ends or pauses, distinguishing observed behavior from documentation. Finish with a concise evidence table and exact errors. Do not create a PR."
```

**Record from the result:** cloud session URL/ID, repository revision, timestamps, host identity/resources, observed hook events versus configured hooks, MCP names, each network result, listener/PID/marker observations before and after a supported follow-up, and pause/end status. Redact secrets. A prerequisite failure is a valid result; missing observations stay **unknown**. Background survival within one command or turn does not prove survival across a turn, pause, VM reclaim or a new session. No Gobby installation is authorized: node installation remains gated on hub-node and hub installation on completion of 1.0.
