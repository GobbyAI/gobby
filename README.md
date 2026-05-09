<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img src="logo.png" alt="Gobby" width="200" />
</p>

<h1 align="center">Gobby</h1>

<p align="center">
  <strong>Local-first agent control plane for AI coding tools.</strong>
</p>

<p align="center">
  <a href="https://github.com/GobbyAI/gobby"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
  <a href="https://github.com/GobbyAI/gobby/blob/main/LICENSE.md"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://github.com/GobbyAI/gobby/stargazers"><img src="https://img.shields.io/github/stars/GobbyAI/gobby?style=flat" alt="Stars"></a>
  <a href="https://github.com/GobbyAI/gobby/issues"><img src="https://img.shields.io/github/issues/GobbyAI/gobby" alt="Issues"></a>
</p>

---

Gobby is the local daemon that lets AI coding tools share the same sessions, tasks,
memory, tools, workflows, and guardrails. It works across Claude Code, Codex,
Gemini CLI, Qwen CLI, and Factory Droid, so work can move between assistants
without losing the thread.

The new bottleneck is coordination: agents forget context, duplicate work, drift
from project rules, and burn tokens rediscovering tools and state. Gobby turns
those loose sessions into a durable local system.

**Gobby is built with Gobby.** This repo's `.gobby/tasks.jsonl` contains 8,000+
source-tracked task records, and the 0.4.0 release was assembled through Gobby's
own task, review, agent, and documentation workflows.

---

## What Gobby is

Gobby sits underneath the models, IDEs, and coding assistants you already use.
It is the shared control plane for their work.

It provides:

- **Shared sessions** that survive restarts, context compaction, terminal swaps,
  and handoffs between supported CLIs.
- **A durable task ledger** with dependencies, stage manifests, claims, review
  state, validation criteria, and commit-linked closure.
- **A progressive MCP proxy** that discovers tools lazily, so agents avoid
  flooding their context windows with every schema from every server.
- **Rules and workflows** that enforce project behavior at hook time instead of
  hoping a prompt reminder sticks.
- **Persistent memory and skills** that are captured once and injected only when
  relevant.
- **Agent orchestration** with spawned sessions, worktree or clone isolation,
  review gates, and merge support.
- **Local observability** for sessions, token usage, traces, metrics, task state,
  and agent runs.

Gobby runs locally. Your database, task state, hooks, transcripts, and workflow
definitions stay on your machine unless you choose to connect external services.

---

## Why it matters

AI coding is moving from one assistant in one terminal to many specialized
agents working across tools. Without shared infrastructure, every CLI becomes
its own island: separate memory, separate task state, separate rules, separate
logs, and separate failure modes.

Gobby makes the coordination layer explicit:

- One task can start in Claude Code, continue in Codex, and finish in Gemini CLI.
- A spawned agent can work in an isolated worktree while the parent session keeps
  its own context clean.
- A rule can block unsafe behavior before it happens.
- A workflow can require task claims, tests, reviews, or commits before a turn is
  allowed to end.
- An MCP client can discover tools progressively instead of loading tens of
  thousands of tokens of schemas up front.

That is the wedge: make today's agent work more reliable, then grow into the
local operating layer for AI software development.

---

## Interactive and autonomous work

Gobby supports two ways of working.

**Interactive work** is the normal pair-programming loop. You use Claude Code,
Codex, Gemini CLI, Qwen CLI, Factory Droid, or Gobby's web chat while the daemon
quietly tracks session state, task links, tool calls, memories, and rules.

**Autonomous work** starts when you hand Gobby a built task tree, plan, or stage
and let agents execute under the daemon's lifecycle. Gobby can dispatch workers,
create isolation, wait on completion IDs, route review, and land results.

The useful distinction is interactive versus autonomous. Agents still need
freedom to explore, design, and implement. Gobby makes the boundaries
deterministic where correctness matters:

- task ownership and dependency readiness
- lifecycle stages and review gates
- hook-time rules and blocked operations
- validation criteria and focused verification
- worktree or clone isolation
- commit-linked task closure

You get agent freedom inside a system that can still say "no" when the workflow
would become unsafe or unverifiable.

---

## What shipped in 0.4.0

Gobby 0.4.0 is a large hardening release. Highlights include:

- Factory Droid as a first-class CLI source across hooks, sessions, storage, web
  chat, and spawned-agent flows.
- Stage-native task lifecycle state, review verdicts, dispatch mutexes, task
  artifacts, and lifecycle event storage.
- `gobby build` automation for plans, epics, leaf tasks, isolated workspaces,
  retry recovery, branch cleanup, and task-scoped controls.
- Run-based task expansion with configurable depth and coverage inventory checks.
- On-demand skill loading, skill hubs, brevity injection, and review/writing
  skill patterns.
- Better observability for sessions, models, token usage, traces, and local model
  status.
- Memory and code graph maintenance tools for embeddings, stale-memory audits,
  and knowledge-graph rebuilds.
- Web UI improvements across chat, sessions, tasks, workflows, cron, projects,
  and compact layouts.

Read the full [CHANGELOG.md](CHANGELOG.md) for release details.

---

## Architecture

Gobby is a Python 3.13+ daemon with:

- SQLite at `~/.gobby/gobby-hub.db`
- HTTP API on `localhost:60887`
- WebSocket server on `localhost:60888`
- Web UI on `localhost:60889`
- stdio MCP server for coding assistants
- hook adapters for supported CLIs
- optional Qdrant and Neo4j services for vector and graph-backed search

Git remains the source of truth for project task state through `.gobby/tasks.jsonl`.
The database gives the daemon fast local state, while task-linked commits make
the history auditable.

The current guide set is the source of truth for behavior:

- [docs/guides/tasks.md](docs/guides/tasks.md)
- [docs/guides/sessions.md](docs/guides/sessions.md)
- [docs/guides/mcp-tools.md](docs/guides/mcp-tools.md)
- [docs/guides/workflows-overview.md](docs/guides/workflows-overview.md)
- [docs/guides/orchestration.md](docs/guides/orchestration.md)
- [docs/guides/system-requirements.md](docs/guides/system-requirements.md)

See [docs/guides/README.md](docs/guides/README.md) for the full guide index.

---

## Supported CLIs

Gobby 0.4.x has first-class support for:

| CLI | Integration | What Gobby adds |
| --- | --- | --- |
| Claude Code | Hooks + MCP | Durable sessions, task links, rule-enforced workflows |
| Codex | Hooks + MCP | Shared tasks, MCP access, spawned agents, handoffs |
| Gemini CLI | Hooks + MCP | Cross-session context, memory, tasks, pipelines |
| Qwen CLI | Hooks + MCP | Shared lifecycle, local model flags, session state |
| Factory Droid | Hooks + MCP | Droid sessions, transcript parsing, spawned-agent flows |

All supported CLIs talk to the same daemon. A task started in one tool can be
continued from another with the same local state and validation gates.

Gobby also works with local model providers through OpenAI-compatible endpoints
where the underlying CLI supports them, including LM Studio and Ollama.

---

## Install

Try it without installing:

```bash
uvx gobby --help
```

Install globally:

```bash
# With uv
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
gobby start
gobby init
gobby install
```

`gobby install` detects supported CLIs and configures hooks plus the Gobby MCP
server. The MCP server uses stdio:

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

For Factory Droid:

```bash
curl -fsSL https://app.factory.ai/cli | sh
gobby start
gobby init
gobby install --droid
```

Open the local web UI at `http://localhost:60889` after the daemon starts.

---

## Core workflows

Use Gobby to:

- create and claim tasks before editing files
- link commits to task closure
- preserve handoff context across compactions and restarts
- search code and memory through local text, vector, and graph indexes
- dispatch agents into isolated worktrees or clones
- run deterministic pipelines with approval gates
- enforce project rules at `turn_start`, `before_tool`, `after_tool`, and
  `turn_end`

For agent operating instructions in this repository, read [CLAUDE.md](CLAUDE.md).

---

## Status and contributing

Gobby is pre-1.0 and moving quickly. The 0.4.x line is usable, but APIs,
configuration formats, workflow definitions, and hook behavior may still change
as the daemon hardens.

The project is Apache 2.0 licensed. See [CONTRIBUTING.md](CONTRIBUTING.md) for
development guidance.

---

<p align="center">
  <sub>Built with Gobby by humans and AI agents working in the same repo.</sub>
</p>
