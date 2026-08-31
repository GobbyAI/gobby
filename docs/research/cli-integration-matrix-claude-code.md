# CLI Integration Readiness — Claude Code take

**Author:** Claude Code (Opus 4.8, 1M). **Date:** 2026-06-27.
**Status:** one of several independent per-CLI research outputs, written for the
user to synthesize and corrected 2026-08-30 for the AGY 1.1.18+ contract. Scope:
which coding CLIs Gobby doesn't yet support but could, and a three-surface readiness gate.

**Epistemic note (read first):** this doc is *strongest* where I read the Gobby
source directly — the integration contract, the baseline rows, and the updated AGY
diagnosis are **codebase-verified**. The external
candidate findings are **web research (directional)**: capability *existence*
signals are reliable, but specific version numbers, GA/EOL dates, and
acquisition/rebrand claims should be re-verified against official docs during
synthesis. Where my web pass disagrees with another CLI's output, I flag it.

---

## What FULL support actually requires (codebase-verified)

Gobby integrates a CLI across **three surfaces**. A CLI is only "supported" when
all three exist; anything less remains limbo or blocked.

| Surface | What it does | Where it lives |
| --- | --- | --- |
| **Hook adapter** | Lifecycle interception — rule enforcement, context injection, tool gating. | `adapters/<cli>.py` (+ `<cli>_contract.py`), `adapters/capabilities.py`, `install/<cli>/hooks-template.json` |
| **Transcript parser** | Session history (messages, tool calls, tokens) from a parseable on-disk format. | `sessions/transcripts/<cli>.py`, registered in `PARSER_REGISTRY` (`sessions/transcripts/__init__.py`) |
| **Web-chat backend** | Daemon-hosted streaming to the web UI. | `servers/websocket/chat/backends/<cli>.py` |

**ACP is a useful readiness lever, not a requirement.** The web-chat surface has
a shared `ACPWebChatBackend` (`servers/websocket/chat/backends/acp.py`).
ACP-speaking CLIs reuse it via a thin wrapper — `backends/grok.py` (1.1 KB) and
`backends/qwen.py` (1.9 KB) are stubs over the shared backend, whereas the
non-ACP backends are large and custom: `codex.py` (26 KB), `droid.py` (30 KB),
`claude.py` + helpers. AGY now adds a bounded custom stream-json backend to that
second category.

Supporting plumbing (needed for a complete integration, not a gate cell):
`SessionSource` enum value (`hooks/events.py:75`), adapter registration
(`adapters/__init__.py`), a `ProviderCapabilities` entry
(`adapters/capabilities.py`), ghook install/detection, and context-window
resolution.

Adapter base classes: `BaseAdapter` (`adapters/base.py:131`) for custom hook
protocols; `ACPHookAdapter` (`adapters/acp_hook_adapter.py:53`) for the ACP hook
*translation* plumbing. **Subclassing `ACPHookAdapter` is an internal code-reuse
choice and does NOT mean the CLI speaks ACP as a server. AGY does not expose ACP;
its stable custom stream-json subprocess transport satisfies web chat instead.

## The readiness gate

Score each candidate on the three surfaces. **GREEN** overall requires all three:

- **(A) Hook adapter** — emits lifecycle hooks (≥ session start/end, before/after
  tool, stop) with decision control (allow/deny/modify/ask) and a context-injection
  channel.
- **(B) Transcript parser** — writes a parseable on-disk transcript (JSONL reuses
  the base parser; SQLite/protobuf/custom need a dedicated parser).
- **(C) Web-chat backend** — speaks ACP (reuse `ACPWebChatBackend`) or budget a
  custom subprocess backend.

Two-of-three = **limbo** (don't ship as supported). One-of-three where the rest
are *unavailable upstream* = **blocked** until a later provider version or newly
verified transport changes the contract.

Secondary signals (affect priority, not the gate): MCP client support, OSS/license,
maturity & EOL risk, platform coverage, and **whether the hook protocol is
Claude-compatible** (cheapest adapter to write).

---

## AGY: versioned recovery from the cautionary baseline

The old classification described AGY 1.0.11: five hooks, opaque protobuf payloads,
and no stable daemon transport. It was correct for that capture and stale for the
supported version.

- **(A) Hook adapter — FULL.** AGY exposes exactly five PascalCase events:
  `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, and `Stop`.
- **(B) Transcript parser — FULL.** AGY 1.1.18+ persists parseable JSONL at
  `brain/<id>/.system_generated/logs/transcript_full.jsonl`; Gobby registers a
  dedicated AGY parser.
- **(C) Web-chat backend — FULL.** `AgyWebChatBackend` uses AGY's stable
  `--input-format stream-json` transport. ACP was never required.

**Lesson:** preserve versioned negative evidence, then re-probe it at the supported
version. A stable custom subprocess protocol is a complete web-chat surface even
when the provider does not expose an ACP server.

| Classification | CLI | Verified basis |
| --- | --- | --- |
| **Supported** | **agy / Antigravity** | 1.1.18+: hooks.json dispatch, JSONL transcripts, `--input-format stream-json` transport; no ACP required |

---

## Matrix — baseline (already integrated, codebase-verified)

| CLI | (A) Hook adapter | (B) Transcript | (C) Web-chat | Status |
| --- | --- | --- | --- | --- |
| Claude Code | Full (reference impl) | JSONL parser | Custom (`claude.py`) | **FULL** |
| Codex | Full | JSONL parser | Custom (`codex.py`) | **FULL** |
| Droid | Full | JSONL parser | Custom (`droid.py`) | **FULL** |
| Grok | Full (real ACP event vocab) | JSONL parser | ACP (`grok.py` stub) | **FULL** |
| Qwen | Full (real ACP event vocab) | JSONL parser | ACP (`qwen.py` stub) | **FULL** |
| **agy / Antigravity** | Full (5 events; 1.1.18 floor) | JSONL parser | Custom stream-json (`AgyWebChatBackend`) | **FULL** |

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
live probe. Before scoring any candidate's (B) cell as GREEN, run or update a probe
instead of trusting docs — AGY's 1.0.11 protobuf capture and 1.1.18 JSONL recovery
show why observations must remain versioned. Per-surface checks:

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
6. **AGY:** supported on the 1.1.18+ floor through hooks.json, JSONL transcripts,
   and a custom stream-json backend; no ACP server is required.
