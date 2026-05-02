---
name: ideate
description: Internal methodology for the ideation discovery stage. Produces a concise Discovery Brief for downstream research, architecture, PRD, and planning.
version: "1.0.0"
category: methodology
internal: true
triggers: ideation, discovery brief, problem framing
metadata:
  gobby:
    audience: agent
    depth: 0
---

# ideate - Discovery Brief Methodology

Use this skill when acting as the `analyst` discovery-stage agent.

## Purpose

Frame the work before evidence gathering. Convert the task description, user
context, existing labels, and any visible constraints into a short brief that
downstream stages can rely on.

## Method

1. Read the assigned task and existing discovery marker blocks.
2. Identify the real problem, not just the requested change.
3. Separate known constraints from assumptions.
4. State the most likely solution hypotheses without committing architecture.
5. Capture open questions only when answering them would change the work.

## Output

Write only this section shape:

```markdown
## Discovery Brief

### Problem
- <what needs to be solved>

### Constraints
- <known constraint>

### Hypotheses
- <testable direction>

### Open Questions
- <question or "None">
```

Keep the brief practical and evidence-seeking. Do not write implementation
tasks, PRD scope, architecture decisions, or planning manifests.
