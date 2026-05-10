<p align="center">
    <img src="logo.png" alt="Gobby" width="200" />
    <h3 align="center">Start with a task. Walk away. End with a PR.</h3>
</p>


<p align="center">
  <a href="https://github.com/GobbyAI/gobby"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
  <a href="https://github.com/GobbyAI/gobby/blob/main/LICENSE.md"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://github.com/GobbyAI/gobby/stargazers"><img src="https://img.shields.io/github/stars/GobbyAI/gobby?style=flat" alt="Stars"></a>
  <a href="https://github.com/GobbyAI/gobby/issues"><img src="https://img.shields.io/github/issues/GobbyAI/gobby" alt="Issues"></a>
</p>

---

```bash
gobby build '#1842'
```

That's the loop. Hand Gobby a task, walk away, come back to a PR.

Behind that one command: a plan, an expansion into staged subtasks, isolated
worktrees, dispatched agents, hook-time guardrails, validation, review gates,
and a commit-linked close. If something goes off the rails, Gobby stops and
escalates instead of merging garbage.

**Gobby built Gobby.** 7K+ commits. 15K+ tasks across my projects. Two
paying clients running production systems on it. The 0.4.0 release was assembled
through Gobby's own task, dispatch, review, and documentation flows — the
receipts live in this repo's `.gobby/tasks.jsonl`.

---

## Why this exists

The bottleneck in AI coding stopped being model capability a long time ago. The
bottleneck is babysitting. Agents lose context across compactions. They drift
from the rules you wrote in your CLAUDE.md. They duplicate work. They burn
thousands of tokens reloading the same MCP schemas every turn. You still review
every diff because you can't actually trust what comes back.

The fix isn't a better prompt. The fix is infrastructure around the agent.

Gobby is a local daemon that sits underneath the AI coding CLIs you already use
— Claude Code, Codex, Gemini CLI, Qwen CLI, Factory Droid — and gives them what
they're missing: shared sessions, a durable task ledger, hook-time rules,
progressive MCP discovery, agent isolation, review gates, and a build loop that
turns a task into a PR without you in the middle.

It is **not another agent.** It is the control plane the agents you already
have are missing.

---

## What Gobby is

A Python 3.13+ daemon you run locally. SQLite at `~/.gobby/gobby-hub.db`.
HTTP on `:60887`, WebSocket on `:60888`, web UI on `:60889`, stdio MCP server
that your coding CLIs talk to.

Three things make Gobby load-bearing:

### 1. Stage-manifest dispatch + hook-time rules

Most autonomous agents are one giant prompt loop where the model decides
everything. That's the failure mode you've already lived through.

Gobby splits the runtime in two. Dispatch is **deterministic**: a heartbeat
scans tasks, reads the current stage manifest row (`ideation` → `research` →
`architecture` → `prd` → `planning` → `expansion` → `development` →
`holistic_qa` → `pr` → `merge`), evaluates ordered rules in
`src/gobby/dispatch/rules.py`, acquires a per-task mutex, and executes one
bounded action — start a stage, spawn an agent, create isolation, advance,
escalate. No prompting, no model freelancing.

Inside a spawned worker, the agent gets full **autonomy** to plan, edit,
verify, and commit. But every tool call passes through the rule engine on
`turn_start`, `before_tool`, `after_tool`, and `turn_end`. Rules can block,
rewrite, inject context, or set variables synchronously. They are evaluated as
code, not hoped for in a prompt.

Agent freedom inside enforced boundaries. That's the only way `gobby build`
gets to "hands-off" without lying about it.

### 2. Local-first, built with itself

Your database, transcripts, hooks, task ledger, workflows, and rules stay on
your machine. No cloud control plane. No SaaS dependency. Apache 2.0.

The repo you're reading was built through its own build loop. 5K+ commits.
15K+ tasks. 0.4.0 was assembled by spawned agents working through staged
manifests, with the dispatcher routing review and merge. That's the production
test bed: every regression in dispatch, hooks, isolation, or task lifecycle
shows up as a stalled build the next morning.

I've also used it to ship production systems for two paying clients. It is the
tool I needed to actually trust the output of an AI coding agent on real work.

### 3. Sits under your CLIs, not next to them

Aider, Cline, OpenHands, Plandex, BMAD-METHOD — these *are* the agent. They
own the CLI, the loop, the context window. Switching means re-learning a
workflow.

Parallel runners like Superset, parallel-code, and claude_code_bridge launch
multiple CLIs side-by-side in worktrees, but each one is still its own island
with its own session, memory, and task state.

Gobby is the layer underneath. The same daemon, the same task ledger, the same
memory, and the same rule engine serve every CLI you use. A task started in
Claude Code can be claimed in Codex and finished in Droid without losing
context, validation gates, or review state. You don't pick a winner among
coding CLIs; you pick what's best for the work in front of you and Gobby keeps
them coherent.

---

## How `gobby build` actually works

```bash
gobby build '#1842'                          # plan, epic, or leaf task
gobby build plans/auth-refactor.md --quick   # straight from a plan file
gobby build stop '#1842'                     # task-scoped controls
```

Under the hood:

1. **Build state** is written onto the task: `allow_automation=true`,
   isolation (`none` / `worktree` / `clone`), assigned agent, target branch.
   Backlog tasks are inert until this gate is opened.
2. **Stage manifest** materializes from the registry into `task_stage_states`.
   Each row carries position, state (`ready` / `in_progress` / `needs_review` /
   `review_approved` / `done`), review policy, reviewer, and attempt counters.
3. **Heartbeat** scans opted-in tasks, filters out claimed/leased/escalated/
   dependency-blocked work, reads the current stage row, and lets ordered
   deterministic rules pick exactly one action under a mutex.
4. **Agent runs** in a worktree or full clone. Tool calls pass through the
   rule engine. Skills load on demand. Memory and code-graph results inject
   only when relevant.
5. **Review** is stage-native. Workers `submit_for_review` instead of closing
   directly; the next heartbeat spawns the configured reviewer; approval
   advances the row; rejection retries or escalates.
6. **Close** requires a commit. If you changed files, you commit them — the
   daemon won't let a leaf close with diffs and no SHA.

The dispatcher does not draft plans, repair artifacts inline, or prompt
models. Prompting belongs in spawned agents. Routing belongs to dispatch.
Keeping that line clean is what makes the whole thing trustable.

---

## The toolchain (sister repo)

Gobby ships with a set of Rust binaries in
[GobbyAI/gobby-cli](https://github.com/GobbyAI/gobby-cli) that solve the
non-glamorous problems agents run into in practice. They install separately,
but Gobby wires them in for you.

| Tool | What it does | Why it matters |
| --- | --- | --- |
| [`gcode`](https://github.com/GobbyAI/gobby-cli) | AST symbol search over 18 languages via tree-sitter + SQLite FTS5; with Qdrant/FalkorDB it adds vector + graph search and Reciprocal Rank Fusion ranking | Agents stop reading whole files. They retrieve by symbol. Cuts 90%+ off file-level loads on large repos. |
| [`gsqz`](https://github.com/GobbyAI/gobby-cli) | Wraps shell commands and compresses output via 28 built-in pipelines (git, cargo, pytest, eslint, ruff, npm, more) | Verbose test/lint/build output collapses before it ever reaches the model. >90% token reduction on noisy commands, ~9ms overhead. |
| [`gloc`](https://github.com/GobbyAI/gobby-cli) | One command to launch Claude Code or Codex against a local LLM (LM Studio, Ollama). Manages model lifecycle, env vars, warmup. | Same Gobby workflows run against local and cloud models without rewriting anything. |
| [`ghook`](https://github.com/GobbyAI/gobby-cli) | Sandbox-tolerant hook dispatcher that spools events to `~/.gobby/hooks/inbox/` *before* posting to the daemon | Hook events survive sandbox FS denials, network blips, and daemon restarts. The drain worker replays them. |

Plus the progressive MCP proxy itself, which only fetches schemas when a tool
is actually called instead of on every list. That's another 30–40K tokens the
average session never has to spend.

These aren't side projects. The token tax is the thing keeping agents from
finishing real work on real codebases, and the toolchain is part of the moat.

---

## How Gobby compares

| Tool | Category | Where Gobby differs |
| --- | --- | --- |
| **Claude Code, Codex, Gemini CLI, Qwen CLI, Droid** | First-party AI coding CLIs | Gobby runs *under* them. They become the worker, not the orchestrator. |
| **Aider, Cline, OpenHands, Plandex, Continue** | Coding agents / IDE extensions | They each own the loop. Gobby owns the task, the rules, the dispatch, and the review gates around whichever loop you pick. |
| **BMAD-METHOD** | Multi-agent role framework (Markdown/YAML personas) | Real overlap on staged work, but BMAD is a methodology layered on top of an existing agent; Gobby is the daemon, ledger, hook engine, and dispatcher. |
| **Superset, parallel-code, claude_code_bridge, CLI Agent Orchestrator** | Parallel CLI launchers | They run multiple CLIs side-by-side in worktrees. They don't share session, task, memory, or rules across CLIs. Gobby does. |
| **IBM Context Forge, MintMCP, Composio, Runlayer** | MCP gateways | Cloud/enterprise reverse proxies for MCP. Gobby is local-first, adds progressive discovery, and binds MCP to a task lifecycle and rule engine. |
| **OpenClaw** | Personal AI assistant across messaging channels | Different category — OpenClaw is a personal agent for WhatsApp/Slack/Telegram-style use. Gobby is dev infra for agents that ship code. |
| **Devin, OpenHands Cloud** | Hosted autonomous SWE | Cloud-only, opinionated stack, your code on their servers. Gobby runs on your laptop, talks to whichever model and CLI you trust, and is Apache 2.0. |

The honest summary: if you've already picked a coding CLI you like, Gobby
makes it more reliable. If you want to use several of them for different jobs,
Gobby is the only thing that keeps them coherent. If you want to send a task
into the build loop and get a PR back, Gobby is the only open-source project
I'm aware of that does that locally.

---

## What shipped in 0.4.0

0.4.0 is the first release where the full task → PR loop is the supported
path, not a power-user trick.

- **`gobby build`** as the single entry point: CLI, MCP, and HTTP all resolve
  to one shared build service with the same `BuildResult` shape. `--quick` for
  one-step lifecycles, `--skip-stage` and per-stage cap overrides for shape
  control, task-scoped controls (`stop`, `resume`, `clean`, `restart`), branch
  cleanup, retry recovery.
- **Stage-native lifecycle**: `task_stage_states`, `task_dispatch_mutex`,
  `task_artifacts`, `task_lifecycle_events`. Review verdicts attached to
  manifest rows. PR and merge delivery artifacts.
- **Factory Droid** as a first-class CLI source — hooks, sessions, transcripts,
  spawned agents, web chat parity.
- **Run-based task expansion** with configurable depth, five-level ceiling,
  expansion QA coverage manifests, and inventory checks.
- **Skill loading on demand**, skill hubs (SkillsMP, GitHub-backed installs),
  brevity injection, verification/review skill patterns.
- **Memory and code-graph maintenance**: stale-memory auditor, async knowledge-
  graph rebuilds, embedding health, code-index refreshes.
- **Observability** for sessions, models, tokens, traces, local-model status,
  and a built-in trace viewer.
- **Web UI** improvements across chat, sessions, tasks, workflows, cron,
  projects — including 320px compact layouts and shared design tokens.

Full release notes: [CHANGELOG.md](CHANGELOG.md).

---

## Architecture

- Python 3.13+ daemon (`uv` for everything)
- SQLite at `~/.gobby/gobby-hub.db`
- HTTP API on `localhost:60887`, WebSocket on `:60888`, web UI on `:60889`
- stdio MCP server for coding assistants
- Hook adapters for Claude Code, Codex, Gemini CLI, Qwen CLI, Factory Droid
- Optional Qdrant + FalkorDB for vector and graph-backed search
- Companion Rust toolchain via [gobby-cli](https://github.com/GobbyAI/gobby-cli)

The SQLite database at `~/.gobby/gobby-hub.db` is the source of truth for task
state. `.gobby/tasks.jsonl` is the git-native sync projection — checked in,
diffable in PRs, and reconciled with the DB so task-linked commits stay
auditable across machines. Linear is supported as an optional external sync
target for teams that already track work there.

The guides set is the source of truth for behavior:

- [docs/guides/tasks.md](docs/guides/tasks.md) — task lifecycle, validation, commit-linked closure
- [docs/guides/dispatch.md](docs/guides/dispatch.md) — stage-manifest dispatch and rule chain
- [docs/guides/orchestration.md](docs/guides/orchestration.md) — build, agents, isolation, review
- [docs/guides/sessions.md](docs/guides/sessions.md) — session lifecycle and handoffs
- [docs/guides/mcp-tools.md](docs/guides/mcp-tools.md) — MCP proxy and progressive discovery
- [docs/guides/workflows-overview.md](docs/guides/workflows-overview.md) — rules, agents, pipelines, dispatch
- [docs/guides/system-requirements.md](docs/guides/system-requirements.md) — prerequisites

See [docs/guides/README.md](docs/guides/README.md) for the full guide index.

---

## Supported CLIs

| CLI | Integration | What Gobby adds |
| --- | --- | --- |
| Claude Code | Hooks + MCP | Durable sessions, task links, rule-enforced workflows, build dispatch |
| Codex | Hooks + MCP | Shared tasks, MCP access, spawned agents, cross-CLI handoffs |
| Gemini CLI | Hooks + MCP | Cross-session context, memory, tasks, pipelines |
| Qwen CLI | Hooks + MCP | Shared lifecycle, local-model flags, session state |
| Factory Droid | Hooks + MCP | Droid sessions, transcript parsing, spawned-agent flows |

A task started in any one of them can be continued in any other with the same
local state, validation gates, and review state.

Local model providers (LM Studio, Ollama) work through the same hooks and MCP
layer wherever the underlying CLI supports OpenAI-compatible endpoints.

---

## Install

Try without installing:

```bash
uvx gobby --help
```

Install globally:

```bash
# With uv (recommended)
uv tool install gobby

# With pipx
pipx install gobby

# With pip
pip install gobby
```

Python 3.13+ is required for the 0.4.x series.

---

## Quick start

From a project directory:

```bash
gobby start                  # start the daemon
gobby install                # detect supported CLIs and wire hooks + MCP
gobby init                   # initialize .gobby/ for this repo
```

`gobby install` configures every detected CLI with the same stdio MCP server:

```json
{
  "mcpServers": {
    "gobby": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "gobby", "mcp-server"]
    }
  }
}
```

Open the local web UI at `http://localhost:60889` once the daemon is running.

Then either start interactive work in your CLI of choice — Gobby will track it
quietly — or hand it a task and let the build loop run:

```bash
gobby tasks create "Add OAuth refresh-token rotation" --type feature
gobby build '#<id>'
```

For agent operating instructions in this repository, read [CLAUDE.md](CLAUDE.md).

---

## Where it's going

0.4.0 is the platform baseline. The next chunk of work is hardening that
baseline, then porting the hot path to Rust, then opening up multi-machine and
team surfaces.

### Post-0.4.0: hardening

- **PostgreSQL hub migration** (`#12761`) — replace SQLite as the runtime hub
  with `psycopg` v3, `pg_search`, dual-backend test infra, and a one-shot
  cold-cutover migration tool. Phased across baseline reflattening, service
  bootstrap, dual-backend tests, schema and query parity, migration tooling,
  cutover, and rollback.
- **FalkorDB graph migration** (`#12746`) — swap Neo4j for FalkorDB across
  daemon writes, Rust read clients, web UI, admin payloads, and the setup
  wizard.
- **Memory recall helper** (`#12898`) — bounded background helper agent that
  searches memory per turn and injects fresh results once into the parent
  session.
- **Plan registry APIs and UI editors** (`#14140`) — expose stage and build-
  profile registries through APIs and editing surfaces so lifecycle shape can
  evolve without hand-editing storage.
- **Attached-session UX parity** with first-class web chat: context-usage
  indicator, mode/model sync, attachments relay, persona switching, STT/TTS.
- **Logging cleanup** before enforcing logging-format rules: config reset,
  runtime-vs-app log separation, normalized handlers, automation logs for
  cron and dispatch.

### 0.5.0: Rust migration

Strangler migration, not a rewrite. Python remains the public daemon and
behavioral reference until each boundary passes parity, observability, and
rollback gates. Rust sidecars run on internal ports, with Python delegating
selected route families behind explicit flags. Compare mode runs both and
returns the Python response until parity is proven.

The bridgehead already exists in [gobby-cli](https://github.com/GobbyAI/gobby-cli):
`gcode`, `gsqz`, `gloc`, `ghook`, plus `gobby-core` shared primitives. 0.5.0
extends that into the daemon itself.

### Later

- **Pro sync and multi-daemon** — encrypted sync for tasks, memories, and
  session metadata; multi-daemon discovery and handshake; fleet inventory,
  health, and remote command; shared task boards, team workflows, audit, and
  enterprise controls. This is the commercial layer.
- **Native apps** — desktop app with tray lifecycle and a bundled daemon;
  mobile companion for observing sessions, reviewing tasks, and approving
  gates remotely.
- **Ecosystem** — public plugin registry, stack-specific starter packs (hooks,
  workflows, skills, task templates), additional CLI integrations.
- **SWE-bench evaluation** (`docs/plans/SWE-BENCH.md`) — eval run/result
  storage, `gobby eval` CLI, Docker-backed harness, trajectory capture,
  Gobby-enabled vs baseline A/B tests.

Full plan: [ROADMAP.md](ROADMAP.md).

---

## Status and contributing

Gobby is pre-1.0 and moving fast. The 0.4.x line is what I run and ship from
every day, but APIs, configuration, workflow definitions, and hook behavior
will continue to change as the daemon hardens. If that's a problem for you,
wait for 1.0. If you want to influence the shape of it, jump in now.

Apache 2.0 licensed. See [CONTRIBUTING.md](CONTRIBUTING.md) for development
guidance.

---

<p align="center">
  <sub>Built with Gobby. By a human and a lot of agents, working in the same repo.</sub>
</p>
