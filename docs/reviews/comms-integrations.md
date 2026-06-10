# Review: communications + integrations + github_triage

- **Scope:** `src/gobby/communications/` (manager, router, polling, 7 adapters, support modules), `src/gobby/integrations/` (linear_graphql, github_helper, linear, github), `src/gobby/github_triage/` (service, issue_index, cron)
- **Reviewer:** Fable 5 (6 parallel general-purpose reviewers; every Blocker re-verified link-by-link against source by the synthesizer via `gcode grep` / schema reads)
- **Commit / branch:** `1922f2d4a` on `0.5.0`
- **Summary:** 17 Blocker · 30 Important · ~20 Nit — **the communications subsystem is non-functional end-to-end**; integrations and github_triage are each load-bearing-bug-ridden. This is the lowest-health area reviewed in the epic so far.

The headline: with secrets stored the documented way, **no Slack/Discord/Teams/SMS/email channel can initialize**, and even if they could, **outbound messaging fails for every channel except email** because the destination contract is broken. Every failure is reported up the stack as success. github_triage layers a self-perpetuating GitHub-comment-spam loop and a default-accept-and-autonomously-build posture on top.

## Findings

### [BLOCKER] Secret `$secret:` refs are passed to `SecretStore.get`, which only does a bare-name lookup → every secret-backed channel fails to initialize
- **Where:** `communications/manager.py:169` (`await adapter.initialize(channel, self._secret_store.get)`); consumers `adapters/slack.py:72-73`, `adapters/discord.py:87-88`, `adapters/teams.py:66-67`, `adapters/sms.py:69`, `adapters/email.py:134,147`; store `storage/secrets.py:198-208` (`get` calls `_normalize_name` = `name.strip().lower()` then `SELECT ... WHERE name = %s`)
- **Failure mode:** Adapters call the resolver with the full ref, e.g. `secret_resolver("$secret:SLACK_BOT_TOKEN")`. `SecretStore.get` does not strip the `$secret:` prefix, so it looks up a secret literally named `$secret:slack_bot_token`, finds nothing, returns `None`. Slack/Discord/Teams/SMS/email then raise in `initialize` ("SLACK_BOT_TOKEN secret is required", "Could not resolve Discord bot token", etc.). `add_channel` itself writes refs as `config[key] = "$secret:COMMS_..."` — the documented path — so this fires on every secret-backed channel, every startup. Telegram is the lone survivor because it strips the prefix itself (`telegram.py:68-70`).
- **Why it matters:** Five of seven channel types are dead on arrival whenever secrets are stored through the supported mechanism. Unit tests mask it: every adapter test injects its own resolver keyed by the full `$secret:` string, so the suite green-lights the broken contract.
- **Minimal fix:** In `_init_adapter`, pass a ref-aware resolver: `lambda ref: self._secret_store.get(ref.removeprefix("$secret:"))`. Separately, Slack/Teams/SMS hardcode global secret names (`$secret:SLACK_BOT_TOKEN`) and ignore the channel-scoped `config_json` ref that `add_channel` writes — make them read the ref from config like email/discord/telegram. Add one integration test that wires the real `SecretStore.get` through `_init_adapter`.
- **Confidence:** high — `SecretStore.get` body, all five adapter call sites, and the manager wiring line all quoted/verified.

### [BLOCKER] New-identity creation always crashes: empty-string timestamps inserted into `TIMESTAMPTZ NOT NULL` columns
- **Where:** `communications/identities.py:108-121` (`created_at=""`, `updated_at=""`), `storage/communications.py:130-158` (`create_identity` INSERTs them verbatim), schema `storage/postgres_baseline_schema.sql:1567-1568` (`created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`)
- **Failure mode:** First inbound message from any not-yet-known user hits `IdentityManager.resolve_identity`, which builds `CommsIdentity(..., created_at="", updated_at="")` with the comment "Store generates id/created_at/updated_at on insert." The store generates only the `id`; it inserts the empty strings into the NOT NULL TIMESTAMPTZ columns → Postgres error 22007 (invalid datetime). The exception propagates out of the unguarded `resolve_identity` call (`manager.py:471`) and aborts the whole inbound batch; for Telegram polling, `poll()` advances the offset before processing, so those updates are permanently lost.
- **Why it matters:** Mainline inbound crash plus data loss; identities and auto-sessions are never created for any new user. The core inbound contract is dead on first contact.
- **Minimal fix:** In `create_identity`, generate timestamps when falsy (`identity.created_at or datetime.now(UTC).isoformat()`) or omit the columns and let the DB `DEFAULT NOW()` apply; fix the misleading comment. Add a non-mocked integration test driving `resolve_identity` against the real store (current tests mock the store and pass valid timestamps, masking it).
- **Confidence:** high — code, INSERT, and schema all quoted.

### [BLOCKER] Reaction-driven pipeline approval calls methods that do not exist
- **Where:** `communications/reactions.py:115-128` (`pipelines = self._services.pipeline_execution_manager`; `await pipelines.approve_step(run_id, step_id, session_id)` / `reject_step(...)`)
- **Failure mode:** `ServiceContainer.pipeline_execution_manager` is a `LocalPipelineExecutionManager`, which has no `approve_step`/`reject_step` (verified: the only definitions are `PipelineGatekeeper.approve_step`/`reject_step` at `workflows/pipeline/gatekeeper.py:145,181`, which take an approval **token**, not `(run_id, step_id, session_id)`). At runtime: `AttributeError` → swallowed by the blanket `except Exception` at `reactions.py:129-131` → `logger.error("Failed to process approval action...")` after already logging "Executing action approve...". The user who reacted 👍 believes they approved; the pipeline stays `waiting_approval` forever.
- **Why it matters:** The emoji-approval feature is structurally dead and silently violates its contract on every use.
- **Minimal fix:** Persist the approval **token** in `approval_context` when sending the approval prompt, and route through the gatekeeper: `await gatekeeper.approve_step(context["token"], approved_by=identity.id)`. Replace the MagicMock in `test_reactions.py` with `MagicMock(spec=LocalPipelineExecutionManager)` so the missing method fails the test.
- **Confidence:** high — exhaustive grep confirms `approve_step` exists only on the gatekeeper.

### [BLOCKER] Outbound destination contract drift: Slack/SMS/Teams/Discord send to the internal channel UUID → every manager-driven outbound fails
- **Where:** producer `communications/manager.py` `send_message`/`send_attachment` build `CommsMessage(channel_id=channel.id, ...)` (a `uuid4()`); consumers `adapters/slack.py:123,156,180,242`, `adapters/sms.py:111,143`, `adapters/teams.py:108`, `adapters/discord.py:355,392,411` read `message.channel_id` (or `platform_thread_id or message.channel_id`) as the platform destination
- **Failure mode:** The manager injects the real destination as `metadata_json["platform_destination"]` (via `_enrich_outbound_metadata`), but only the email adapter reads it (`email.py:316,358`). Slack POSTs `{"channel": "<gobby-uuid>"}` → `ok:false channel_not_found`; Twilio gets `To=<uuid>` → 400; Teams builds `/v3/conversations/<uuid>/activities` → 404; Discord POSTs to `/channels/<uuid>/messages` → 404. Telegram instead demands `metadata_json["chat_id"]`, which no mainline caller supplies → `ValueError`. Every send is recorded `status="failed"`.
- **Why it matters:** Outbound messaging is 100% broken for every channel except email — across `send_message`, `send_event`, and `send_attachment`. Adapter unit tests hand-craft `channel_id="C12345"`, masking the drift.
- **Minimal fix:** Standardize on `platform_destination` as the canonical adapter-facing destination key, document it on `BaseChannelAdapter`, and migrate every adapter to `message.metadata_json.get("platform_destination") or <config default>`. Teams additionally needs `conversation_reference["service_url"]`, which the manager must inject.
- **Confidence:** high — manager `channel_id=channel.id` and each adapter read verified.

### [BLOCKER] Inbound `channel_id` FK violation: adapters store platform IDs → inbound rows rejected/orphaned for 6 of 7 adapters
- **Where:** `communications/manager.py:457-459` (`if not message.channel_id: message.channel_id = channel.id` — only fixes falsy); adapters set platform-native IDs: `slack.py:319,343`, `discord.py:488,524`, `email.py:455` (`channel_id=sender`), `sms.py:206` (`channel_id=from_number`), `teams.py:273`; schema FK `comms_messages.channel_id REFERENCES comms_channels(id)` (`postgres_baseline_schema.sql:1580`)
- **Failure mode:** Adapters populate `channel_id` with truthy platform identifiers, so the manager's falsy-only heal leaves them; `create_message` then violates the FK → caught per-message and dropped. Only Telegram (sets `""`) survives. Where the value is a real-but-wrong UUID-shaped value it silently orphans the row instead — `list_messages(channel_id=<uuid>)` never returns it.
- **Why it matters:** Total inbound persistence loss (or orphaning) for 6 of 7 adapters on the mainline path; poisons thread/session history and `get_message_by_platform_id`.
- **Minimal fix:** In `handle_inbound_messages`, unconditionally `message.channel_id = channel.id` and stash the platform channel in `metadata_json["platform_channel_id"]`; define the adapter contract as "leave `channel_id` empty."
- **Confidence:** high — FK, falsy-only heal, and adapter values verified.

### [BLOCKER] `remove_channel` silently no-ops for inactive/disabled channels while reporting success
- **Where:** `communications/manager.py:632-654`; surfaced by `servers/routes/communications.py:139-153` (returns `{"status":"ok"}`) and `mcp_proxy/tools/communications.py:132-141` (`{"success": True}`)
- **Failure mode:** `remove_channel` pops from `_adapters`/`_channel_by_name`; the only `delete_channel(channel.id)` call is gated behind `if channel is not None:` (line 648). For any channel not currently active — disabled, failed init (i.e. every Slack/Discord channel per the secret-resolver Blocker), or daemon-started-without-it — both pops return `None`, so the DB row is never deleted, yet the route/MCP report success. The channel resurrects on next restart and (if its init fails) can never be cleanly removed.
- **Why it matters:** Delete reports success while the contract is violated; composes with the init-failure Blockers into "broken channel that cannot be removed."
- **Minimal fix:** After in-memory teardown, fall back to `self._store.get_channel_by_name(name)` and delete the DB row regardless of adapter state; report not-found only when there is genuinely no row.
- **Confidence:** high — `if channel is not None:` gates the only delete call.

### [BLOCKER] `update_channel` (including disable) never reaches the running adapter — the kill switch is bypassed
- **Where:** `communications/manager.py:714-724` (pure store delegation); caller `servers/routes/communications.py:119-136`
- **Failure mode:** `update_channel` only writes the DB row. It does not stop polling, shut down/reinit the adapter, refresh `_channel_by_name` (which the send paths read), or reconfigure the rate limiter. `PUT /api/comms/channels/{id}` with `enabled:false` returns the updated record while the adapter keeps polling and sending until daemon restart. Token/destination/rate-limit changes likewise never apply.
- **Why it matters:** Disabling a channel is the operator's kill switch (e.g. a spamming or compromised webhook target). The API confirms the disable while traffic continues — enforcement silently bypassed.
- **Minimal fix:** After persisting, reconcile runtime state: stop polling + shutdown adapter; if still enabled, re-run `_init_adapter` + rate-limiter config + polling; refresh `_channel_by_name` (handle renames by removing the old key).
- **Confidence:** high.

### [BLOCKER] `add_channel` reports success when adapter initialization fails
- **Where:** `communications/manager.py:627-630` (`except Exception as e: logger.error(...)`, then `return channel_config`); callers `mcp_proxy/tools/communications.py:112-129` (`{"success": True}`) and `servers/routes/communications.py:100-116` (200 + `asdict`)
- **Failure mode:** When `_init_adapter` raises (bad token — always, for Slack/Discord per the secret Blocker), the exception is logged and swallowed; the method returns the `ChannelConfig` normally. MCP returns `success:True`; HTTP returns 200 with no degraded-state field. The user believes the channel works at the exact moment they could fix the credentials.
- **Why it matters:** Misconfiguration reported as success; combines with the `remove_channel` Blocker so the broken channel can't even be deleted.
- **Minimal fix:** Propagate the init failure (roll back / mark the row disabled) or include `"active": False, "init_error": str(e)` in the returned payload so all three surfaces report it.
- **Confidence:** high.

### [BLOCKER] MCP `send_message` tool reports `success: True` for failed deliveries
- **Where:** `communications/manager.py:251-258` (`except`, sets `message.status = "failed"`, returns message normally); `mcp_proxy/tools/communications.py:37-43` (`return {"success": True, "message_id": msg.id}` without inspecting `msg.status`)
- **Failure mode:** `send_message` swallows adapter exceptions into `status="failed"` and returns the message; the MCP tool never checks `msg.status`. An agent sending through a dead channel (every channel, per the outbound Blocker) is told the send succeeded.
- **Why it matters:** Success-reported-while-contract-violated on the primary agent-facing API; agents/pipelines believe a human was notified when nothing was delivered.
- **Minimal fix:** `return {"success": msg.status == "sent", "message_id": msg.id, "error": msg.error}`.
- **Confidence:** high.

### [BLOCKER] `webhook_secret` stored plaintext and leaked through the channels HTTP API
- **Where:** storage `communications/manager.py:576` (`if key == "webhook_secret": continue` — skips the SecretStore path), `storage/communications.py` plain INSERT, schema `postgres_baseline_schema.sql:1554` (`webhook_secret TEXT`); exposure `servers/routes/communications.py:97,113,136` (`asdict(c)` serializes `webhook_secret` into GET/POST/PUT channel responses)
- **Failure mode:** Every other channel secret routes through Fernet-encrypted `SecretStore`, but `webhook_secret` — the HMAC key authenticating inbound webhooks — is deliberately skipped, written plaintext to the DB, and returned verbatim by the channel list/create/update endpoints (auth is off by default in this local-first daemon). Anyone who can read the API or DB can forge "verified" webhooks, defeating `handle_inbound`'s signature check entirely.
- **Why it matters:** Security hole; contradicts the subsystem's own secret-handling pattern five lines above.
- **Minimal fix:** Store `webhook_secret` via `SecretStore` and resolve it in `handle_inbound`; until then redact it in route responses (`d.pop("webhook_secret")` before returning `asdict`).
- **Confidence:** high — all four sites verified; `ChannelConfig` is a dataclass, routes use `dataclasses.asdict`.

### [BLOCKER] GobbyChatAdapter reports success with no broadcast wired (and the wiring is never called)
- **Where:** `communications/adapters/gobby_chat.py:112-123` (`_broadcast is None` → warn, still `return message.id`); wiring `manager.py:687` `set_websocket_broadcast` has zero production callers (only `tests/communications/test_manager_extra.py`)
- **Failure mode:** `_broadcast` is always `None` at runtime, so every gobby_chat send logs a warning and still returns `message.id`; the manager marks `status="sent"`. The DB says delivered; the adapter delivered nothing. (Partially masked by the manager's separate `event_callback → broadcast_comms_event`, which pushes a different event shape.)
- **Why it matters:** Success-reported-while-contract-violated for the internal web-UI channel.
- **Minimal fix:** Call `set_websocket_broadcast(websocket_server.broadcast)` from runner wiring; in the adapter, return `None`/raise when `_broadcast` is unset so the manager marks `failed`.
- **Confidence:** high.

### [BLOCKER] Telegram registers a webhook URL that 404s (wrong `/v1/` prefix and channel-id instead of name)
- **Where:** `communications/adapters/telegram.py:83` (`f"{base}/v1/comms/webhooks/{config.id}"`) vs actual route `servers/routes/communications.py:32` (`POST /api/comms/webhooks/{channel_name}`, looked up by name)
- **Failure mode:** `setWebhook` registers a URL with the wrong path prefix and identifier. Telegram accepts it, the adapter logs "Successfully registered Telegram webhook," and every update delivery 404s on Gobby's side. Worse, while a webhook is registered Telegram rejects `getUpdates` with 409, poisoning the polling fallback too.
- **Why it matters:** Success reported while inbound Telegram is completely broken in webhook mode, and polling is wedged.
- **Minimal fix:** `f"{base}/api/comms/webhooks/{config.name}"`; add a test asserting the registered path matches the mounted route.
- **Confidence:** high.

### [BLOCKER] Telegram bot token leaks into logs and the messages DB via httpx error strings
- **Where:** `communications/adapters/telegram.py:77` (`self._api_base = f"https://api.telegram.org/bot{self._bot_token}"`), error paths `:91-97,123-124,152-155,246-249`
- **Failure mode:** The token is in every request URL. `httpx.HTTPStatusError` messages embed the full URL (`...for url 'https://api.telegram.org/bot<TOKEN>/sendMessage'`). On any non-2xx, `initialize` raises → manager logs `str(e)`; send failures set `message.error = str(e)`, which is **persisted to `comms_messages`** and surfaced through the messages API/UI; poll failures are logged.
- **Why it matters:** Secret exfiltration into plaintext logs and durable DB rows readable over the HTTP API.
- **Minimal fix:** Catch `httpx.HTTPStatusError` and re-raise sanitized (`str(e).replace(self._bot_token, "***")` or `RuntimeError(f"Telegram API error {status}")`).
- **Confidence:** high.

### [BLOCKER] Email `poll()` parses the wrong IMAP library's response shape → all inbound email lost and permanently marked Seen
- **Where:** `communications/adapters/email.py:417-424` (`for part in fetch_data: if isinstance(part, tuple): msg_bytes = part[1]`), `:470-474` (`_mark_seen` runs unconditionally per message number)
- **Failure mode:** The code expects stdlib `imaplib`'s tuple shape, but the daemon uses `aioimaplib`, whose `fetch()` returns `Response(result, lines)` with `lines` a **flat list of bytes** — never tuples. So `isinstance(part, tuple)` is always False, zero messages parse, and every unseen email is then flagged `\Seen` and never returned/stored. Poll looks healthy (returns `[]`, no error).
- **Why it matters:** Silent, permanent loss of all inbound email. Tests mock `fetch` in the wrong library's shape, green-lighting the broken path.
- **Minimal fix:** Parse aioimaplib `lines` (locate the literal payload between the `FETCH (` header and `)`); only mark Seen after a successful parse; pin a test to the real `aioimaplib.Response` shape.
- **Confidence:** high — installed aioimaplib 2.0.1 `Response` namedtuple verified.

### [BLOCKER] Email OAuth2 IMAP login calls a nonexistent method → AttributeError crash
- **Where:** `communications/adapters/email.py:211` (`await imap_client.authenticate("XOAUTH2", lambda: auth_string)`)
- **Failure mode:** `aioimaplib.IMAP4_SSL` (2.0.1) has no `authenticate` method (it exposes `login`, `xoauth2(user, token)`, `logout`, `select`). With `auth_method: oauth2` + an IMAP host — the documented Gmail path — `initialize` raises `AttributeError`, and so does every reconnect.
- **Why it matters:** OAuth2 + IMAP can never initialize or poll; the channel dies with an unhandled attribute error rather than a config error.
- **Minimal fix:** Use the library's native call: `resp = await imap_client.xoauth2(self._from_address, token)` and check `resp.result == "OK"`.
- **Confidence:** high — installed library surface verified.

### [BLOCKER] Slack URL-verification challenge is raised as an exception nothing catches → 500, Events API can never be enabled
- **Where:** `communications/adapters/slack.py:296-299` (`raise SlackVerificationChallenge(challenge)`); consumers `manager.py:546` and `servers/routes/communications.py:62-65` look for a message with `content_type == "url_verification"` (produced nowhere)
- **Failure mode:** Slack requires the challenge echoed with HTTP 200 to enable an Events endpoint. `parse_webhook` raises instead of returning the challenge message the router convention expects; the route's `except Exception` turns it into 500. Since Slack `supports_polling` is False, inbound Slack is entirely unusable.
- **Why it matters:** Success-path contract violation between adapter and router conventions; Slack inbound can never be activated.
- **Minimal fix:** Return `[CommsMessage(content=challenge, content_type="url_verification", ...)]` (matching the existing route special-case) instead of raising; delete the dead branch.
- **Confidence:** high.

### [BLOCKER] github_helper treats MCP `isError` results as success → `create_branch` returns `True`, `push_files`/`get_file_contents` return the error payload as data
- **Where:** `integrations/github_helper.py:79-99` (`_call_github_mcp` never checks `isError`), `:285` (`return True`), `:328-338` (push_files returns the parsed error), `:243-244` (`get_file_contents` returns error text as file body); downstream `mcp_proxy/client_manager/invocation.py` also returns the raw result without checking `isError`
- **Failure mode:** MCP tool failures come back in-band as `CallToolResult(isError=True)` with the message as text content — `session.call_tool` does not raise. `_call_github_mcp` JSON-parses or returns that error text. So a rejected branch creation makes `create_branch` return `True` (and skip its git fallback, since nothing threw); `push_files` returns `{"result": "<error text>"}` as commit info; `get_file_contents` returns the error string as the file's contents.
- **Why it matters:** Callers believe a remote branch/commit/file exists when it doesn't; downstream automation builds on phantom state. Latent today (only `list_commits` has production callers, all double-wrapped in caller-side fallbacks), but it is the helper's public API and the first path new callers hit. Zero tests exist for this file.
- **Minimal fix:** In `_call_github_mcp`, check `getattr(result, "isError", False)` and raise before parsing — that single change restores the git fallbacks and makes `push_files` raise instead of fabricating success.
- **Confidence:** high — SDK semantics and all sites verified.

### [BLOCKER] github_triage self-perpetuating re-triage loop: every previously-triaged open issue is re-commented and re-`build`-dispatched on every cron cycle
- **Where:** `github_triage/service.py:313-352` (idempotency gate `existing.content_hash == current_hash and existing.verdict == outcome.verdict`, then `apply_triage_outcome`), `issue_index.py:146-154` (`content_hash` includes `"updated_at"` and `"labels"`)
- **Failure mode:** First triage computes hash H1, then posts a comment and adds `gobby:accepted` — mutating the issue's `labels` and `updated_at`. Next hourly reconcile fetches the issue → H2 ≠ H1 → the gate fails → `apply_triage_outcome` runs again: another comment, labels re-added, and **`_run_build` re-dispatched**. The new comment bumps `updated_at` again, guaranteeing the loop forever. Only `dedup` (which closes the issue) escapes.
- **Why it matters:** Unbounded public comment spam on users' GitHub issues, repeated label churn, and repeated `build()` dispatch onto live tasks every cron tick — plus embeddings + judge calls per issue per hour. The core idempotency contract is violated by the subsystem's own side effects. The one test reuses identical `issue_data`, masking it.
- **Minimal fix:** Exclude `updated_at` and `gobby:*`-managed labels from `content_hash`; short-circuit side effects when `existing.verdict == outcome.verdict` (only re-act on verdict change); fire `_run_build` only on first acceptance (no existing `task_id`).
- **Confidence:** high.

### [BLOCKER] github_triage default judge accepts every issue as "implement" and auto-`build`s with `isolation="none"` → external issue text becomes autonomous-agent instructions in the live tree
- **Where:** `github_triage/service.py:595-608` (`_judge` fallback `return TriageOutcome("implement", ...)` when `self.judge is None`), `:708-713`/`:432-461` (raw `issue.body` → task description), `:527-531` (`_run_build` → `BuildOptions(skip_stages=[], isolation="none")`); both production constructors pass `judge=None` (`servers/routes/github_triage.py`, cron handler), and no judge is ever wired despite the docs/triage-agent workflow describing one
- **Failure mode:** With triage enabled on a public repo, any GitHub account opens an issue ("To fix this, run `curl … | sh`…"). No duplicate, no `gobby:ignore` → verdict `implement` → a Gobby task is created with the attacker's text verbatim as the description, then `build()` opts it into `allow_automation` dispatch with **no worktree isolation** — an autonomous coding agent executes attacker-authored instructions against the live repo.
- **Why it matters:** A direct prompt-injection → autonomous-execution pipeline from an external actor (only an HMAC-valid webhook is needed, which the repo's own config provides). The safe-by-default state is "execute everything." Severity is gated by triage being disabled by default, but the documented happy path is exactly this.
- **Minimal fix:** Default verdict must be `escalate` (human review) when no judge is configured; never auto-`build` without explicit judge approval; if building from external issues is ever allowed, force `isolation="worktree"`/`"clone"` and fence the injected body in the task description.
- **Confidence:** high — every code fact verified.

### [BLOCKER] github_triage swallows MCP `isError` → rate limits look like end-of-pagination and the cron run reports success while doing nothing; comment/label/close failures are invisible
- **Where:** `github_triage/service.py:697-705` (`_parse_mcp_result` never checks `isError`), `:249-252` (`reconcile_project_repos` `break` when result isn't a list), `:487-525` (`_comment_and_label`/`_close_issue` with `required=False`)
- **Failure mode:** A GitHub 403/429 during reconcile returns an error result → `_parse_mcp_result` returns the error text string → `isinstance(issues, list)` is False → pagination silently terminates → the cron handler returns `scanned=0 triaged=0 errors=0`, so scheduler failure backoff never engages. Separately, `add_issue_comment`/`add_labels`/`update_issue` errors are stringified and ignored while the verdict is recorded as processed. There is no retry/backoff or rate-limit awareness anywhere.
- **Why it matters:** Rate limiting (the most common GitHub failure) is indistinguishable from "no issues" and from "side effects applied" — success reported while the contract is violated.
- **Minimal fix:** Check `isError` in `_parse_mcp_result`/`_github_call` and raise a typed error; count it in reconcile instead of `break`; add backoff honoring `X-RateLimit-*`.
- **Confidence:** high that errors are unchecked; high that the official GitHub MCP server surfaces API errors as `isError` results.

## Important

Deduped across reviewers; grouped by theme. Each carries file:line and is real per source.

**Sync DB / network on the asyncio event loop (systemic — IMPORTANT per the bar).** `communications/manager.py` (`create_message`, `resolve_identity`, identity lookups, `list_routing_rules` inside async `router.match_channels`), `identities.py:53-123` (incl. `SessionManager.register` taking advisory locks), `reactions.py:53,65`, and `integrations/linear_graphql.py:45-50` (`from_database` → sync `SecretStore.get` called from async sync paths) all run blocking psycopg/network on the daemon loop. github_triage's `teams.py:298-309` does a **synchronous JWKS HTTP fetch with no timeout** (`PyJWKClient`) inside async `verify_webhook` — a hanging login.botframework.com freezes all daemon I/O. Wrap store/JWKS calls in `asyncio.to_thread` (or async variants).

**Webhook endpoints not auth-exempt.** `servers/middleware/auth.py:32-50` exempts `/api/github/webhooks/` but not `/api/comms/webhooks/`. With UI auth enabled, all Slack/Telegram/Teams/Discord webhook deliveries get 401 before reaching the per-channel signature verification that is their real auth. Add `"/api/comms/webhooks/"` to `_PUBLIC_PREFIXES`.

**Webhook signature verification silently skipped unless `channel.webhook_secret` is set.** `manager.handle_inbound` gates on `if channel.webhook_secret:`. Slack resolves `SLACK_SIGNING_SECRET` into `_signing_secret` but `verify_webhook` only uses the passed-in `secret`; Teams' JWT verify needs no shared secret yet is gated on one. Result: default-open authentication on internet-facing webhook routes (forged Slack events, Twilio SMS, Teams activities accepted). Let adapters self-verify when they hold material.

**Teams missing the Bot Framework `serviceUrl`-claim equality check** (`teams.py:311-312`): only checks `serviceUrl.startswith("https://")`, never `decoded["serviceUrl"] == activity["serviceUrl"]`. A replayed token + forged `serviceUrl` poisons conversation refs and exfiltrates the bot's Bearer token to the attacker on the next `send_proactive`.

**Cross-channel identity linking trusts a self-asserted username** (`identities.py:38-44,78-79`): a new external user whose claimed username matches any existing identity on any other channel is silently bound to that identity's `session_id`, with no verification handshake — session hijack / cross-user message leakage. Latent today (no adapter populates `external_username`, the next item) but fully armed. Remove implicit username auto-linking; require a verified bridging flow.

**`external_username`/`identity_id` adapter-contract drift.** No adapter writes `metadata_json["external_username"]` (manager reads exactly that key), and Telegram's `parse_webhook` never sets `identity_id` at all — so identity resolution and session bridging silently no-op for Telegram and run degraded for the rest. Define the adapter contract (`identity_id` = platform user id, `external_username` in metadata) and add a conformance test.

**Polled messages lost on failure / one bad message poisons the batch.** `polling.py` + `manager.handle_inbound_messages:460-488`: only `create_message` is per-message guarded; identity resolution at `:471` is not, so one DB hiccup aborts the batch. Telegram advances its offset before processing, making the loss permanent; webhook retries duplicate rows (no dedup on `(channel_id, platform_message_id)`). Wrap the per-message body; advance cursors only after handling; add a unique guard.

**Rate limiter `rate<=0`/`burst==0` crashes or hangs the send path.** `rate_limiter.py:160` (`needed / bucket.rate` → ZeroDivisionError), negative rate → `asyncio.sleep(negative)` hot loop, `burst==0` → infinite wait; `wait_if_needed` has no max-wait bound, so sustained backoff hangs `send_message` (and any awaiting MCP/pipeline) indefinitely. `ChannelDefaults` has no `ge=1` constraints. Validate config and add a max-wait that raises into `status="failed"`.

**Polling gated on global `webhook_base_url`, killing poll-only adapters.** `manager.py:112,622`: `if adapter.supports_polling and not self._config.webhook_base_url`. Setting `webhook_base_url` (needed for Slack/Teams/Telegram webhooks) disables polling for email too (`supports_webhooks=False`). Use `(not adapter.supports_webhooks or not webhook_base_url)`.

**Telegram webhook-vs-polling decision split across channel-level and global config** can contradict → either zero inbound (adapter deletes webhook, manager never polls) or a permanent 409 loop (adapter registers webhook + manager polls). Single source of truth.

**Adapters skip `super().__init__()`** (Discord, Telegram, Slack, Teams, SMS — email is correct): base sets `_rate_limit_callback = None`; any 429 retry path without a prior `set_rate_limit_callback` raises `AttributeError`. Latent (manager always calls it first) but fragile. Add `super().__init__()`.

**`initialize()` failure paths leak live clients/connections;** the manager catches and discards without `shutdown()` (email SMTP+IMAP sockets, Slack/Teams `httpx.AsyncClient`). Wrap each `initialize` body in `try/except: await self.shutdown(); raise`. Combined with the secret Blocker, this fires for every channel on every startup.

**Email security/correctness:** XOAUTH2 SMTP auth result ignored (`email.py:197-202`, failed auth reported as success); IMAP login result ignored + reconnect doesn't catch aioimaplib `Abort` (`:206-264`, silent auth failure wedges polling forever); credentials sent over plaintext SMTP for ports other than 465/587 (`:113-121`, no override); incremental `_mark_seen` loses already-marked messages on mid-loop failure (`:404-474`); body decoded ignoring part charset → mojibake (`:436-446`); reply recipient taken verbatim from inbound `From` → comma-separated address injection / spam relay (`:465,315-322`); SMTP/IMAP reconnect has no lock → concurrent sends race and leak connections (`:215-266`).

**SMS:** `parse_qsl` drops blank params before computing the Twilio signature → valid webhooks rejected, and media-only MMS dropped at parse (`sms.py:181,243`). Use `keep_blank_values=True`; accept `NumMedia>0`.

**Discord gateway:** receives `MESSAGE_CREATE` and only `logger.debug`s it — inbound gateway messages never reach the manager (`discord.py:237-242`); reconnect retries fatal close codes (4004/4014) forever, burning the bot's session-start budget (`:247-258`); clean close reconnects with zero delay (tight loop); `resume_gateway_url` used without `?v=10&encoding=json` so RESUME never succeeds; heartbeat ignores ACKs (zombie connections undetected) and dies silently on error; outbound lacks `allowed_mentions` → `@everyone` injection via relayed content; PING never ACKed with `{"type":1}` so Interactions endpoint verification can't succeed.

**`Retry-After` honored with no upper bound** (`base.py:176-198`): a hostile/buggy server can park `send_message` for hours and push that backoff into the shared rate limiter. Clamp.

**Integrations (Linear/GitHub):** no pagination anywhere — Linear `first:100` and GitHub `per_page≤100` single-page silently truncate (`pageInfo`/`hasNextPage` appear nowhere in `src/gobby`); `list_issues` >100 issues silently drops the rest while sync advances `linear_synced_at` (borders Blocker). Non-idempotent Linear mutations retried on timeout → duplicate issue/project creation (`linear_graphql.py:60-70`). `GitHubIntegration._check_availability` lacks the lazy-connect handling its Linear twin got, so GitHub MCP is reported unavailable under the default `lazy_connect=True` manager (hard failures on push/PR paths). `list_commits` returns `[]` instead of the documented git fallback on unexpected shapes. Git option-injection via unvalidated `branch`/`from_branch`/`ref` reaching git argv in option position (`github_helper.py:180-296`; mitigated by the sole current caller's `_validate_git_ref`). `list_projects` fallback swallows the primary error without chaining and doubles request load during outages. Zero tests for `github_helper.py`.

**github_triage:** delivery stuck in `processing` forever if the worker crashes mid-triage (no sweeper; `process_delivery` treats non-`pending` as terminal); transient failures leave deliveries permanently `error` with no retry; `webhook_secret_ref` echoed through the project-config GET/PUT API via `to_dict()` (plaintext if an inline secret is used); per-project cron registration aborts all remaining projects on one failure (`cron.py:92-159`); concurrent webhook+reconcile can double-comment/double-build the same issue (DB unique index prevents duplicate task rows but not duplicate side effects); "Accepted" comment posted to the public issue **before** the build dispatch that may silently fail.

## Nit

Representative (full per-reviewer lists captured in triage). Comms: dead `asyncio.iscoroutine` branch in `router.py:59`; router normalizes the event but not the rule pattern (case-sensitive `fnmatch`); `send_proactive` bypasses rate limiting and persistence; event-callback failures logged at DEBUG; `add_channel` mutates the caller's `config` dict in place; `ThreadManager` keyed by mutable channel name; `models.py` `from_row` uses naive local time and masks missing NOT NULL columns with `"unknown"`; `chunk_message` returns zero chunks for whitespace-only over-limit content (silent "sent"); attachment `get_path` suffix matching can return another attachment's file; `cleanup_old` never scheduled (unbounded attachment dir despite `retention_days`); repeated in-function imports. Integrations: retry messages hardcode "3 attempts"; new `httpx.AsyncClient` per `execute()`; twin-class duplication `linear.py`/`github.py`; `import base64` mid-function; `limit` silently capped at 100. github_triage: high-but-uncertain (0.90) similarity auto-closes real issues; `int(issue["number"])` unguarded in `from_github`; label-only edits trigger full re-embed.

## Systemic patterns

1. **"Log-and-return-success" everywhere.** The manager swallows failures into logs at every boundary (adapter init, send, store, delete) and the MCP/HTTP callers translate normal returns into `success:True`. github_triage and github_helper do the same with MCP `isError`. This single pattern produces ~10 of the Blockers. The fix is structural: manager methods must return status objects, and MCP/HTTP surfaces must inspect them; a shared "call MCP and raise on isError" helper closes the github_helper + github_triage + invocation-layer instances at once.

2. **`CommsMessage.channel_id` is overloaded with no documented contract.** Inbound adapters put platform IDs in it; outbound the manager puts the internal UUID in it; adapters read it as the platform destination. This one ambiguity is the root of both the inbound-FK-drop Blocker and the outbound-destination Blocker. Define the field's meaning on the model and route the platform destination through `platform_destination` consistently.

3. **Secret-resolution is per-adapter folklore, untested end-to-end.** Adapters pass full `$secret:` refs; `SecretStore.get` expects bare names; only Telegram strips the prefix. Every adapter test fakes its own resolver, so the suite encodes the broken contract. One `resolve_secret_ref(ref, resolver)` helper on the base class plus a shared conformance test fixes all of it.

4. **Tests mock the seam where the contract breaks.** Mock stores hide the empty-timestamp crash; MagicMock service containers hide the missing `approve_step`; wrong-library fetch shapes hide the email parse bug; hand-set platform IDs hide the destination drift. The subsystem has essentially no real-store, real-types, through-the-manager integration test — which is exactly why 17 Blockers coexist. That missing end-to-end test is the highest-value single fix.

5. **Half-wired feature surfaces carrying live bugs.** `send_event`/`MessageRouter`/routing rules, `AttachmentManager.download/cleanup_old`, cross-channel identity bridging, and `set_websocket_broadcast` are all built, documented, and unreferenced by any production path — dead code that will activate unreviewed (with its security/resource bugs) the moment someone wires it.

6. **No pagination and no rate-limit awareness in any external client.** Linear, GitHub-helper, and github_triage all single-page-truncate and none honor `X-RateLimit-*`/backoff; truncation and rate-limiting both masquerade as success.
