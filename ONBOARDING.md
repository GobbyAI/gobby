# Welcome to Gobby

## How We Use Claude

Based on my usage over the last 30 days:

Work Type Breakdown:
  Plan Design     ████████████░░░░░░░░  64%
  Build Feature   ███░░░░░░░░░░░░░░░░░░  13%
  Write Docs      ██░░░░░░░░░░░░░░░░░░░  12%
  Analyze Data    ██░░░░░░░░░░░░░░░░░░░  11%

Top Skills & Commands:
  /compact  ████████████████████  85x/month
  /gobby    █████████░░░░░░░░░░░░  37x/month
  /bridge   ███░░░░░░░░░░░░░░░░░░  13x/month
  /mcp      █░░░░░░░░░░░░░░░░░░░░   6x/month
  /usage    █░░░░░░░░░░░░░░░░░░░░   4x/month

Top MCP Servers:
  gobby                   ████████████████████  6188 calls
  claude_ai_Gmail         █░░░░░░░░░░░░░░░░░░░░     3 calls
  claude_ai_Google_Drive  █░░░░░░░░░░░░░░░░░░░░     2 calls

## Your Setup Checklist

### Codebases
- [ ] gobby — https://github.com/gobbyai/gobby
- [ ] gobby-cli — https://github.com/gobbyai/gobby-cli
- [ ] gobby-web — https://github.com/gobbyai/gobby-web

### MCP Servers to Activate
- [ ] gobby — The local-first Gobby daemon: MCP proxy with progressive discovery, task management, sessions, memory, and code search. This is the workhorse (6000+ calls/month). Run the daemon locally with `uv run gobby start` and connect via the bundled MCP config.
- [ ] claude_ai_Gmail — Gmail access through claude.ai connectors. Activate via the claude.ai connector settings (used occasionally for email lookups).
- [ ] claude_ai_Google_Drive — Google Drive access through claude.ai connectors. Activate via the claude.ai connector settings (used occasionally for docs).

### Skills to Know About
- [ ] /gobby — Router for Gobby's installed skills and `/gobby help`. The main entry point for Gobby workflows (planning, building, tasks).
- [ ] /bridge — Setup Drawbridge MCP and process UI annotation tasks.
- [ ] /compact — Compact the conversation to keep context lean on long sessions (the team's most-used command).
- [ ] /loop — Run a prompt or slash command on a recurring interval (e.g. watch CI every 15m and fix issues until it passes).
- [ ] /effort — Adjust reasoning effort for the task at hand.

## Team Tips

_TODO_

## Get Started

_TODO_

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
