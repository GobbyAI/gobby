# Gobby Implementation Roadmap

## Overview

This document defines the implementation order across all Gobby planning documents. Each phase is designed to deliver standalone value while building toward the complete vision.

## Document References

| Document | Location | Focus |
|----------|----------|-------|
| HOOK_EXTENSIONS | `docs/plans/HOOK_EXTENSIONS.md` | WebSocket events, webhooks, plugins |
| WORKFLOWS | `docs/plans/WORKFLOWS.md` | Phase-based workflow enforcement |
| TASKS | `docs/plans/TASKS.md` | Persistent task tracking system |
| MCP_PROXY_IMPROVEMENTS | `docs/plans/MCP_PROXY_IMPROVEMENTS.md` | Tool metrics, semantic search, self-healing |

---

## Implementation Order

```
═══════════════════════════════════════════════════════════════════════════════
                              FOUNDATION LAYER
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 1: Hook Event Broadcasting                                            │
│ HOOK_EXTENSIONS Phase 1                                                      │
│                                                                              │
│ Deliverable: Real-time hook events via WebSocket                            │
│ Dependencies: None (uses existing WebSocket infrastructure)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 2: Core Task System                                                   │
│ TASKS Phases 1-6                                                             │
│                                                                              │
│ Deliverable: Task CRUD, dependencies, ready work detection, git sync        │
│ Dependencies: None (self-contained)                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 3: Task MCP Tools & CLI                                               │
│ TASKS Phases 7-10                                                            │
│                                                                              │
│ Deliverable: Task management via MCP tools and CLI                          │
│ Dependencies: Sprint 2                                                       │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                              WORKFLOW ENGINE
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 4: Workflow Foundation                                                │
│ WORKFLOWS Phases 0-2                                                         │
│                                                                              │
│ Deliverable: YAML loader, state manager, core engine                        │
│ Dependencies: None                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 5: Workflow Hook Integration                                          │
│ WORKFLOWS Phase 3                                                            │
│                                                                              │
│ Deliverable: Workflows evaluate on hook events, tool blocking               │
│ Dependencies: Sprint 4                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 6: Workflow Actions                                                   │
│ WORKFLOWS Phase 4                                                            │
│                                                                              │
│ Deliverable: inject_context, capture_artifact, generate_handoff, etc.       │
│ Dependencies: Sprint 5                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7: Context Sources & Templates                                        │
│ WORKFLOWS Phases 5-6                                                         │
│                                                                              │
│ Deliverable: Jinja2 templating, built-in workflow templates                 │
│ Dependencies: Sprint 6                                                       │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                            EXTENSIONS & INTEGRATION
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 8: Webhooks                                                           │
│ HOOK_EXTENSIONS Phase 2                                                      │
│                                                                              │
│ Deliverable: Config-driven HTTP callouts on hook events                     │
│ Dependencies: Sprint 1 (broadcaster pattern)                                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 9: Python Plugins                                                     │
│ HOOK_EXTENSIONS Phase 3                                                      │
│                                                                              │
│ Deliverable: Dynamic plugin loading, custom hook handlers                   │
│ Dependencies: Sprint 1                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 10: Workflow CLI & MCP Tools                                          │
│ WORKFLOWS Phases 7-8                                                         │
│                                                                              │
│ Deliverable: gobby workflow commands, workflow MCP tools                    │
│ Dependencies: Sprint 7                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 11: Workflow-Task Integration                                         │
│ TASKS Phases 11-13                                                           │
│                                                                              │
│ Deliverable: Tasks linked to workflows, LLM expansion, agent instructions   │
│ Dependencies: Sprint 3 + Sprint 7                                            │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                              MCP PROXY ENHANCEMENTS
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 12: Tool Metrics                                                      │
│ MCP_PROXY_IMPROVEMENTS Phase 1                                               │
│                                                                              │
│ Deliverable: Track tool call/success rates, expose in recommendations       │
│ Dependencies: None                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 13: Lazy Server Init                                                  │
│ MCP_PROXY_IMPROVEMENTS Phase 2                                               │
│                                                                              │
│ Deliverable: Deferred MCP server connections, faster startup                │
│ Dependencies: None                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 14: Semantic Tool Search                                              │
│ MCP_PROXY_IMPROVEMENTS Phase 3                                               │
│                                                                              │
│ Deliverable: Embeddings-based tool search, hybrid recommend_tools           │
│ Dependencies: Sprint 12                                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 15: Self-Healing & Incremental Indexing                               │
│ MCP_PROXY_IMPROVEMENTS Phases 4-5                                            │
│                                                                              │
│ Deliverable: Fallback suggestions on failure, hash-based schema refresh     │
│ Dependencies: Sprint 14                                                      │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                              POLISH & DOCUMENTATION
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 16: Hook Extensions CLI & Workflow Integration                        │
│ HOOK_EXTENSIONS Phases 4-5                                                   │
│                                                                              │
│ Deliverable: Webhook as workflow action, plugin-defined actions/conditions  │
│ Dependencies: Sprint 9 + Sprint 7                                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 17: Testing & Error Recovery                                          │
│ WORKFLOWS Phases 9-11                                                        │
│                                                                              │
│ Deliverable: Comprehensive tests, crash recovery, escape hatches            │
│ Dependencies: Sprint 10                                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 18: Documentation                                                     │
│ ALL PLANS Documentation Phases                                               │
│                                                                              │
│ Deliverable: User guides, examples, updated CLAUDE.md                       │
│ Dependencies: All previous sprints                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Sprint Summary Table

| Sprint | Focus | Plan Reference | Dependencies |
|--------|-------|----------------|--------------|
| 1 | WebSocket Broadcasting | HOOK_EXTENSIONS Phase 1 | None |
| 2 | Core Task System | TASKS Phases 1-6 | None |
| 3 | Task MCP/CLI | TASKS Phases 7-10 | Sprint 2 |
| 4 | Workflow Foundation | WORKFLOWS Phases 0-2 | None |
| 5 | Workflow Hooks | WORKFLOWS Phase 3 | Sprint 4 |
| 6 | Workflow Actions | WORKFLOWS Phase 4 | Sprint 5 |
| 7 | Context & Templates | WORKFLOWS Phases 5-6 | Sprint 6 |
| 8 | Webhooks | HOOK_EXTENSIONS Phase 2 | Sprint 1 |
| 9 | Python Plugins | HOOK_EXTENSIONS Phase 3 | Sprint 1 |
| 10 | Workflow CLI/MCP | WORKFLOWS Phases 7-8 | Sprint 7 |
| 11 | Workflow-Task Integration | TASKS Phases 11-13 | Sprints 3, 7 |
| 12 | Tool Metrics | MCP_PROXY Phase 1 | None |
| 13 | Lazy Init | MCP_PROXY Phase 2 | None |
| 14 | Semantic Search | MCP_PROXY Phase 3 | Sprint 12 |
| 15 | Self-Healing | MCP_PROXY Phases 4-5 | Sprint 14 |
| 16 | Hook Workflow Integration | HOOK_EXTENSIONS Phases 4-5 | Sprints 7, 9 |
| 17 | Testing & Recovery | WORKFLOWS Phases 9-11 | Sprint 10 |
| 18 | Documentation | All Plans | All |

---

## Parallel Tracks

Some sprints can run in parallel if multiple contributors are available:

### Track A: Core Platform
Sprints 1 → 4 → 5 → 6 → 7 → 10 → 17

### Track B: Task System
Sprints 2 → 3 → 11 (joins Track A at Sprint 7)

### Track C: Hook Extensions
Sprints 1 → 8 → 9 → 16 (joins Track A at Sprint 7)

### Track D: MCP Improvements
Sprints 12 → 13 → 14 → 15 (independent, can run anytime)

---

## Milestones

### Milestone 1: "Observable Gobby" (Sprints 1-3)
- WebSocket event streaming
- Full task system with CLI
- **Value**: External tools can monitor sessions, agents can track work

### Milestone 2: "Workflow Engine" (Sprints 4-7)
- Phase-based workflow enforcement
- Tool restrictions and transitions
- Built-in templates
- **Value**: Deterministic agent behavior without prompt engineering

### Milestone 3: "Extensible Gobby" (Sprints 8-9)
- Webhook integrations
- Python plugin system
- **Value**: Infinite customization without forking

### Milestone 4: "Smart MCP Proxy" (Sprints 12-15)
- Tool metrics and recommendations
- Semantic search
- Self-healing fallbacks
- **Value**: Intelligent tool orchestration across MCP servers

### Milestone 5: "Production Ready" (Sprints 16-18)
- Full integration
- Comprehensive testing
- Documentation
- **Value**: Ship it!

---

## Quick Start Recommendations

**If you want immediate value**: Start with Sprint 1 (WebSocket broadcasting) - unlocks real-time monitoring.

**If you want agent productivity**: Start with Sprints 2-3 (Task system) - agents can track and manage work.

**If you want deterministic agents**: Start with Sprints 4-7 (Workflow engine) - enforce plan-act-reflect patterns.

**If you have performance issues**: Start with Sprints 12-13 (Tool metrics + lazy init) - faster startup, better tool selection.
