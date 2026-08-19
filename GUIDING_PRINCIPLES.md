# Guiding Principles — the Why

The enforceable rules live in [AGENTS.md](AGENTS.md) (Working Rules) and are backed by
hooks and the rule engine. This document is the rationale: why each rule exists, and
what failure it prevents. Read this when a rule feels arbitrary; change behavior by
changing the rule and its enforcement, not this narrative.

## Progressive tool discovery

An MCP registry can expose hundreds of tools. Loading every schema up front floods the
context window before work starts, and stale schemas cause malformed calls. Lease-based
discovery keeps context small and current: schemas enter context only when needed, and
the proxy re-serves a schema whenever a call arrives without a valid lease. The
step-per-tool design exists because collapsing discovery into `call_tool` would defeat
the proxy's validation layer.

## Tasks, attribution, and close gates

Every edit belongs to a task so that three things stay true: work is traceable to a
commit, concurrent sessions can share a worktree without trampling each other (file
attribution is how ownership is resolved), and closing requires evidence rather than
assertion. The close checklist — linked commit, clean worktree, validation visible in
the transcript, bounded criteria review — replaced an honor system that let "done"
drift from "verified done". The stop hook holds a turn open while a task is claimed
because abandoned claims used to strand half-finished work invisibly.

## You found it, you fix it

Deferral is the failure mode this rule exists to kill: a finding filed as a task is a
finding someone else must rediscover with less context than you have right now. Fixing
in-session is cheaper for the system even when it feels slower for the session. The
single exclusion — another session's uncommitted files — exists because destroying
in-flight work is strictly worse than deferring; messaging the owner keeps the finding
alive without the destruction. Only the user can approve a deferral because the person
paying for the debt should be the one signing for it.

## The monolith ceiling

A 1,000-line hand-maintained file is where review quality collapses and merge
conflicts concentrate. The ceiling is enforced at write time, and decomposition must
finish in the same session because deferred "split this file later" tasks historically
never ran — the threshold-crossing session has the context to do it safely; a later
session does not.

## Decision-complete plans

A plan with open questions delegates thinking to the implementing agent, which sees
only its own slice and guesses. Exploration belongs before plan finalization, so every
section hands an implementer everything needed to act without re-deriving intent.

## Least mechanism, whole problem

Correctness and completeness are not negotiable; among complete solutions, the one
with the least unjustified mechanism wins. Both halves matter: shortcuts that dodge
root causes create rediscovery work (see "you found it, you fix it"), while
speculative abstraction creates maintenance surface nobody asked for. Complexity must
earn its place — so must every line of code.

## Templates are not live config

Bundled templates sync into DB registry tables, and the DB is what the daemon actually
evaluates. The split exists so users can toggle and override behavior without editing
shipped files, and so upgrades can refresh definitions without destroying user intent.
The corollary trips people constantly: editing a template YAML changes nothing until
sync runs, and reading a template tells you what would be installed, not what is
active.

## Prefer gcode

The code index returns ranked, symbol-level results at a fraction of the token cost of
raw grep, and it understands structure (outlines, usages, blast radius) that text
search cannot. Hooks redirect grep/rg because habit otherwise wins over economics.

## No backward compatibility

0.5.0 has not shipped. Compatibility shims written before a first release protect
nobody and cost forever — the moment to delete them is before anyone depends on them.

## Agent depth and messaging discipline

The depth limit (5) bounds runaway recursive spawning — each level multiplies cost and
halves observability. Cross-session communication goes through
`gobby-agents:send_message` because it is attributed and durable;
`gobby-sessions:send_keys` types into a live terminal and is reserved for terminal
control, where impersonating a message channel would be both fragile and invisible to
the audit trail.
