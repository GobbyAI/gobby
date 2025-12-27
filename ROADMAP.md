# Gobby Implementation Roadmap

## Overview

This document defines the implementation order across all Gobby planning documents. Each phase is designed to deliver standalone value while building toward the complete vision.

## Document References

| Document | Location | Focus |
|----------|----------|-------|
| HOOK_EXTENSIONS | `docs/plans/HOOK_EXTENSIONS.md` | WebSocket events, webhooks, plugins |
| WORKFLOWS | `docs/plans/WORKFLOWS.md` | Phase-based workflow enforcement |
| TASKS | `docs/plans/TASKS.md` | Persistent task tracking system |
| SESSION_TRACKING | `docs/plans/SESSION_TRACKING.md` | Async JSONL processing, multi-CLI message storage |
| MEMORY | `docs/plans/MEMORY.md` | Persistent memory and skill learning |
| MCP_PROXY_IMPROVEMENTS | `docs/plans/MCP_PROXY_IMPROVEMENTS.md` | Tool metrics, semantic search, self-healing |

---

## Implementation Order

```
═══════════════════════════════════════════════════════════════════════════════
                              FOUNDATION LAYER
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 1: Hook Event Broadcasting ✅ COMPLETED                               │
│ HOOK_EXTENSIONS Phase 1                                                      │
│                                                                              │
│ Deliverable: Real-time hook events via WebSocket                            │
│ Dependencies: None (uses existing WebSocket infrastructure)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 2: Core Task System ✅ COMPLETED                                      │
│ TASKS Phases 1-6                                                             │
│                                                                              │
│ Deliverable: Task CRUD, dependencies, ready work detection, git sync        │
│ Dependencies: None (self-contained)                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 3: Task MCP Tools & CLI ✅ COMPLETED                                  │
│ TASKS Phases 7-10                                                            │
│                                                                              │
│ Deliverable: Task management via MCP tools and CLI + gobby-* proxy       │
│ Dependencies: Sprint 2                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 3.5: Task Extensions ✅ COMPLETED                                     │
│ TASKS Phases 9.5-9.9                                                         │
│                                                                              │
│ Deliverable: Compaction, Labels, Maintenance, Import, Stealth Mode          │
│ Dependencies: Sprint 3                                                       │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                              WORKFLOW ENGINE
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 4: Workflow Foundation ✅ COMPLETED                                   │
│ WORKFLOWS Phases 0-2                                                         │
│                                                                              │
│ Deliverable: YAML loader, state manager, core engine                        │
│ Dependencies: None                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 5: Workflow Hook Integration 🔶 PARTIAL                               │
│ WORKFLOWS Phase 3                                                            │
│                                                                              │
│ Deliverable: Workflows evaluate on hook events, tool blocking               │
│ Dependencies: Sprint 4                                                       │
│ Done: session_start, session_end hooks. Pending: prompt_submit, tool hooks  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 6: Workflow Actions ✅ COMPLETED                                      │
│ WORKFLOWS Phase 4                                                            │
│                                                                              │
│ Deliverable: inject_context, capture_artifact, generate_handoff, etc.       │
│ Dependencies: Sprint 5                                                       │
│ Done: All scheduled actions (handoff, state, LLM, todo, mcp)                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7: Context Sources & Templates ✅ COMPLETED                           │
│ WORKFLOWS Phases 5-6                                                         │
│                                                                              │
│ Deliverable: Jinja2 templating, built-in workflow templates                 │
│ Dependencies: Sprint 6                                                       │
│                                                                              │
│ - [x] Jinja2 integration                                                     │
│ - [x] Template engine implementation                                         │
│ - [x] Context sources (previous_session_summary, handoff, artifacts, etc.)  │
│ - [x] LLM-powered generate_handoff action                                    │
│ - [x] Git status and file changes context gathering                          │
│ - [x] All 7 built-in templates (session-handoff, plan-execute, react,       │
│       plan-act-reflect, plan-to-tasks, architect, test-driven)               │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                            SESSION MESSAGE TRACKING
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.1: Session Message Foundation                                       │
│ SESSION_TRACKING Phase 1                                                     │
│                                                                              │
│ Deliverable: Database schema, LocalMessageManager, ParsedMessage dataclass  │
│ Dependencies: None                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.2: Async Message Processor                                          │
│ SESSION_TRACKING Phase 2                                                     │
│                                                                              │
│ Deliverable: SessionMessageProcessor with byte-offset polling, debouncing   │
│ Dependencies: Sprint 7.1                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.3: Session Tracking Integration                                     │
│ SESSION_TRACKING Phases 3-4                                                  │
│                                                                              │
│ Deliverable: Runner/HookManager integration, WebSocket broadcasting         │
│ Dependencies: Sprint 7.2                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.4: Multi-CLI Parsers & API                                          │
│ SESSION_TRACKING Phases 5-6                                                  │
│                                                                              │
│ Deliverable: Gemini/Codex parsers, parser registry, query API, MCP tools    │
│ Dependencies: Sprint 7.3                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                            MEMORY-FIRST AGENTS
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.5: Memory Storage & Operations                                      │
│ MEMORY Phases 1-2                                                            │
│                                                                              │
│ Deliverable: Memory storage layer, remember/recall/forget operations        │
│ Dependencies: None (can start in parallel with workflow sprints)            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.6: Skill Learning                                                   │
│ MEMORY Phases 3-4                                                            │
│                                                                              │
│ Deliverable: Skill extraction from sessions, trigger matching, hook inject  │
│ Dependencies: Sprint 7.5                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.7: Memory MCP Tools & CLI                                           │
│ MEMORY Phases 5-6                                                            │
│                                                                              │
│ Deliverable: Full MCP tool suite, CLI commands for memory/skill management  │
│ Dependencies: Sprint 7.6                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Sprint 7.8: Memory Git Sync & Enhancements                                   │
│ MEMORY Phases 7-10                                                           │
│                                                                              │
│ Deliverable: JSONL sync, semantic search, auto-extraction, documentation    │
│ Dependencies: Sprint 7.7                                                     │
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

| Sprint | Focus | Plan Reference | Dependencies | Status |
|--------|-------|----------------|--------------|--------|
| 1 | WebSocket Broadcasting | HOOK_EXTENSIONS Phase 1 | None | ✅ Completed |
| 2 | Core Task System | TASKS Phases 1-6 | None | ✅ Completed |
| 3 | Task MCP/CLI | TASKS Phases 7-10 | Sprint 2 | ✅ Completed |
| 3.5 | Task Extensions | TASKS Phases 9.5-9.9 | Sprint 3 | ✅ Completed |
| 4 | Workflow Foundation | WORKFLOWS Phases 0-2 | None | ✅ Completed |
| 5 | Workflow Hooks | WORKFLOWS Phase 3 | Sprint 4 | 🔶 Partial (session lifecycle) |
| 6 | Workflow Actions | WORKFLOWS Phase 4 | Sprint 5 | 🔶 Partial (handoff actions) |
| 7 | Context & Templates | WORKFLOWS Phases 5-6 | Sprint 6 | ✅ Completed |
| 7.1 | Session Message Foundation | SESSION_TRACKING Phase 1 | None | ✅ Completed |
| 7.2 | Async Message Processor | SESSION_TRACKING Phase 2 | Sprint 7.1 | ✅ Completed |
| 7.3 | Session Tracking Integration | SESSION_TRACKING Phases 3-4 | Sprint 7.2 | ✅ Completed (Perf deferred) |
| 7.4 | Multi-CLI Parsers & API | SESSION_TRACKING Phases 5-6 | Sprint 7.3 | 🔶 Partial (Parsers complete) |
| 7.5 | Memory Storage & Operations | MEMORY Phases 1-2 | Sprint 7.4 | Pending |
| 7.6 | Skill Learning | MEMORY Phases 3-4 | Sprint 7.5 | Pending |
| 7.7 | Memory MCP/CLI | MEMORY Phases 5-6 | Sprint 7.6 | Pending |
| 7.8 | Memory Sync & Enhancements | MEMORY Phases 7-10 | Sprint 7.7 | Pending |
| 8 | Webhooks | HOOK_EXTENSIONS Phase 2 | Sprint 1 | Pending |
| 9 | Python Plugins | HOOK_EXTENSIONS Phase 3 | Sprint 1 | Pending |
| 10 | Workflow CLI/MCP | WORKFLOWS Phases 7-8 | Sprint 7 | Pending |
| 11 | Workflow-Task Integration | TASKS Phases 11-13 | Sprints 3, 7 | Pending |
| 12 | Tool Metrics | MCP_PROXY Phase 1 | None | Pending |
| 13 | Lazy Init | MCP_PROXY Phase 2 | None | Pending |
| 14 | Semantic Search | MCP_PROXY Phase 3 | Sprint 12 | Pending |
| 15 | Self-Healing | MCP_PROXY Phases 4-5 | Sprint 14 | Pending |
| 16 | Hook Workflow Integration | HOOK_EXTENSIONS Phases 4-5 | Sprints 7, 9 | Pending |
| 17 | Testing & Recovery | WORKFLOWS Phases 9-11 | Sprint 10 | Pending |
| 18 | Documentation | All Plans | All | Pending |

---

## Parallel Tracks

Some sprints can run in parallel if multiple contributors are available:

### Track A: Core Platform

Sprints 1 → 4 → 5 → 6 → 7 → 10 → 17

### Track B: Task System

Sprints 2 → 3 → 3.5 → 11 (joins Track A at Sprint 7)

### Track C: Hook Extensions

Sprints 1 → 8 → 9 → 16 (joins Track A at Sprint 7)

### Track D: MCP Improvements

Sprints 12 → 13 → 14 → 15 (independent, can run anytime)

### Track E: Session & Memory

Sprints 7.1 → 7.2 → 7.3 → 7.4 → 7.5 → 7.6 → 7.7 → 7.8 (Session Tracking feeds Memory System)

---

## Milestones

### Milestone 1: "Observable Gobby" (Sprints 1-3) ✅ COMPLETE

- WebSocket event streaming
- Full task system with CLI
- **Value**: External tools can monitor sessions, agents can track work

### Milestone 2: "Workflow Engine" (Sprints 4-7) ✅ COMPLETE

- [x] Workflow foundation (loader, state manager, engine)
- [x] Session lifecycle hooks (session_start, session_end)
- [x] Handoff actions (find_parent, restore_context, generate_handoff)
- [x] LLM-powered session summaries with context handoff
- [x] Context sources (previous_session_summary, handoff, artifacts, observations, workflow_state)
- [x] Jinja2 templating for context injection
- [x] All 7 built-in templates (session-handoff, plan-execute, react, plan-act-reflect, plan-to-tasks, architect, test-driven)
- **Value**: Complete workflow templating system ready for phase-based enforcement
- **Next**: Tool hooks and phase-based restrictions (Sprint 10: Workflow CLI/MCP)

### Milestone 2.5: "Session Recording" (Sprints 7.1-7.4)

- Async JSONL message processing for all CLIs
- Multi-CLI parsers (Claude, Gemini, Codex, Antigravity)
- Real-time WebSocket message streaming
- Message search and query API
- **Value**: Full conversation history for memory, analytics, and debugging

### Milestone 3: "Memory-First Agents" (Sprints 7.5-7.8)

- Persistent memory across sessions
- Skill learning from trajectories
- Automatic context injection
- Cross-CLI memory sharing
- **Value**: Agents that learn and remember like coworkers, not contractors

### Milestone 4: "Extensible Gobby" (Sprints 8-9)

- Webhook integrations
- Python plugin system
- **Value**: Infinite customization without forking

### Milestone 5: "Smart MCP Proxy" (Sprints 12-15)

- Tool metrics and recommendations
- Semantic search
- Self-healing fallbacks
- **Value**: Intelligent tool orchestration across MCP servers

### Milestone 6: "Production Ready" (Sprints 16-18)

- Full integration
- Comprehensive testing
- Documentation
- **Value**: Ship it!

---

## Quick Start Recommendations

**If you want immediate value**: Start with Sprint 1 (WebSocket broadcasting) - unlocks real-time monitoring.

**If you want agent productivity**: Start with Sprints 2-3 (Task system) - agents can track and manage work.

**If you want deterministic agents**: Start with Sprints 4-7 (Workflow engine) - enforce plan-act-reflect patterns.

**If you want learning agents**: Start with Sprints 7.5-7.8 (Memory system) - agents that remember and improve.

**If you have performance issues**: Start with Sprints 12-13 (Tool metrics + lazy init) - faster startup, better tool selection.
