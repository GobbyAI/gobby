# CLI Integration Readiness — Claude Code take

**Author:** Claude Code (Opus 4.8, 1M). **Date:** 2026-06-27.
**Status:** one of several independent per-CLI research outputs, written for the
user to synthesize. Scope: which coding CLIs Gobby doesn't yet support but could,
and a readiness gate so we never ship another partial-support CLI like agy.

**Epistemic note (read first):** this doc is *strongest* where I read the Gobby
source directly — the integration contract, the baseline rows, and the agy
diagnosis are **codebase-verified** with `file:line` anchors. The external
candidate findings are **web research (directional)**: capability *existence*
signals are reliable, but specific version numbers, GA/EOL dates, and
acquisition/rebrand claims should be re-verified against official docs during
synthesis. Where my web pass disagrees with another CLI's output, I flag it.

---

## What FULL support actually requires (codebase-verified)

Gobby integrates a CLI across **three surfaces**. A CLI is only "supported" when
all three exist; anything less is the limbo agy sits in.

| Surface | What it does | Where it lives |
| --- | --- | --- |
| **Hook adapter** | Lifecycle interception — rule enforcement, context injection, tool gating. | `adapters/<cli>.py` (+ `<cli>_contract.py`), `adapters/capabilities.py`, `install/<cli>/hooks-template.json` |
| **Transcript parser** | Session history (messages, tool calls, tokens) from a parseable on-disk format. | `sessions/transcripts/<cli>.py`, registered in `PARSER_REGISTRY` (`sessions/transcripts/__init__.py`) |
| **Web-chat backend** | Daemon-hosted streaming to the web UI. | `servers/websocket/chat/backends/<cli>.py` |

**The single biggest readiness lever is ACP.** I verified the web-chat surface
has a shared `ACPWebChatBackend` (`servers/websocket/chat/backends/acp.py:36`).
ACP-speaking CLIs reuse it via a thin wrapper — `backends/grok.py` (1.1 KB) and
`backends/qwen.py` (1.9 KB) are stubs over the shared backend, whereas the
non-ACP backends are large and custom: `codex.py` (26 KB), `droid.py` (30 KB),
`claude.py` + helpers. So: **ACP → cheap third surface; non-ACP → expensive custom
backend.**

Supporting plumbing (needed for a complete integration, not a gate cell):
`SessionSource` enum value (`hooks/events.py:75`), adapter registration
(`adapters/__init__.py`), a `ProviderCapabilities` entry
(`adapters/capabilities.py`), ghook install/detection, and context-window
resolution.

Adapter base classes: `BaseAdapter` (`adapters/base.py:131`) for custom hook
protocols; `ACPHookAdapter` (`adapters/acp_hook_adapter.py:53`) for the ACP hook
*translation* plumbing. **Subclassing `ACPHookAdapter` is an internal code-reuse
choice and does NOT mean the CLI speaks ACP as a server** — agy proves this (below).

## The readiness gate ("don't ship another agy")

Score each candidate on the three surfaces. **GREEN** overall requires all three:

- **(A) Hook adapter** — emits lifecycle hooks (≥ session start/end, before/after
  tool, stop) with decision control (allow/deny/modify/ask) and a context-injection
  channel.
- **(B) Transcript parser** — writes a parseable on-disk transcript (JSONL reuses
  the base parser; SQLite/protobuf/custom need a dedicated parser).
- **(C) Web-chat backend** — speaks ACP (reuse `ACPWebChatBackend`) or budget a
  custom subprocess backend.

Two-of-three = **limbo** (don't ship as supported). One-of-three where the rest
are *unavailable upstream* = **blocked** — the agy condition: no amount of Gobby
code fixes it, you wait on the vendor.

Secondary signals (affect priority, not the gate): MCP client support, OSS/license,
maturity & EOL risk, platform coverage, and **whether the hook protocol is
Claude-compatible** (cheapest adapter to write).

---

## agy: the cautionary baseline (codebase-verified, strongest finding)

I independently confirmed agy's identity and its exact blocker from the Gobby
source — not from another model's output.

- **agy = Google Antigravity CLI** (Gemini-family). The contract-probe fixture
  `tests/fixtures/provider_contracts/agy/transcript-manifest.json` (CLI v1.0.10,
  probed 2026-05-22) shows paths under `~/.gemini/antigravity-cli/...`, a
  `~/.gemini/config/projects/<PROJECT_ID>.json` config, and model label
  "Gemini 3.5 Flash". (Gemini CLI is being sunset; Antigravity is its successor —
  worth re-verifying the transition dates from Google's own changelog.)
- **(A) Hook adapter — PARTIAL.** `AGY_EVENT_MAP` (verified via
  `tests/adapters/test_agy_contract.py`) is exactly five events:
  `PreInvocation→BEFORE_AGENT`, `PreToolUse→BEFORE_TOOL`,
  `PostToolUse→AFTER_TOOL`, `PostInvocation→AFTER_AGENT`, `Stop→STOP`.
  **No `SESSION_START`/`SESSION_END`.** `translate_from_hook_response`
  (`adapters/agy.py`) honors only `PreToolUse` decisions; its own docstring says
  other AGY hook stdout "is currently ignored."
- **(B) Transcript parser — BLOCKED (the precise reason).** agy is *not* in
  `PARSER_REGISTRY` (which has only claude/grok/qwen/codex/droid), so it silently
  falls back to the Claude parser. But the deeper blocker is the format: the probe
  shows conversation payloads are **opaque binary protobuf** at
  `~/.gemini/antigravity-cli/conversations/<id>.pb` (`content_committed: false`,
  reason: "Binary protobuf payload may contain private transcript data"). There
  *is* a JSON conversation index at `~/.gemini/antigravity-cli/cache/
  last_conversations.json` (so session *discovery* works), but the **payload is
  undocumented protobuf** — a parser would mean brittle reverse-engineering.
- **(C) Web-chat backend — BLOCKED.** No `backends/agy.py`. agy is not an ACP
  *server* (subclassing `ACPHookAdapter` is internal plumbing only), so the cheap
  ACP path is unavailable and a custom backend has nothing stable to consume.

**Lesson:** agy is hook-only and **upstream-blocked** — (B) and (C) can't be built
in Gobby until Antigravity ships a documented transcript format and/or an ACP
server. This is *why* the gate exists: verify all three surfaces are *buildable*
before integrating, or you inherit agy's limbo with no in-house fix.

---

## Matrix — baseline (already integrated, codebase-verified)

| CLI | (A) Hook adapter | (B) Transcript | (C) Web-chat | Status |
| --- | --- | --- | --- | --- |
| Claude Code | Full (reference impl) | JSONL parser | Custom (`claude.py`) | **FULL** |
| Codex | Full | JSONL parser | Custom (`codex.py`) | **FULL** |
| Droid | Full | JSONL parser | Custom (`droid.py`) | **FULL** |
| Grok | Full (real ACP event vocab) | JSONL parser | ACP (`grok.py` stub) | **FULL** |
| Qwen | Full (real ACP event vocab) | JSONL parser | ACP (`qwen.py` stub) | **FULL** |
| **agy / Antigravity** | Partial (5 events, no session start/end, responses mostly ignored) | **Blocked** (binary protobuf, no parser) | **Blocked** (no ACP server) | **BLOCKED — hook-only** |

## Matrix — candidates (web research, directional)

Confidence is explicit. The decisive (C) question is **"does it speak ACP?"** —
my web pass under-weighted this; cross-check candidate ACP support against the
ACP agent registry during synthesis.

| CLI | (A) Hooks | (B) Transcripts | (C) Web-chat | OSS | My verdict | Confidence |
| --- | --- | --- | --- | --- | --- | --- |
| **Cursor CLI** | Hooks documented; verify event parity and payloads | Yes (`~/.cursor`; `transcript_path` on hook payload) | ACP reported; verify | No | **Promising; verify hooks/ACP** | Med |
| **GitHub Copilot CLI** | Full, Claude-compatible payloads (cheapest adapter) | Yes (`events.jsonl` + SQLite; `transcriptPath` on payload) | ACP (preview) — *verify* | No | **Strong; verify (C)** | Med |
| **OpenCode** | **Pending** (Claude-hooks compat in progress) | Yes (SQLite/ORM — needs custom parser) | ACP | MIT (yes) | **Best OSS, but hooks gap** | Med |
| **Goose** | Yes (plugin hooks) | SQLite `sessions.db` (custom parser) | ACP | Apache (yes) | **OSS local-LLM option** | Med |
| **Cline** | **My pass: none found** (others report `PreToolUse`) | `--json` stream; persistence unverified | ACP (reported) | Apache (yes) | **Verify hooks before trusting** | Low |
| **Aider** | None (no tool-use loop to intercept) | Markdown `.aider.chat.md` + git | No ACP | Apache (yes) | **Skip — wrong architecture** | High |
| **Windsurf / Cascade** | Existed; **mid-pivot/EOL risk** | Unclear post-pivot | No ACP confirmed | No | **Defer** | Low-Med |
| **Open Interpreter / gptme** | Hooks + MCP (local-LLM) | Unverified | Unverified | Apache/MIT | **Defer; verify (B)/(C)** | Low |

### Cross-CLI discrepancy to resolve in synthesis
**Kiro CLI (Amazon Q successor).** My sub-agent found **no CLI hooks** and unclear
streaming, and flagged EOL concerns → I scored it a poor fit. Another CLI's output
scored Kiro **Ready**, citing a `kiro.dev/docs/cli/hooks/` hooks page and ACP
support. These can't both be current — **tiebreak by fetching the Kiro CLI hooks
and ACP docs directly** before trusting that row either way.

---

## Per-candidate notes (sources)

- **Cursor CLI** — documented hooks system, headless `--print` with `text`/`json`/
`stream-json`, MCP client, JSONL/SQLite session storage under `~/.cursor`.
Proprietary (so is Claude Code — not a blocker for Gobby). Could cover all three
if hook payloads and ACP are confirmed. Sources: `cursor.com/docs/hooks`,
`cursor.com/docs/cli/headless`.
- **GitHub Copilot CLI** — hooks with Claude-compatible payload shapes (makes the
  adapter close to a Claude clone), `~/.copilot/session-state/` `events.jsonl` +
  SQLite, MCP client. My pass could not confirm a true live-stream output mode
  (JSON output exists; streaming was an open question) — if ACP preview is real,
  that resolves (C). Sources: `docs.github.com/en/copilot/reference/hooks-reference`.
- **OpenCode** — fully OSS (MIT), SQLite/ORM session store (custom parser, not
  JSONL), MCP, ACP. Lifecycle hooks were **not** confirmed in my pass (compat in
  progress). Best OSS candidate once hooks land. Source: `opencode.ai`, GitHub.
- **Goose** — Apache-2.0, SQLite `sessions.db`, plugin hooks, MCP-rich, local-LLM
  via Ollama. The transcript parser is the custom work. Source: `block.github.io/goose`.
- **Cline** — Apache-2.0, large user base, `--json` NDJSON stream, `cline history`.
  Hooks and on-disk transcript persistence both need direct verification.
  Source: `docs.cline.bot`.
- **Aider** — no hook/tool-interception surface; markdown chat history + git
  commits; no ACP. Gobby's model (hook enforcement, transcript capture, web-chat)
  doesn't map onto Aider's edit-commit design. Source: `aider.chat/docs`.
- **Windsurf** — Cascade hooks existed but the product is mid-pivot; no confirmed
  ACP server; transcript story unclear post-pivot. Defer until it stabilizes.

---

## Verification mechanism (use this, don't guess)

Gobby already has a **contract-probe harness** at
`tests/fixtures/provider_contracts/` (currently `agy` and `grok`, plus a README).
It captures a CLI's real config paths, transcript locations, and formats from a
live probe. Before scoring any candidate's (B) cell as GREEN, run/author a probe
for it rather than trusting docs — that's exactly how agy's protobuf blocker was
caught. Per-surface checks:

1. **(A) Hooks** — confirm session-start + pre-tool-use (with allow/deny/modify)
   + stop events; note config path/format and Claude-compatibility; note
   fail-open vs fail-closed.
2. **(B) Transcript** — confirm a *parseable* on-disk format (JSONL/SQLite/
   protobuf/markdown) and whether the path is discoverable from a hook payload or
   env var. Protobuf/markdown = expensive; JSONL = base-parser reuse.
3. **(C) Web-chat** — confirm an ACP *server* (cheap, reuse `ACPWebChatBackend`)
   or budget a custom subprocess backend.

## Recommendation (my independent ranking)

1. **Cursor CLI** — promising all-around; confirm hook payloads, ACP server, and
transcript format before building.
2. **GitHub Copilot CLI** — cheapest hook adapter (Claude-compatible); gate on
   confirming the web-chat/ACP surface.
3. **OpenCode** — best OSS bet; blocked until lifecycle hooks ship (watch upstream).
4. **Goose** — OSS local-LLM option; budget a SQLite transcript parser.
5. **Defer/skip:** Aider (architecture mismatch), Windsurf (pivot/EOL), Open
   Interpreter/gptme (unverified B/C), Cline (verify hooks first).
6. **agy:** leave as blocked; revisit only if Antigravity documents its transcript
   format or ships an ACP server.
