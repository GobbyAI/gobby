# Review: config

- **Scope:** `src/gobby/config/` — `app.py` (DaemonConfig), `bootstrap.py`/`bootstrap_io.py`/
  `postgres_bootstrap.py` (pre-DB bootstrap), `persistence.py`, `validation_detection.py`
  (close-gate command classifier), `tasks.py`, `build.py`, `features.py`/`feature_base.py`/
  `feature_candidate_defaults.py`, `mcp.py`, `servers.py`, `embedding_keys.py`,
  `sessions.py`, `voice.py`, `extensions.py`, `tmux.py`, `daemon_sandbox.py`, `code_index.py`,
  `ui.py`, and the small modules (`cron`, `system_loops`, `ai`, `local`, `skills`, etc.).
  Plus the secret-handling seam: `storage/secrets.py`, `storage/config_store.py`,
  `mcp_proxy/client_manager/secrets.py`, `servers/routes/configuration_secrets.py`,
  `cli/secrets.py`, and the install path (`cli/installers/postgres.py`,
  `data/docker-compose.services.yml`). Cross-seam reads into the close-gate evidence
  consumers, the auth middleware, and tests.
- **Reviewer:** Claude Fable 5 — 4-agent parallel fan-out, all Blockers synthesizer-verified
  (the close-gate classifier findings verified by **executing the classifier**; the docker
  binding + static password and the secrets-route auth verified against source).
- **Commit / branch:** `0.5.0` @ HEAD `a704e9161` (working tree clean at review time).
- **Summary:** 4 Blocker · 17 Important · 5 Nit — the close-gate command classifier can be
  defeated by a one-token suffix (`|| echo ok`, `--collect-only`), the install path ships a
  hardcoded DB password on a 0.0.0.0-published port, and the secrets HTTP route is
  unauthenticated by default. Bootstrap secret hygiene for the *flat config-store keys* is
  genuinely strong (Fernet at rest, plaintext rejection, 0600 files, names-not-values in
  exports); the gaps are at the edges — nested/object configs, the DSN, and the install
  defaults.

## Findings

### [BLOCKER] Close-gate classifier records a FAILED command as passing validation evidence via `cmd || echo ok`
- **Where:** `config/validation_detection.py:453` (`classify_validation_command` matches the first segment) feeding `workflows/observer_verification.py:51-55` (the `success` flag comes from the **whole shell tool's** exit code, not the matched segment). Empirically verified: `classify_validation_command("pytest || echo ok")` → categories `('test',)`, and `detect_verification_evidence('pytest || echo ok', exit_code=0)` → `verification_evidence_recorded=True, success=True`.
- **Failure mode:** The classifier splits on `&&`/`||`/`;`/`|`, matches the `pytest` segment, but the consumer uses the aggregate shell exit code. `pytest || echo ok` → pytest fails, `echo ok` makes the shell exit 0 → recorded as a *successful* test run, flipping `verification_evidence_recorded=true` — the exact variable `completion-readiness.yaml` and `_lifecycle_close.py:516` consult to clear the close gate. The same works inside `bash -c "pytest || echo passed"` (the inner `||` isn't even segment-split).
- **Why it matters:** A task closes with broken code. An agent (or adversary) suffixes a failing suite with `|| echo ok` and the gate sees passing validation. Direct gate poisoning of the repo's core close contract.
- **Minimal fix:** The classifier must report which segment matched and the consumer must map success to that segment — and since per-segment exit codes aren't available from tool output, the safe rule is: never treat a multi-segment command containing `||` (or any chain) as positive evidence (`success=True`).
- **Confidence:** high (empirically verified).

### [BLOCKER] Non-executing flags (`--collect-only`, `--version`, `--help`, `--co`, `--fixtures`) classify as a passing test/lint run
- **Where:** `config/validation_detection.py:755-773` (`_matcher_matches_segment` — the only arg gate is `forbidden_args_any = _MUTATING_VALIDATION_ARGS` at `:20`: `["--fix","--unsafe-fixes","--write","-w"]`; nothing excludes information-only flags). Empirically verified: `classify_validation_command("pytest --collect-only")` and `("pytest --version")` both → `('test',)`.
- **Failure mode:** `pytest --collect-only` exits 0 running zero tests, yet records `verification_evidence_recorded=True`. Same for `--version`/`--help`/`--co`/`--fixtures`/`--dry-run`, and `mypy --version`/`ruff check --help`. The cheapest possible gate bypass — a one-token suffix no-op. These flags aren't in `_MUTATING_VALIDATION_ARGS` because they're non-executing, an orthogonal exclusion category the module never models.
- **Minimal fix:** Add a per-tool `non_executing_args_any` exclusion seeded with `--collect-only`/`--co`/`--version`/`-V`/`--help`/`-h`/`--fixtures`/`--markers`/`--dry-run`/`-n0`; disqualify any segment whose argv contains one. (Root of the already-filed hooks-review finding; the fix belongs here.)
- **Confidence:** high (empirically verified).

### [BLOCKER] Install ships a hardcoded DB password on a 0.0.0.0-published port
- **Where:** `data/docker-compose.services.yml:89` (`"${GOBBY_POSTGRES_PORT:-60891}:5432"` — no `127.0.0.1:` host-IP prefix; verified), `:9` falkordb `16379:6379`, `:33` qdrant `6333:6333`; `cli/installers/postgres.py:32` (`DEFAULT_POSTGRES_PASSWORD = "gobby_dev"`, verified), `:83` (`env.setdefault(..., "gobby_dev")`), `:450` (DSN built from the static creds).
- **Failure mode:** Docker publishes the ports to `0.0.0.0` on the host (and on Linux bypasses the host firewall via Docker's iptables rules), so the full Gobby hub Postgres — the entire task/session/memory/secrets datastore — is LAN-reachable with the known credentials `gobby:gobby_dev`. No `secrets.token_*` password generation exists. FalkorDB ships the static `gobbyfalkor`; Qdrant has no auth. The DSN-in-bootstrap design assumes the DB is local-only; the container binding contradicts it.
- **Minimal fix:** Prefix all published ports with `127.0.0.1:` and generate a random per-install password (`secrets.token_urlsafe`) in the installer, persisting it to the DSN.
- **Confidence:** high (binding + static password verified; LAN-reachability on Linux Docker is standard).

### [BLOCKER] Secrets HTTP routes are unauthenticated by default — an unauthenticated write/delete takeover primitive
- **Where:** `servers/middleware/auth.py:74-75` (`dispatch` returns `call_next` unconditionally when `is_auth_enabled` is false), `auth.py:62-65` (auth "enabled" only when both `auth.username` and a stored `auth.password` exist — the local-first default ships neither); routes at `servers/routes/configuration_secrets.py:117-170` (`/api/config/secrets`); `app_factory.py:624` (`run_server` bind default `0.0.0.0`) vs `bootstrap.py:27` (`DEFAULT_DAEMON_BIND_HOST="localhost"`).
- **Failure mode:** Out of the box, `GET/POST/DELETE /api/config/secrets` accept any caller with no credential. The GET returns only names (values are masked — see non-bugs), but unauthenticated **write/delete** is itself a takeover: overwrite `auth.password` to lock in a login, rewrite a secret referenced by MCP headers to point at an exfil endpoint, or delete `falkordb_password` to DoS. The route is NOT in `_PUBLIC_PREFIXES` (the gate was *intended*), but the gate is a no-op until the user configures a web login, which most local CLI users never do. With a non-loopback `bind_host`, it's remotely reachable; even on loopback any local process can hit it.
- **Minimal fix:** Gate `/api/config/secrets` write/delete behind a local runtime token (the `/api/local/` token pattern already exists) independent of web-login, or refuse secret writes when auth is disabled AND the bind host is non-loopback.
- **Confidence:** high (auth-disabled passthrough + route mounting verified; remote reach depends on operator `bind_host`).

### [IMPORTANT] Web UI auth is off by default; non-loopback bind + UI-enabled + empty credentials exposes the whole control-plane API
- **Where:** `config/ui.py:39-58` (`AuthConfig` username/password/session_secret all default `""`); `servers/middleware/auth.py:74-75` (bypass when not enabled). Only `bootstrap.py:27` localhost default prevents remote exposure.
- **Failure mode:** Setting `ui.enabled=True` with empty credentials serves the UI and all `/api/` routes with no auth; an operator who also sets a non-loopback `bind_host` (a common "reach it from my LAN" change) exposes an unauthenticated control plane. No guardrail/warning fires.
- **Minimal fix:** A cross-field validator/startup check that refuses to start (or loudly warns) when `ui.enabled` + non-loopback bind + unconfigured auth combine.

### [IMPORTANT] Runtime Postgres DSN (with password) is stored plaintext in bootstrap.yaml; the keyring contract has drifted
- **Where:** `config/postgres_bootstrap.py:18` (`KEYRING_DATABASE_URL_REF` defined+exported but never used to read/write a keyring — verified zero `keyring.get/set_password` calls), `:39-45` (`write_postgres_install` writes `data["database_url"]` and pops `database_url_ref`); `bootstrap.py:123-126` (loader *rejects* `database_url_ref` as "no longer supported"), `:138` (loads plaintext `database_url`).
- **Failure mode:** CLAUDE.md documents the DSN as living in the OS keyring (`gobby:postgres_database_url`) with bootstrap holding only a `database_url_ref`. The code does the opposite: the keyring path is dead and the full DSN (with the DB password) is always plaintext in `~/.gobby/bootstrap.yaml`. Mitigated to owner-only by 0600 (`bootstrap_io.py:46,48`, enforced on read at `bootstrap.py:186-190`) — hence IMPORTANT, not Blocker — but weaker than the keyring (backups, `tar`/`rsync` of `~/.gobby`, any same-user process), and the docs mislead operators.
- **Minimal fix:** Restore the keyring-backed `database_url_ref` resolution, or remove the dead constant and update CLAUDE.md to state the DSN is plaintext-in-0600-bootstrap. The contract and code must agree.

### [IMPORTANT] MCP server `env`/`headers` secrets are persisted plaintext (DB + file), unlike the config_store which enforces `$secret:` refs
- **Where:** `config/mcp.py:221-233` (`save_servers` writes `headers`/`env` verbatim to `~/.gobby/mcp-servers.json`); `storage/mcp.py:192-194` (`upsert` does `json.dumps(env)`/`json.dumps(headers)` straight into `mcp_servers`); `mcp_proxy/services/server_mgmt.py:42-89` passes them through with no extraction. Contrast `config_store.py:87-95` which *rejects* plaintext for secret-suffixed keys.
- **Failure mode:** MCP servers commonly need API keys as env vars; a user/agent passing a literal key (the natural thing) writes it cleartext to the DB and/or file. The `$secret:NAME` mechanism exists (`client_manager/secrets.py`) but is purely opt-in and undocumented at the add surface, so cleartext is the default outcome.
- **Minimal fix:** On add/save, detect secret-looking `env`/`headers` values, store them in `SecretStore`, and substitute a `$secret:` ref before persisting — mirroring config_store; at minimum warn.

### [IMPORTANT] Blocking webhooks fail OPEN when the endpoint is unreachable; no fail-closed config knob
- **Where:** `config/extensions.py:80` (`WebhookEndpointConfig.can_block`, no `fail_closed`/`on_error` field) + `hooks/webhooks.py:253-259` (failure result has `decision=None`) and `:320-339` (`get_blocking_decision` only blocks on an explicit block/deny → returns allow otherwise).
- **Failure mode:** A `can_block=True` webhook that times out / errors / exhausts retries allows the action. A security webhook meant to gate an action is silently disabled whenever it's unreachable. (Cross-confirms the hooks-review finding; this is the config gap behind it.)
- **Minimal fix:** Add `fail_closed: bool = False` (or `on_error: Literal["allow","block"]`) and treat a failed blocking result as `block` when set.

### [IMPORTANT] `WebhookEndpointConfig.url`/`headers` advertise `${ENV_VAR}` substitution that is never implemented
- **Where:** `config/extensions.py:51-61` (descriptions claim `${ENV_VAR}` support) vs `hooks/webhooks.py:165,172` (uses `endpoint.url`/`headers` raw; no expansion). The stdio MCP transport *does* implement it (`mcp_proxy/transports/stdio.py:21-60`).
- **Failure mode:** A user following the documented `url: "https://${HOST}/hook"` / `Authorization: "Bearer ${TOKEN}"` pattern gets the literal unexpanded string — broken URLs/auth, and the documented way to keep webhook secrets out of plaintext config silently doesn't work, pushing users to inline tokens.
- **Minimal fix:** Implement `${ENV_VAR}` expansion at dispatch (reuse the stdio helper), or remove the false claim.

### [IMPORTANT] `DaemonOwnedSandboxConfig` exposes no mode/network toggle — daemon-owned sandbox hardcoded permissive + network-on
- **Where:** `config/daemon_sandbox.py:8-22` (only `enabled`/`extra_read_paths`/`extra_write_paths`) vs `agents/sandbox.py:50-51` (`_DAEMON_OWNED_SANDBOX_MODE="permissive"`, `_DAEMON_OWNED_ALLOW_NETWORK=True`).
- **Failure mode:** Web-chat and agent sandboxes always run permissive with outbound network; no config field can tighten them. Defense-in-depth gap (cross-confirms the agents review); the config model is the natural place to surface these knobs and doesn't.
- **Minimal fix:** Add `mode`/`allow_network` to `DaemonOwnedSandboxConfig` and thread them through `agents/sandbox.py` instead of module constants.

### [IMPORTANT] `config reset` / `delete_all()` orphans encrypted secret values in the `secrets` table
- **Where:** `servers/routes/configuration_values.py:220-228` (`reset_config` → `config_store.delete_all()`); `storage/config_store.py:167-169` (`delete_all` truncates only `config_store`) and `:162-165` (`delete`), vs the correct `:230-241` (`clear_secret` deletes from both tables in a transaction).
- **Failure mode:** Per-key clears route through `clear_secret` (both tables); `reset_config` calls `delete_all`, which leaves every Fernet-encrypted value (`auth.password`, `falkordb_password`, `embeddings_api_key`, MCP secrets) orphaned in `secrets` with no surviving reference and no UI to clear it. After a "reset to defaults," the user believes secrets are gone but they persist (Fernet key derives from local machine_id+salt); the table grows unbounded.
- **Minimal fix:** In `reset_config`, enumerate `get_secret_keys()` and `clear_secret()` each inside the transaction; make `delete_all`/`delete` secret-aware.

### [IMPORTANT] Malformed numeric/scalar in bootstrap.yaml silently discards the ENTIRE bootstrap (incl. the DSN)
- **Where:** `config/bootstrap.py:131-140` (`int(...)`/`str(...)` casts) inside the `try` whose `except Exception` at `:143-144` returns `_default_bootstrap_config()` (database_url=None).
- **Failure mode:** A non-numeric `daemon_port` (e.g. `"abc"`) raises `ValueError` (not `BootstrapConfigError`), caught by the broad handler, logged at warning, and all defaults returned — the operator's explicit `bind_host`, ports, and runtime DSN silently dropped from one typo. Fails safe for bind_host but operationally destructive (lost DSN → confusing later failure, or silent default ports). The permission check and backend parse correctly hard-fail; these scalar casts should too.
- **Minimal fix:** Validate the int casts and raise `BootstrapConfigError` on bad values rather than resetting.

### [IMPORTANT] Config-file layer (Layer 2) doesn't resolve `${VAR}`/`$secret:`; `load_yaml()` (which does) is unreachable at runtime
- **Where:** `config/app.py:867-869` (Layer 2 reads with raw `yaml.safe_load`, no `expand_env_vars`/`_resolve_config_values`) vs Layer 3 DB values (`:886-889`) and the standalone `load_yaml()` (`:592`, zero internal callers).
- **Failure mode:** `${ENV}`/`$secret:NAME` in a non-bootstrap config.yaml pass verbatim into `DaemonConfig`. Latent at runtime today (only bootstrap.yaml is loaded, and Layer 2 is gated off it), but any caller passing a config.yaml path gets unexpanded refs — contradicting field docstrings promising load-time expansion.
- **Minimal fix:** Route Layer 2 through `expand_env_vars`/`load_yaml` with the same secret resolver, or delete the unused `load_yaml` export.

### [IMPORTANT] `build.yaml` accepts `max_active_agents: 0`/negative — the YAML path bypasses the `>= 1` guard the CLI path enforces
- **Where:** `config/build.py:263-266` (`_normalize_int`, no lower bound) used at `:161-164` for `max_active_agents`/`dispatch_interval_seconds`, while `_normalize_optional_int` (`:269-275`) enforces `>= 1` for stage caps and `build/validation.py:51-53` enforces `>= 1` only on the CLI-flag path.
- **Failure mode:** A YAML `max_active_agents: 0` is falsy and silently falls back to the default (masking the misconfig); a negative value makes the dispatcher's cap comparison always reject, effectively disabling all dispatch. The same invariant is enforced in two of three entry points.
- **Minimal fix:** Enforce `>= 1` in `_normalize_int` for these fields, mirroring `_validate_max_active_agents`.

### [IMPORTANT] `mypy --install-types` (network-installing, mutating) classifies as a clean typecheck; zero-test selectors record as passing
- **Where:** `config/validation_detection.py:188-217` (the lint/type matcher's `forbidden_args_any` is the flat `_MUTATING_VALIDATION_ARGS`, missing tool-specific mutating flags); `:755-773` (no "ran zero tests" gate). Empirically: `mypy --install-types` → pass; `pytest -k nonexistent`/`go test -run xxx`/`cargo test nope` exit 0 with zero tests run and record as passing.
- **Failure mode:** `forbidden_args_any` covers exactly one category (mutation) and is tool-agnostic, so `--install-types`, selector-narrowing (`-k`/`--run`/`--filter`), and `--maxfail=0` all slip through. Higher-effort gate bypasses than `--collect-only` but equally effective.
- **Minimal fix:** Per-matcher-extensible `forbidden_args_any` (add `--install-types`); treat selector-narrowing args as evidence-weakening (record, don't auto-pass) or require the tool's output to confirm ≥1 test ran.

### [IMPORTANT] `;`-segmentation bug and redirection tokens defeat the classifier's command splitting
- **Where:** `config/validation_detection.py:664-679` (`shell_command_segments` relies on `shlex.split` then matches whole tokens in `_SHELL_SEGMENT_SEPARATORS`). `shlex.split("pytest; true")` → `['pytest;','true']` — the `;` stays glued, so `pytest;` never equals the separator and the segment matches nothing; `pytest > /dev/null` yields argv `['pytest','>','/dev/null']` (the `>` becomes a bogus arg).
- **Failure mode:** Segmentation depends on whitespace around operators — `||`/`&&` (with spaces) split (the dangerous false-positive direction) while `;`-chained legitimate runs are silently missed (false negative). Fragile and surprising.
- **Minimal fix:** Regex-split the raw string on `(\s*(?:&&|\|\||;|\|)\s*)` before shlex; strip redirection tokens from argv.

### [IMPORTANT] Non-atomic config/secret writes and missing close-gate tests
- **Where:** `config/app.py:973-977` (`export_config_to_yaml` truncates-then-writes with `chmod 0o600` only after — torn file on crash + a brief default-perms window, while `bootstrap_io.write_bootstrap_yaml` does tmp+fsync+chmod-before-rename correctly); `tests/config/test_validation_detection.py` has **no** coverage for non-executing flags, `|| echo ok` exit-code evasion, `mypy --install-types`, `;`-segmentation, or the `detect_verification_evidence` success-recording path — the gate-critical cases are untested.
- **Minimal fix:** Reuse `bootstrap_io`'s atomic writer for the export; add tests asserting the poisoning cases do NOT yield positive evidence (they fail today, codifying the fix).

### [IMPORTANT] Numeric validation gaps and URL/SSRF hardening across config models
- **Where:** `config/code_index.py:15,45,57,116` (`symbol_summary.batch_size`/`maintenance_interval_seconds`/`max_file_size_bytes`/`sync_worker_interval_seconds` lack lower bounds while siblings have `ge=1`); `config/tasks.py:148,152,131` (expansion `timeout`/`research_timeout`/`research_max_steps` lack the `>= 0` validator `WorkflowConfig.timeout` has); operator-supplied endpoints (`mcp_proxy/models.py:122`, `config/voice.py:24`, `config/ai.py:32`, `config/extensions.py:51`) validated only non-empty, no scheme/host check.
- **Failure mode:** `maintenance_interval_seconds=0`/negative → tight loop; `max_file_size_bytes=0` → indexes nothing; the URL fields are operator-trusted today (no live SSRF) but become a live SSRF if any ever becomes settable by a lower-trust caller (e.g. `import_mcp_server` from remote registry data).
- **Minimal fix:** Add `gt=0`/`ge=1`/`ge=0` bounds for consistency; centralize a URL validator (require http/https, optionally block link-local/metadata) for the URL fields.

### [NIT] Nested `api_key` fields bypass config_store secret enforcement; redaction gaps
- **Where:** `config/voice.py:32`, `config/ai.py:38` (`api_key` nested in object/list config — never passes `config_store._reject_plaintext_secret_value`, which guards only flat secret-suffixed dotted keys; `ai` documents `$secret:NAME`, voice does not, and voice has no redaction in any config dump).
- **Minimal fix:** Add `$secret:` guidance to voice's `api_key` and ensure config dump/export redacts these nested fields.

### [NIT] Dead keyring constant + stale contract; `resolve_secrets_in_config` re-raises with `exc_info`
- **Where:** `config/postgres_bootstrap.py:18,25` (`KEYRING_DATABASE_URL_REF` defined/exported, referenced nowhere — vestige of the reverted keyring migration; CLAUDE.md still claims keyring storage); `mcp_proxy/client_manager/secrets.py:69-71` (generic `except Exception` re-raises with `exc_info=True` — no current leak since `resolve_dict` doesn't put values in exceptions, but a future change surfacing a header/env value would land in logs).

## Systemic patterns

1. **The close-gate classifier answers "does this name a validator?" not "did a validator execute and pass?"** Three Blocker/Important gaps (`|| echo ok`, non-executing flags, zero-test selectors) all stem from this altitude mismatch, and `forbidden_args_any` — a flat, shared, mutation-only list — is the only behavioral filter, covering one of three needed categories (mutation, non-execution, zero-selection). Segment-then-match returns the first matching segment while every consumer has only the aggregate exit code, so any shell operator decouples "matched command's result" from "recorded success."
2. **Two-tier secret handling.** `config_store` enforces `$secret:` for flat secret-suffixed keys (Fernet at rest, plaintext rejection, names-not-values in exports) — genuinely strong. But every nested/object/sibling path skips it: MCP `env`/`headers`, voice/ai/local `api_key`, and the Postgres DSN all store secrets plaintext with at most a doc-comment. The enforcement boundary is the config_store API, not the data.
3. **Config model present, consumer hardcodes the security knob.** `TmuxConfig` and `DaemonOwnedSandboxConfig` are well-formed, but consumers either instantiate defaults (TmuxConfig in ~14 sites, ignoring a user socket override) or hardcode the policy (sandbox mode/network in `agents/sandbox.py`), so the config can't actually tighten behavior.
4. **Fail-open / off-by-default security gates** — blocking webhooks allow on failure with no opt-in, web auth defaults off, the secrets route gate is a no-op until web-login is configured. The localhost bind default is doing the load-bearing security work; one `bind_host` change exposes the lot.
5. **Broad `except Exception → defaults` mixes "absent" with "malformed"** in `load_bootstrap` — file-absent legitimately defaults, file-malformed should fail loud; the scalar-cast reset and the unreadable-config warning share the anti-pattern. Two crash-safe-write standards coexist (`bootstrap_io` atomic, `export_config_to_yaml` not).

## Verified non-bugs (cleared — don't re-chase)

- **Flat config-store secret hygiene is solid:** `_reject_plaintext_secret_value` refuses plaintext for secret-suffixed keys; secrets are Fernet-encrypted (PBKDF2-HMAC-SHA256, 600k iterations, machine_id+salt; salt file 0600 via `os.open`); the export route and `model_dump` emit secret *names* only; `database_url` has `exclude=True` so it never appears in dumps/exports/logs (grep-confirmed no `postgresql://` in any log line).
- **GET `/api/config/secrets` returns metadata only** (no values); `/api/config/values` masks secrets (`********`, test-covered); the CLI never prints secret values (char-count only, `hide_input`); the DSN password is redacted in CLI output (`_redact_dsn`).
- **Default `bind_host` is `localhost`** everywhere except the `run_server` signature default; bootstrap precedence is clean (no env preemption of bind/ports/DSN, unlike the GOBBY_PROJECT_ID issue); `_restore_bootstrap_backend_selection` forces the bootstrap DSN to win over any DB-stored value (prevents a poisoned DB row redirecting the runtime DSN).
- **bootstrap.yaml + mcp-servers.json are written/validated 0600 and atomically** (tmp+fsync+chmod-before-rename); the salt file too.
- **No shell injection in MCP stdio spawn** (`StdioServerParameters` exec, not shell — arbitrary `command` is operator-configured by design); embedding API key handling is sound (`$secret:` ref through SecretStore, plaintext rejected).
- **Substring/prefix false positives do NOT occur in the classifier** — `echo running pytest`/`cat pytest.log`/`git commit -m "run pytest"` correctly return no match (argv[0]-anchored, basename-aware); bare `true`/`echo`/`cd` don't classify; `ruff check --fix` is correctly excluded.
- **Port/probability validators are consistent and reject out-of-range values**; `ChatConfig.default_mode` defaults to safe `"plan"`; the well-validated modules (`servers`, `sessions`, `cron`, `pipelines`, `ui` limits, `feature_base`) enforce bounds and cross-field constraints.
- **`%s` placeholders are correct** per repo convention (CLAUDE.md's `$N` mandate is stale doc drift).
