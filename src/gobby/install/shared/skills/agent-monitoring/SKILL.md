---
name: agent-monitoring
description: "Inspect Gobby agent progress through supported MCP tools before using raw terminal or database fallbacks."
category: core
triggers: monitor agent, agent status, running agents, child session, capture output, agent result
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Agent Monitoring

Use supported MCP tools for normal progress inspection. They preserve session
context, agent-run metadata, and terminal capture behavior without depending on
raw process internals.

## Normal Progress Checks

Use `gobby-agents` first:

- `list_running_agents` for the build-wide active-agent overview
- `get_running_agent` for one active run
- `list_agent_runs` for recent runs tied to a parent session
- `get_agent_result` for completed run output and status

`list_running_agents` defaults to build-wide scope. Use `scope="parent"` or
`parent_session_id="<session>"` when you specifically need only direct children
of one parent session. Use `status="running"` when comparing with
`gobby agents runs list --status running`.

Use `gobby-sessions` when the question is about the child session or terminal
context:

- `get_session` for session metadata and terminal context
- `get_session_messages` for recorded messages
- `capture_output` for terminal-backed output capture

Follow progressive discovery before calls: list servers, list tools, fetch the
schema, then call the tool.

## Fallbacks

Treat raw SQLite/database queries and direct `tmux` commands as debugging fallbacks.
Use them only after the MCP tools cannot answer the question or when you are
fixing the monitoring stack itself. Prefer recording what the supported tools
returned before falling back so the reason for bypassing them is explicit.
