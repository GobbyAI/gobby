---
name: research
description: Internal methodology for the research discovery stage. Produces concise evidence and source-backed context for architecture and PRD stages.
version: "1.0.0"
category: methodology
internal: true
triggers: research findings, evidence gathering, domain context
metadata:
  gobby:
    audience: agent
    depth: 0
---

# research - Research Findings Methodology

Use this skill when acting as the `researcher` discovery-stage agent.

## Purpose

Answer the discovery brief's highest-value questions with evidence. Prefer
repository facts, task history, project memory, and primary sources. Browse only
when the information is time-sensitive, external, or not present locally.

## Method

1. Read the task and prior discovery marker blocks.
2. Turn open questions and hypotheses into focused research questions.
3. Gather evidence from the repo, task graph, memories, docs, and external
   sources when needed.
4. Record source links or local file references when evidence supports a claim.
5. Call out uncertainty and risks that affect product or architecture choices.

## Output

Write only this section shape:

```markdown
## Research Findings

### Research Questions
- <question investigated>

### Domain Context
- <context that changes downstream choices>

### Evidence & Sources
- <evidence with source or file reference>

### Risks
- <risk or "None">
```

Keep findings traceable. Do not invent source citations or bury unsupported
assumptions as facts.
