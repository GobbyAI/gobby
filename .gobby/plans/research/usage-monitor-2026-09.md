# Usage monitor: integrate, steal or build (2026-09-24)

Researcher gobby#14435. Research only; nothing was changed.

## Request

Josh, relayed by the Assistant (gobby#14069): "We'll need a usage monitor we can
integrate/steal/build, lower priority though."

Trigger: Codex seats kept running on paid credits after the weekly limit hit
100%. Nobody was alerted, and Josh bought a usage reset ("like $700", his
figure). The PD's gap id is 18a47bbb.

Scope: usage and spend across the CLIs Gobby runs, what Gobby already parses,
and existing monitors worth integrating or copying.

Tags:

- **[L]** I verified it locally, from session files on this machine or from
  source at 0.5.0 HEAD.
- **[S]** A subagent read the source: Gobby at HEAD, or upstream on GitHub.
- **[V]** Vendor docs or a project README. "snippet" means the page refused
  the fetch and only a search excerpt was seen.
- **[I]** Inference.

Web pages were read as untrusted data.

## Answer

- **Build it, don't integrate it.** Codex writes the signal that would have
  caught this on every turn, into the rollout files Gobby already parses.
  Gobby throws it away. [L]
- **Smallest complete fix, in order:**
  1. Read `rate_limits` from the Codex `token_count` events Gobby already
     parses.
  2. Send a global alert when the weekly window crosses 90%, and again when it
     reaches 100% while credits are being drawn.
  3. Refuse new Codex spawns while the weekly window is at 100%, unless credits
     are explicitly allowed.

  All three are Python only, with no new dependency and no credential reads.
- **Nothing surveyed is fit to integrate as a runtime dependency.** The one safe
  reuse is `ccusage <agent> daily --json`, run as a subprocess, if dollar
  estimates are wanted. The alert design is worth copying from CodexBar,
  claude-monitor and code-notify.
- **Codex's official limit flags stayed null for the whole incident** on this
  personal Pro seat. A monitor keyed on them would never have fired. [L]
- **It can happen again this week.** The new window opened 09-24 18:53 UTC and
  resets 10-01 18:53. The last window reached 100% in 3.3 days. At that pace
  this one reaches 100% around 09-28, about 3.5 days before its reset, and the
  remaining 1,265.60 credits would last under 2 hours at the 09-22 rate of
  about 850 credits an hour.
  [I]

## What happened: Codex, 09-22 to 09-24 UTC [L]

Source: 23,944 `token_count` rows in 274 rollout files under
`~/.codex/sessions`, from 09-22 05:12 to 09-24 19:03 UTC. A re-check at
20:37 UTC with the same filter found 25,152 rows in 277 files; the extra rows
come from sessions that ran after 19:03. Every row had the same `limit_id`,
plan and window shape, and the flags were null.

| Time (UTC) | Weekly used | Credit balance | Note |
| --- | --- | --- | --- |
| 09-19 08:09 | | | Window opened (reset time minus 7 days) |
| 09-22 05:12 | 83% | 17,657.26 | First row scanned; the balance stays flat below 100% |
| 09-22 12:38 | 95% | 17,657.26 | |
| 09-22 15:59 | 100% | 17,657.26 | Limit hit 3.3 days into the 7-day window; credits start |
| 09-22 23:59 | 100% | 10,835.43 | About 6,822 credits in 8 h |
| 09-23 23:59 | 100% | 3,752.19 | About 7,083 credits that day |
| 09-24 18:53 | 0% | 1,265.60 | Purchased reset; new reset time 10-01 18:53 |

- **Total drawn:** 16,391.66 credits over 50.9 h at 100%. These are credits,
  and I did not convert them to dollars. "Like $700" is Josh's figure for the
  reset.
- **Timing of the reset:** the natural reset was due 09-26 08:09 UTC, so the
  purchase came about 37 h early.
- **The reset was a separate purchase.** The balance was 1,265.60 on both sides
  of 18:53:35, so the reset was not drawn from credits. Codex app-server exposes
  paid resets as `account/rateLimitResetCredit/consume`. [L][S]
- **No auto-reload.** The balance never rose during the window. [L]
- **Now:** at 20:37 UTC the new window stood at 2%, and the balance was
  unchanged at 1,265.60. [L]
- **Load:** 2–21 rollout files started per hour.
- **One account window.** Every row carries `limit_id` `codex` and plan `pro`,
  so every seat on this machine drew from the same weekly window.
- **Data shape on this seat:**
  - The weekly window is `primary` (`window_minutes` 10080), and `secondary` is
    null.
  - `credits.has_credits` is true, `unlimited` is false, and `balance` is a
    string.
  - `rate_limit_reached_type`, `spend_control_reached` and `individual_limit`
    were null in every row. Their non-null values are workspace variants. [S]

### Why nothing fired

- **Codex never stops a turn at the limit on a personal plan.** The TUI warns at
  50, 75, 90 and 95%, then draws credits. [S]
  - There is no config key for this. [V]
  - Personal Plus and Pro have no overage toggle. openai/codex issue #28382,
    filed 2026-06-15, asks for a toggle to stop automatic credit use. It is
    still open, with no maintainer reply. [S]
  - Business workspaces can set per-member credit limits. [V snippet]
- **Both of Gobby's quota detectors fire only once Codex stops.** [L] Codex
  never stopped, so neither matched. [I]
  - The watchdog maps `task_complete.error.codex_error_info ==
    "usage_limit_exceeded"` to `provider_quota_exhausted`
    (`src/gobby/agents/watchdog/codex.py:136-156`).
  - The pane scan matches "You've hit your usage limit"
    (`src/gobby/agents/watchdog/quota.py:10`).

## What Gobby already parses

- **Codex transcripts:** `src/gobby/sessions/transcripts/codex.py:637` accepts
  `token_count` and reads tokens from `info.last_token_usage` or
  `info.total_token_usage` (:640-644). The sibling `rate_limits` is ignored. [L]
  The watchdog classifies the same events live without reading their contents
  (`agents/watchdog/codex.py:130-170`). [S]
- **Limit fields:** `rate_limits`, `used_percent`, `window_minutes`,
  `has_credits`, `spend_control` and `rateLimits` have no functional match in
  `src/`, `crates/` or `web/src`. [S]
- **Raw payloads are dropped:** `token_events.metadata` keeps only
  `content_type` (`sessions/processor_usage.py:146-148`). [S]
- **Pricing:** `storage/model_metadata.py:4` says "Pricing data has been
  removed — tokens are tracked directly." [L] There are no dollar budgets
  anywhere. [S]
- **Capacity is AGY only.** [S]
  - `providers/usage.py:142-194` runs `agy -p /usage --output-format json` and
    writes `provider_capacity_snapshots`.
  - `ProviderCapacityService` has no consumer.
  - `select_next_provider` (`agents/provider_rotation.py:119`) has no callers.
- **Claude:** the installer adds a ghook statusline, but
  `crates/ghook/src/statusline.rs:22-87` only pipes the raw stdin bytes to
  `GOBBY_STATUSLINE_DOWNSTREAM` and returns its output. It never parses the
  JSON, so `rate_limits` is never read. [L]
- **Codex app-server client (web chat):** it sends `account/status`
  (`adapters/codex_impl/client_api.py:477-479`) but handles no rate-limit
  method or notification. [S]
- **Alerting:** none. Communications, rules and pipelines have no usage
  threshold, and nothing checks quota before a spawn. [S]
- **Existing usage surfaces:** `gobby-metrics:get_usage_report`,
  `get_provider_capacity`, `/api/admin/usage`, `gobby tokens stats` and the
  web context meter. They carry token counts only. [S]

## What each CLI exposes

| CLI | Local signal | Stops at the limit? | Overage control |
| --- | --- | --- | --- |
| Codex | Rollout `token_count.rate_limits`: weekly %, credits, flags [L]. App-server `account/rateLimits/read` and `account/rateLimits/updated` [S] | No, it only warns [S] | Personal: none [S]. Business: per-member credit limits [V] |
| Claude Code | Statusline stdin `rate_limits.five_hour/seven_day` (Pro/Max, after the first response) [V]. stream-json `rate_limit_event` [S] | Yes, unless usage credits are on [V] | Usage credits are off by default and can be capped monthly [V]. `--max-budget-usd` in print mode [V] |
| Grok Build | `updates.jsonl` `usage.costUsdTicks` per turn [L] | The subscription pauses at the weekly limit [V] | API: hard spending limit [V] |
| Droid | Session `.settings.json` `tokenUsage.factoryCredits` [L] | Prepaid balance [V] | Extra Usage toggle [V] |
| AGY | `/usage` JSON and statusline quota [V], which Gobby already reads [S] | Yes, when overage is set to Never [V] | Never / Always [V] |
| Qwen | Tokens only [L] | Turn, time and tool-call caps only [V] | None found |
| Gemini | Tokens only [V] | Asks the user | `overageStrategy`, consumer tiers only, which are now cut off [V] |

Local spend readings [L]:

- **Grok:** $244.48 in August and $898.79 in September, grouped by file month.
  - Source: `costUsdTicks` (1e10 ticks = $1), counted once per record because
    `modelUsage` repeats the value.
  - These are API list-price equivalents. The plan type isn't visible locally,
    and I did not read auth files, so this may not be what was billed.
- **Droid:** `factoryCredits` of 18.36M in June, 8.71M in July and 19.33M in
  September, across 221 sessions. These are Factory credits, not dollars.

Admin APIs: neither Anthropic's nor OpenAI's usage API covers Pro/Max or
Plus/Pro subscriptions. [V]

## Existing monitors

| Tool | License / form | How it gets data | Alerting | Verdict |
| --- | --- | --- | --- | --- |
| ccusage | MIT, Rust with npm packaging, v20.0.24 | Local logs only, no credentials. Reads Codex `token_count` tokens but ignores `rate_limits` [S] | None [V] | Integrate only as a subprocess, for dollar estimates |
| CodexBar | MIT, Swift, macOS-first | Reads `~/.codex/auth.json` and calls the undocumented `chatgpt.com/backend-api/wham/usage`. Refreshes tokens with Codex's own client_id [S] | Edge-triggered `quota_low` / `quota_reached` / `quota_reset` hooks, `guard --window weekly` exit gate, `serve` loopback JSON [V] | Steal the alert model; don't integrate |
| claude-monitor | MIT, Python | Statusline capture to `latest.json`: stale after 600 s, a window is dropped after `resets_at`, no network [S] | Exit codes 10 (near) and 11 (hit) [S] | Steal the capture pattern |
| code-notify | MIT, shell | Reads CLI credentials, calls `wham/usage` and `api/oauth/usage`, sends a fake User-Agent [S] | Warns at 20% and 10% remaining, reset reminders, webhooks [S][V] | Steal the ladder only |
| ccstatusline, claude-hud | MIT | Claude statusline (ccstatusline also reads credentials) [S][V] | Colors only | Display only |
| tokscale | MIT | Reads credentials, sends a spoofed browser User-Agent; `submit` uploads to a public leaderboard [S][V] | None found | Avoid |
| splitrail | MIT, Rust | Local logs, dollar estimates [V] | None found | No advantage over ccusage |
| caut | MIT plus a license rider | Rust port of CodexBar [V] | Error exit codes only | Avoid: the rider voids all rights for OpenAI, Anthropic "or any person… acting… on behalf of" them [S] |

None of these can stop a running seat. Gobby spawns the seats, so only Gobby
can. [I]

## Integrate / steal / build

The build order is set by what catches the actual failure, running seats
drawing credits, with the least mechanism.

### Tier 1: catches the $700 failure

1. **Build: read `rate_limits` in the Codex parser.** Put it beside the
   token-count read at `transcripts/codex.py:637`, or in watchdog
   classification.
   - Choose the weekly window by `window_minutes == 10080`, not by
     primary/secondary position. On this seat it is `primary`. [L]
   - Keep the newest non-null snapshot per `limit_id`. Some rows are stale
     readings from older sessions, and some events carry null. [L][S]
   - States:
     - `warn`: `used_percent >= 90`.
     - `drawing_credits`: `used_percent >= 100`, `credits.has_credits` true, and
       the balance lower than the previous reading.
     - `critical`: `rate_limit_reached_type` non-null or `spend_control_reached`
       true. These stayed null here but do fire on workspaces. [S]
   - Persist the latest snapshot. `provider_capacity_snapshots` already has
     `state` and `windows` jsonb and is AGY only today. Reusing it would make
     `get_provider_capacity` show Codex too. [I]
2. **Build: alert on edges, not levels.** Send a `global` `send_message` on
   ok→warn, warn→drawing_credits and reset.
   - Steal CodexBar's edge-triggering against the previous reading and its
     600 s throttle.
   - Steal code-notify's ladder if more rungs are wanted.
3. **Build: a spawn gate.** While the state is `drawing_credits`, refuse or
   reroute Codex spawns unless an explicit allow-credits setting is on.
   - Optionally stop running Codex seats at that edge.
   - Rerouting can reuse the fallback path
     (`mcp_proxy/tools/spawn_agent/_factory.py:478-545`) or the uncalled
     `select_next_provider`. [S]
   - Steal the semantics of `codexbar guard --window weekly`.

### Tier 2: visibility

4. **Idle seats.** Rollout data updates only while a seat runs turns. For a
   fresh reading before a spawn, poll `codex app-server`
   `account/rateLimits/read`.
   - It uses the vendor's binary and auth, so Gobby reads no credentials.
   - Honor `ordinaryUsageAllowed`. The source comment says "clients must not
     infer recovery from percentages or reset times." [S]
   - The method is in the source but not on the public app-server docs page,
     so treat it as unstable. [S]
5. **Claude.** Capture statusline stdin `rate_limits` in ghook. It is a crate
   change, so it needs a rebuild and promotion.
   - Claude already stops at the limit unless usage credits are turned on, so
     this is visibility, not the $700 failure. [V][I]
6. **Dollar estimates.**
   - If Josh wants dollars per CLI, run `ccusage <agent> daily --json` as a
     subprocess.
   - Grok `costUsdTicks` and Droid `factoryCredits` can be read directly. [L]

### Don't

- **Don't call `wham/usage` or `api/oauth/usage`.** Both are undocumented, and
  both would need Gobby to read CLI credentials.
  - Anthropic's legal page says developers may not "collect, store, or
    intermediate Claude.ai credentials or session tokens". [V]
  - OpenAI's terms bar programmatic extraction except through the API.
    [V snippet]
  - Refreshing tokens with Codex's client_id might invalidate the CLI's own
    login. [I]
- **Don't reuse caut's code**, because of its license rider.
- **Don't rely on `rate_limit_reached_type` or `spend_control_reached`
  alone.** [L]

### Stopgap until tier 1 ships

The only control for a personal seat is the credit balance, because there is no
overage switch. Keeping the balance low, or leaving auto-reload off, bounds the
burn. [V snippet][I]

It also makes the failure loud. At zero credits Codex stops, and Gobby's
existing detectors (`usage_limit_exceeded` and the pane text) fire, so silent
spend becomes a visible seat failure. [I]

## Limits

- **Time range:** the timeline covers rollout rows from 09-22 05:12 onward
  only. I did not scan earlier weeks, so I can't say whether earlier windows
  also reached 100%.
- **Dollar amounts:** the request attached "like $700" to the reset purchase.
  The 16,391.66 credits drawn before the reset are a separate cost, which I did
  not convert to dollars.
- **Coverage:** one machine and one account. Codex seats on other machines, if
  any, are not in these files.
- **Grok dollars** are API-equivalent, and the plan is unknown.
- **Upstream claims** ([S] and [V]) are as of 09-24. Some OpenAI help-center
  pages returned 403 and were seen only as search snippets.
- **Claude statusline:** anthropics/claude-code issue #95918 is an open bug
  report that `rate_limits` is missing on v2.1.278.
- **Gobby code refs** are as of 0.5.0 HEAD befc623971.

## Sources

### Local [L]

- `~/.codex/sessions/2026/09/{22,23,24}/rollout-*.jsonl`: `event_msg`
  `token_count` `rate_limits`.
- `~/.grok/sessions/<cwd>/<id>/updates.jsonl`: `usage.costUsdTicks`.
- `~/.factory/sessions/**/*.settings.json`: `tokenUsage.factoryCredits`.
- Gobby source:
  - `src/gobby/sessions/transcripts/codex.py:637-646`
  - `src/gobby/agents/watchdog/codex.py:136-156`
  - `src/gobby/agents/watchdog/quota.py:10`
  - `src/gobby/storage/model_metadata.py:4`
  - `crates/ghook/src/statusline.rs:22-87`

### Gobby source read by a subagent [S]

- `sessions/processor_usage.py:146-148`
- `providers/usage.py:142-194`
- `agents/provider_rotation.py:79-119`
- `mcp_proxy/tools/spawn_agent/_factory.py:478-545`
- `adapters/codex_impl/client_api.py:477-479`
- `mcp_proxy/tools/metrics.py:86-100,333`
- `crates/gcore/assets/schema/baseline.sql`: sessions 3478-3492, token_events
  3970-3987, provider_capacity_snapshots 3085-3096
- `docs/guides/observability.md:231-249,351-367`

### Upstream

- Codex protocol and rollout:
  - https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs
  - https://github.com/openai/codex/blob/main/codex-rs/rollout/src/policy.rs
- Codex app-server:
  - https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/common.rs
  - https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/account.rs
- Codex TUI warnings:
  https://github.com/openai/codex/blob/main/codex-rs/tui/src/chatwidget/rate_limits.rs
- Codex issues:
  - https://github.com/openai/codex/issues/28382
  - https://github.com/openai/codex/issues/18018 (credit accounting bugs)
- OpenAI:
  - https://learn.chatgpt.com/docs/pricing
  - https://help.openai.com/en/articles/12642688
  - https://help.openai.com/en/articles/20001155
  - https://openai.com/policies/row-terms-of-use/
- Claude Code:
  - https://code.claude.com/docs/en/statusline
  - https://code.claude.com/docs/en/costs
  - https://code.claude.com/docs/en/legal-and-compliance
  - https://github.com/anthropics/claude-code/issues/95918
- Grok, Droid and AGY:
  - https://docs.x.ai/developers/cost-tracking
  - https://docs.factory.ai/pricing/individuals
  - https://antigravity.google/docs/plans/
- Monitors:
  - https://github.com/ccusage/ccusage
    ([parser.rs](https://raw.githubusercontent.com/ccusage/ccusage/main/rust/adapters/codex/src/parser.rs))
  - https://github.com/steipete/CodexBar (`docs/configuration.md`, `docs/cli.md`,
    `CodexOAuthUsageFetcher.swift`, `CodexTokenRefresher.swift`)
  - https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
    (`output/official.py`, `output/snapshots.py`)
  - https://github.com/mylee04/code-notify (`lib/code-notify/utils/usage.sh`)
  - https://github.com/sirmalloc/ccstatusline
  - https://github.com/jarrodwatts/claude-hud
  - https://github.com/junhoyeo/tokscale
  - https://github.com/Piebald-AI/splitrail
  - https://github.com/Dicklesworthstone/coding_agent_usage_tracker (LICENSE)
