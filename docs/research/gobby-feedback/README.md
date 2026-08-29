# Gobby Session Feedback

Capture first-hand agent observations about the Gobby harness (daemon, rules,
tools, and session lifecycle). The Markdown inbox under `inbox/` is retired.
Write observations to the `session_feedback` table through
`gobby-sessions:feedback`.

Most context epochs should produce no rows. Task-close and pre-compact survey
gates share one epoch-scoped acknowledgment, so an agent is asked at most once
between closures. Empty `observations: []` is valid and inserts nothing.

A useful report contains at most three specific observations grounded in the
current session. Suitable kinds are `friction`, `noise`, `surprise`,
`missing-affordance`, and `positive-signal`.

```python
call_tool("gobby-sessions", "feedback", {
    "observations": [
        {
            "source": "rule",
            "kind": "friction",
            "evidence": "Specific rule, tool, message, or observed sequence.",
            "impact": "Extra turns, confusion, blocked progress, or useful acceleration.",
            "frequency": "once",
            "suggestion": "Concrete improvement, when apparent.",
        }
    ]
})
```

This survey is about Gobby, not the current repository's product. Concrete
defects in the current repo remain Found Work. Durable project knowledge belongs
in `gobby-memory`. Reports must omit secrets, private user content, and
transcript dumps.

Postgres is the capture log. Embedding `session_feedback` into Qdrant or
`search_memories` is not part of capture.
