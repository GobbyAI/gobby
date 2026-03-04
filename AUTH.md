# Authentication & AI Vendor Policies

## How Gobby handles authentication

Gobby is a local-first daemon. It does not intercept, store, or proxy OAuth
tokens from your AI CLI tools.

### CLI hook integration (Claude Code, Gemini CLI, Cursor, etc.)

- Hooks communicate via HTTP to the local daemon
- Each CLI manages its own authentication independently
- Gobby never sees or touches CLI OAuth tokens or session credentials

### Web chat & workflow agents (Claude Agent SDK)

- Gobby uses the Claude Agent SDK for its web chat and workflow agents
- The SDK defaults to Claude's subscription mode (personal use)
- Gobby never directly interacts with Claude's OAuth tokens — it uses
  the SDK as designed, which handles authentication transparently
- For production/commercial use, we recommend configuring API keys
  (see below)

### API keys & secrets storage

- Gobby can store API keys (ANTHROPIC_API_KEY, GEMINI_API_KEY,
  OPENAI_API_KEY, etc.) in its encrypted secrets store
- Keys can also be read from environment variables at runtime
- All stored secrets are encrypted at rest using Fernet cipher bound
  to machine ID — Gobby cannot see keys entered by users
- Secret values are resolved internally by the daemon and never
  exposed via API
- MCP server headers can use `$secret:NAME` references to the same
  encrypted store

## Individual vs. commercial use

**For individual/personal use:** The Claude Agent SDK defaults to
subscription mode, which uses your existing Claude subscription. This
works out of the box with no additional configuration.

**For commercial/team/production use:** We recommend following the usage
policies of each AI vendor and using API keys:

- **Anthropic (Claude):** Use ANTHROPIC_API_KEY with your organization's
  API account. The Claude Agent SDK defaults to subscription mode, which
  is intended for individual use. For production workloads, configure
  API key mode. See [Anthropic's usage policy](https://www.anthropic.com/policies).
- **Google (Gemini):** Use GEMINI_API_KEY or Application Default
  Credentials (ADC) with your GCP project. Gemini CLI's terms explicitly
  prohibit using Gemini CLI OAuth tokens in third-party software.
  See [Gemini CLI Terms](https://geminicli.com/docs/resources/tos-privacy/).
- **OpenAI:** Use OPENAI_API_KEY with your organization's API account.

Gobby supports both modes. Set `auth_mode: api_key` per provider in your
daemon configuration, or set the environment variables and Gobby will use
them automatically.

## What Gobby does NOT do

- Does not generate, intercept, or store OAuth tokens from any CLI tool
- Does not proxy authentication requests to AI vendors on your behalf
- Does not send any data to external services unless you explicitly configure
  integrations (MCP servers, webhooks, etc.)

Note: Gobby requires internet access for LLM features (chat, task expansion,
summarization, etc.) as local model support is not yet available.
