<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img src="logo.png" alt="Gobby" width="200" />
</p>

<h1 align="center">Gobby</h1>

<p align="center">
  <strong>Local-first daemon and workflow control plane for AI coding tools.</strong>
</p>

<p align="center">
  <a href="https://github.com/GobbyAI/gobby"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
  <a href="https://github.com/GobbyAI/gobby/blob/main/LICENSE.md"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://github.com/GobbyAI/gobby/stargazers"><img src="https://img.shields.io/github/stars/GobbyAI/gobby?style=flat" alt="Stars"></a>
  <a href="https://github.com/GobbyAI/gobby/issues"><img src="https://img.shields.io/github/issues/GobbyAI/gobby" alt="Issues"></a>
</p>

---

Gobby runs as a long-lived local daemon that unifies AI coding CLIs like Claude Code, Gemini CLI, and Codex, giving them shared sessions, memory, workflows, and guardrails instead of yet another one-off helper script. Because Claude Code natively supports OpenAI-compatible endpoints, local model providers like LM Studio and Ollama work out of the box — the same Gobby workflows run against both cloud and local models without changing your setup.

**Gobby is built with Gobby.** Most of this codebase was written by AI agents running through Gobby's own task system and workflows — over 10,000 tasks tracked and counting.

> Deterministic when you need it, autonomous when you do not — with hooks and workflows enforcing guardrails either way.

---

## Why Gobby exists

Modern AI coding tools are powerful but fragmented: each CLI has its own idea of sessions, context, and tasks, and none of them give you a single place to coordinate agents across tools. They also tend to burn tokens on redundant MCP metadata, verbose shell output, and repeated code snippets instead of the information that actually matters to the current change.

Gobby solves this by acting as the control plane for your AI coding stack rather than another agent competing for context. The daemon sits between CLIs, hooks, and MCP servers, orchestrating:

- **Session and state** shared across tools, terminals, and restarts
- **Workflows and pipelines** that can run deterministically or autonomously under rule-enforced guardrails
- **Memory and skills** that are captured automatically and injected only when they are relevant
- **Token optimization** via AST-aware code indexing (`gcode`) and CLI-output compression (`gsqz`)

---

## Core concepts

### Daemon, hooks, and CLIs

Gobby runs as a local daemon with HTTP, WebSocket, and MCP endpoints and never requires a cloud control plane. Your AI CLIs talk to it in two ways:

- **Hooks**: lightweight adapters for Claude Code, Gemini CLI, and Codex that send structured events ("user executed this command", "assistant applied this edit", "session compacted context"), enabling deterministic, testable workflows around otherwise opaque sessions.
- **MCP server**: a stdio-based FastMCP endpoint exposing Gobby's task, session, memory, workflow, and orchestration APIs as tools your assistants can call directly from within the editor.

Because Claude Code natively supports OpenAI-compatible endpoints, local model providers like LM Studio and Ollama work through the same hooks and MCP tools as cloud providers. You can prototype workflows on local models and later swap in cloud providers without rewriting anything.

### Workflow engine and pipelines

At the heart of Gobby is a workflow and pipeline engine that can run in two distinct modes:

- **Deterministic pipelines**: declarative, step-based workflows (shell, prompts, nested pipelines, session spawns) that execute in a predictable order with explicit approval gates for human-in-the-loop control.
- **Autonomous orchestration**: hook- and cron-driven flows that spawn agents, fan out tasks across worktrees or clones, and drive review loops until rule conditions are satisfied, all without manual intervention.

Pipelines are tick-based and can be triggered from hooks (for example, "on push to main" or "when a new epic is created"), from MCP tools, or via HTTP APIs, giving you a single orchestration layer for both manual and automated work.

### Rules and hook-enforced guardrails

Gobby treats safety, discipline, and project conventions as **rules**, not prompts. Rules are evaluated on every hook and workflow event, and can:

- Block unsafe or undesired tool calls (for example preventing `git push` from inside child agents)
- Inject context or skills before a tool executes
- Enforce task-claiming, test-first workflows, and stop-gates for risky changes

This makes autonomous execution far more predictable: if a rule is violated, the workflow is blocked or rerouted instead of relying on the model to "remember" instructions buried in system prompts.

### Memory and skills

Gobby's memory system is designed for **automated capture** and **context-driven injection**, not manual note-taking.

- Hooks and MCP tools record persistent facts, decisions, and outcomes as the daemon observes your sessions.
- During future work, Gobby injects only the memories and skills that match the current project, files, and tasks, rather than dumping the entire history into context.
- Skills are structured instruction packs (defined in `SKILL.md`) that teach agents how to perform recurring workflows, and can be scoped globally or per project.

The end result is that your agents feel like they "remember" how your project works without you hand-curating prompts.

### MCP proxy with progressive discovery

Connecting multiple MCP servers usually means paying a massive token tax every time your assistant loads tools. Gobby's MCP client proxy avoids this with **progressive discovery**:

- A cheap `list_tools` call returns just names and descriptions.
- Full JSON schemas are only fetched when a tool is actually being used.
- Gobby attaches its own context (task, session, project) when routing tool calls so downstream servers can behave more intelligently.

This keeps context focused on the current change instead of on static tool metadata.

### Code intelligence and token optimization

Two companion tools ship alongside Gobby to keep your context window focused on signal:

- **`gcode`** (from the [`gobby-code`](https://github.com/gobby-cli/gobby-code) Rust crate) builds an AST-aware symbol index over your repositories. Gobby enhances it with a knowledge graph and vector database, giving you Reciprocal Rank Fusion scoring that combines FTS5 full-text search, semantic similarity, graph traversal, and graph-associated memory-to-code-symbol references. The result is retrieval by symbol instead of by file — agents find functions, types, and usages without loading entire files.
- **`gsqz`** (from the [`gobby-squeeze`](https://github.com/gobby-cli/gobby-squeeze) Rust crate) wraps shell commands and compresses their output via configurable pipelines, collapsing verbose test runs, linters, and git noise down to concise summaries before injecting them into prompts.

Together, these tools turn "show me everything" patterns into "show me just enough structure to act" while dramatically reducing token spend.

### Web UI

Gobby ships a built-in web interface that auto-starts with the daemon:

- **Chat** with MCP tool support, voice chat, model switching, and slash commands
- **Tasks** — kanban board, tree view, dependency graph, Gantt chart, detail panel
- **Memory** — table view, Neo4j 3D knowledge graph
- **Sessions** — lineage tree, transcript viewer, AI summary generation
- **Observability** — OpenTelemetry tracing, metrics, and a built-in trace viewer with waterfall visualization
- Cron jobs, configuration, skills, projects, agent registry, file browser, and terminal panel

Access at `http://localhost:60887` when the daemon is running.

---

## Supported CLIs and providers

### AI coding CLIs

Gobby's 0.3.x series provides first-class integration with three primary CLIs, all with functional parity through hooks and MCP:

| CLI         | Integration style    | What Gobby adds                                                |
|------------|----------------------|----------------------------------------------------------------|
| Claude Code| Hooks + MCP server   | Persistent sessions, task syncing, rule-enforced workflows     |
| Gemini CLI | Hooks + MCP server   | Shared memory and tasks, cross-session context, pipelines      |
| Codex      | Hooks + MCP server   | Centralized orchestration, tool access, and background agents  |

All three CLIs talk to the same daemon, so a task started in Claude Code can be finished from Gemini CLI or Codex with full context and validation.

### Local model providers

Claude Code supports OpenAI-compatible endpoints, which means local model providers work seamlessly through Gobby's hooks and MCP layer:

- **LM Studio** exposes an OpenAI-compatible API on `localhost` — point Claude Code at it for completely local execution.
- **Ollama** does the same via `ollama serve` on `localhost:11434`.

Because Gobby orchestrates at the session and workflow level rather than the model level, the same tasks, rules, memory, and pipelines apply regardless of whether the underlying model is local or cloud-hosted.

---

## Installation

Gobby is distributed as a Python package and installs as a CLI that manages the daemon, MCP server, and web UI.

### Try it instantly

```bash
uvx gobby --help
```

### Install globally

```bash
# With uv (recommended)
uv tool install gobby

# With pipx
pipx install gobby

# With pip
pip install gobby
```

Python 3.13+ is recommended for the 0.3.x series.

---

## Quick start

From a project directory:

```bash
gobby start   # Start the daemon
gobby init    # Initialize .gobby state for this repo
gobby install # Detect and install hooks for supported CLIs
```

`gobby install` auto-detects your installed CLIs and configures hooks and the MCP server for each one. Under the hood, all three CLIs get the same stdio-based MCP configuration:

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

Once the daemon is running and hooks are installed, you can start issuing tasks, invoking workflows, and letting agents orchestrated by Gobby handle the rest.

---

## Deterministic vs autonomous usage

Gobby is built to support two complementary usage patterns rather than forcing a single "agent does everything" philosophy.

- **Deterministic mode**: use pipelines and workflows as testable automation, where each step is explicit, reviewable, and subject to approval gates.
- **Autonomous mode**: let agents spawned through Gobby run multi-step changes under rule-enforced guardrails, with worktrees or clones keeping experiments isolated until you are ready to merge.

Because both modes share the same daemon, hooks, memory, and skills, you can start with deterministic automation and progressively hand more levers to agents as you gain confidence.

---

## For AI agents

**If you're an AI agent reading this:** Check out [CLAUDE.md](CLAUDE.md) for operating guidance.

---

## Status, roadmap, and contributing

Gobby's 0.3.x line is still pre-1.0 and evolving quickly; APIs and configuration formats may change as the daemon, workflow engine, and hook integrations are hardened for long-running use. Roadmap items include deeper local-model integration, additional CLIs, and more prebuilt workflows for common engineering tasks.

The project is Apache 2.0 licensed and welcomes contributions from developers who want a more transparent, developer-centric control plane for AI coding tools. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

<p align="center">
  <sub>Built with 🤖 by humans and AI, working together.</sub>
</p>
