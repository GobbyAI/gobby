---
name: expansion-agent-selection
description: Select the best bundled developer agent for automated expansion leaves and document fallback decisions.
version: "1.0.0"
category: core
internal: true
triggers: expansion agent selection, assigned agent, automated leaves
metadata:
  gobby:
    audience: all
    depth: 0
---

# expansion-agent-selection - Automated Leaf Agent Selection

Use this skill during the expander's Agent Selection step after leaf generation
and before the compiled spec is returned or applied.

## Automated Leaf Categories

Assign an agent for every automated leaf in these categories:

- `code`
- `config`
- `docs`
- `planning`
- `refactor`
- `research`
- `test`

Reject `category: manual` leaves. Manual is valid for direct task creation, but
automated expansion manifests must compile to deterministic leaf work.

## Label Vocabulary

Use labels and task text as weak signals. They should guide the decision, not
override clear file paths or task descriptions.

### Frontend indicators

Prefer `frontend-developer` for work involving UI components, CSS, browser
behavior, accessibility, routing, client state, React, Vue, Svelte, Vite,
Webpack, Storybook, Playwright UI flows, Lighthouse, or frontend linting such as
ESLint.

### Backend indicators

Prefer `backend-developer` for APIs, CLIs, storage, migrations, schedulers,
workers, servers, daemon code, Python modules, pytest, mypy, Ruff, SQL, sqlite3,
psql, MCP tools, workflow engines, and infrastructure tests.

### Fullstack indicators

Mixed-stack tasks that touch both UI and backend should be split when the plan
permits. If splitting would violate the plan's atomic task shape, pick the
primary agent for the riskiest surface and document the cross-concern surface
in the leaf description.

## Heuristics

1. Read the leaf title, description, category, labels, and affected files.
2. Compare those signals with available agent names and descriptions from
   `list_agent_definitions`.
3. Prefer a specialized matching agent over a general fallback.
4. Keep `additional_skills` empty unless the leaf needs a skill beyond the
   agent's baseline competence.
5. Do not escalate on ambiguity.

## Confidence Threshold

A confident match requires a clear category or content signal and an available
agent whose name or description matches the surface. If the signal is weak,
mixed, or no available agent crosses the threshold, use the fallback.

## Fallback

Default to `backend-developer` when no agent matches above the confidence
threshold. Append a `## Agent Selection` marker to the leaf description that
explains the default, including the leaf category and the ambiguous signals.

Example marker:

```markdown
## Agent Selection

No specialized agent matched this docs leaf above the confidence threshold.
Defaulting to `backend-developer` for auditability.
```

The fallback is intentional audit data, not an error path. Repeated fallback
markers are the signal to add a more specialized agent later.
