# Auto-Detect CLIs, Auth Modes, and Models on Daemon Startup

## Context

Users currently must manually configure `llm_providers` in `config.yaml` with per-provider model lists, auth modes, and API keys. This is tedious, error-prone, and duplicates information the coding CLIs already store locally:

- Codex stores auth in `~/.codex/auth.json` and a full model list in `~/.codex/models_cache.json`
- Gemini stores OAuth creds in `~/.gemini/oauth_creds.json` with account info in `google_accounts.json`
- Claude Code manages subscription auth internally — its binary presence implies subscription availability

This change **replaces** the entire `llm_providers` config section with an auto-detection system that discovers CLIs, auth modes, and models at daemon startup. No deprecation path — full cleanup of the old system.

## Auth Detection Heuristics

| CLI | Subscription Detection | API Key Detection | Models (Subscription) | Models (API Key) |
|-----|----------------------|-------------------|-----------------------|------------------|
| **Claude** | `shutil.which("claude")` exists | `ANTHROPIC_API_KEY` in SecretStore or env | litellm `anthropic` models (filtered) | `GET api.anthropic.com/v1/models` |
| **Codex** | `~/.codex/auth.json` has `tokens` + `OPENAI_API_KEY: null` | `OPENAI_API_KEY` in SecretStore/env or non-null in `auth.json` | `~/.codex/models_cache.json` (slug field) | `GET api.openai.com/v1/models` |
| **Gemini** | `~/.gemini/oauth_creds.json` has `refresh_token` | `GOOGLE_API_KEY`/`GEMINI_API_KEY` in SecretStore or env | litellm `gemini` models (filtered) | `GET generativelanguage.googleapis.com/v1beta/models` |

Priority: subscription first, API key second. If CLI installed but neither auth works, add to `errors` list (not fatal).

## Data Model

```python
@dataclass
class ProviderEntry:
    cli: str              # "claude", "codex", "gemini"
    provider: str         # "anthropic", "openai", "google"
    auth_mode: str        # "subscription" or "api_key"
    models: list[str]     # available model slugs
    default_model: str | None
    display_name: str     # "claude (subscription)", "codex (api)"

@dataclass
class DiscoveryResult:
    providers: list[ProviderEntry]   # sorted: subscription first, then api
    detected_clis: dict[str, bool]   # {cli_name: installed}
    api_keys: dict[str, str]         # {env_var_name: value} resolved keys
    errors: list[str]                # graceful error messages
    timestamp: datetime

    def get_enabled_providers(self) -> list[str]:
        """Unique provider names (cli names). Replaces LLMProvidersConfig.get_enabled_providers()."""

    def get_provider_entry(self, cli: str, auth_mode: str | None = None) -> ProviderEntry | None:
        """Get entry by cli name, optionally filtered by auth_mode. Prefers subscription."""

    def get_auth_mode(self, cli: str) -> str | None:
        """Get preferred auth_mode for a cli (subscription preferred over api_key)."""

    def get_default_model(self, cli: str) -> str | None:
        """Get default model for a cli's preferred auth mode."""
```

## API Response

`GET /admin/providers` (replaces `/admin/models`):

```json
{
  "providers": [
    {"cli": "claude", "provider": "anthropic", "auth_mode": "subscription",
     "display_name": "claude (subscription)", "models": ["claude-opus-4-6", "claude-sonnet-4-5"], "default_model": "claude-opus-4-6"},
    {"cli": "codex", "provider": "openai", "auth_mode": "subscription",
     "display_name": "codex (subscription)", "models": ["gpt-5.2-codex", "gpt-5.1-codex"], "default_model": "gpt-5.2-codex"},
    {"cli": "gemini", "provider": "google", "auth_mode": "subscription",
     "display_name": "gemini (subscription)", "models": ["gemini-2.5-pro", "gemini-2.0-flash"], "default_model": "gemini-2.5-pro"},
    {"cli": "claude", "provider": "anthropic", "auth_mode": "api_key",
     "display_name": "claude (api)", "models": ["..."], "default_model": "claude-opus-4-6"}
  ],
  "default_provider": "claude",
  "default_auth_mode": "subscription"
}
```

## Implementation Phases

### Phase 1: Create discovery module (no existing code changes)

**New file: `src/gobby/llm/discovery.py`** (~300 lines)

Contains:
- `CLI_PROVIDER_MAP` — mapping of cli name to provider name
- `PROVIDER_KEY_NAMES` — mapping of provider to env var names to check
- `detect_installed_clis()` — OS-agnostic detection via `shutil.which()` + platform paths (extracted from `cli/install.py` lines 72-115)
- `_detect_*_auth()` — per-CLI auth detection reading local config files
- `_resolve_api_key(provider, db)` — resolve from SecretStore (category='llm') then env vars
- `_discover_subscription_models(cli)` — read Codex cache or litellm static registry
- `_discover_api_models(provider, api_key)` — async httpx calls to provider APIs (5s timeout, litellm fallback)
- `run_discovery(db)` returning `DiscoveryResult` — orchestrator
- `ProviderEntry` and `DiscoveryResult` dataclasses with helper methods

Key reuse:
- `SecretStore` from `src/gobby/storage/secrets.py` — `store.get(name)` for API key retrieval
- `litellm.models_by_provider` and `litellm.model_cost` — static model registry (already used in `admin.py`)
- Detection patterns from `src/gobby/cli/install.py` lines 72-115

### Phase 2: Wire discovery into daemon startup

**Modify: `src/gobby/servers/http.py`** — In the async lifespan (~line 435), after CodexAdapter init, before yield:

```python
from gobby.llm.discovery import run_discovery
app.state.discovery_result = await run_discovery(db=self.services.database)
```

Store on `app.state` for route access. Also store on `self` (HTTPServer) for service access.

### Phase 3: Refactor LLM stack to use DiscoveryResult

This is the core refactor — replace all `llm_providers` config access with `DiscoveryResult`.

**Modify: `src/gobby/llm/resolver.py`**
- `resolve_provider()` — replace step 3 (`config.llm_providers.get_enabled_providers()`) with `discovery.get_enabled_providers()`
- `_validate_provider_configured()` — validate against `DiscoveryResult` instead of `LLMProvidersConfig`
- `create_executor()` — get `auth_mode` from `DiscoveryResult.get_auth_mode(provider)` instead of `provider_config.auth_mode`
- `_create_*_executor()` functions — get default model from `DiscoveryResult.get_default_model()`, API keys from `DiscoveryResult.api_keys`
- `ExecutorRegistry.__init__` — accept `DiscoveryResult` instead of/alongside `DaemonConfig`
- Remove all `LLMProviderConfig` type references

**Modify: `src/gobby/llm/service.py`**
- `LLMService.__init__` — take `DiscoveryResult` instead of requiring `config.llm_providers`
- `_get_provider_instance()` — check `DiscoveryResult.get_enabled_providers()` instead of `config.llm_providers`
- `get_default_provider()` — use `DiscoveryResult`
- `enabled_providers` property — use `DiscoveryResult`

**Modify: `src/gobby/llm/factory.py`**
- `create_llm_service()` — accept `DiscoveryResult` parameter

**Modify: `src/gobby/llm/claude.py`**
- Constructor: get `auth_mode` from `DiscoveryResult` or explicit parameter instead of `config.llm_providers.claude.auth_mode`
- Remove fallback to config for auth_mode (lines 89-90)

**Modify: `src/gobby/llm/codex.py`**
- Same pattern as claude.py — auth_mode from `DiscoveryResult` (lines 65-66)

**Modify: `src/gobby/llm/gemini.py`**
- Same pattern — auth_mode from `DiscoveryResult` (lines 52-53)

**Modify: `src/gobby/llm/litellm.py`**
- Get API keys from `DiscoveryResult.api_keys` instead of `config.llm_providers.api_keys` (lines 63-64)

**Modify: `src/gobby/mcp_proxy/semantic_search.py`**
- Update error message referencing `llm_providers.api_keys` (line 462) to reference SecretStore

### Phase 4: Update admin endpoints and remove old model discovery

**Modify: `src/gobby/servers/routes/admin.py`**
- Add `GET /admin/providers` endpoint returning `DiscoveryResult` as JSON
- Remove `_discover_models()` function (lines 31-52)
- Remove `_fallback_models_from_config()` function (lines 55-66)
- Remove `GET /admin/models` endpoint (lines 363-404)
- Update status endpoint to include detected CLIs info

### Phase 5: Remove `llm_providers` config

**Modify: `src/gobby/config/app.py`**
- Remove `llm_providers: LLMProvidersConfig` field (line 232)
- Move `default_model` and `json_strict` to top-level `DaemonConfig` fields (they're behavior prefs, not provider config)
- Remove import of `LLMProvidersConfig`

**Delete: `src/gobby/config/llm_providers.py`** — entire module removed

**Modify: `src/gobby/runner.py`**
- Update `create_llm_service()` call to pass `DiscoveryResult` (line 113)
- Note: Runner init is sync; discovery runs later in async lifespan. LLM service init may need to be deferred to lifespan as well, or accept discovery lazily.

### Phase 6: Create adapter registry

**New file: `src/gobby/adapters/registry.py`** (~50 lines)

```python
ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "claude": ClaudeCodeAdapter,
    "gemini": GeminiAdapter,
    "codex": CodexNotifyAdapter,
}

def get_adapter(source: str, hook_manager, codex_adapter=None) -> BaseAdapter:
    """Get adapter for source. Raises HTTPException for unknown source."""
```

**Modify: `src/gobby/servers/routes/mcp/hooks.py`**
- Replace if-else chain (lines 112-144) with registry lookup
- Remove inline adapter imports

### Phase 7: Refactor `cli/install.py`

**Modify: `src/gobby/cli/install.py`**
- Remove `_is_*_installed()` private functions (lines 72-115)
- Import `detect_installed_clis` from `discovery.py`
- Update `install` command to use shared detection

### Phase 8: Update frontend

**Modify: `web/src/hooks/useSettings.ts`**
- Replace `ModelInfo` with `ProviderInfo` interface matching API response
- Fetch from `/admin/providers` instead of `/admin/models?provider=claude`
- Track selected `provider + auth_mode` pair (not just model)
- `updateModel` callback becomes `updateProvider(cli, auth_mode)` + `updateModel(model)`

**Modify: `web/src/components/Settings.tsx`**
- Replace flat model `<select>` with two-level selector:
  - Provider dropdown showing `display_name` values ("claude (subscription)", "codex (subscription)", etc.)
  - Model dropdown for selected provider
- Subscription providers appear first in the list

### Phase 9: Update tests

**Modify: `tests/servers/routes/test_admin_extended.py`**
- Update model endpoint tests (lines 74, 99) to use `/admin/providers`

**New: `tests/llm/test_discovery.py`**
- Mock `shutil.which` for CLI detection
- Mock filesystem (`auth.json`, `oauth_creds.json`, `models_cache.json`) for auth detection
- Mock SecretStore for API key resolution
- Mock httpx for provider API responses
- Test sorting: subscription entries before API entries
- Test error cases: CLI installed but no auth, API timeout fallback

**Update: existing LLM tests**
- Any tests using `LLMProvidersConfig` fixtures need updating to use `DiscoveryResult`

## Files Summary

| File | Action | Phase |
|------|--------|-------|
| `src/gobby/llm/discovery.py` | **CREATE** | 1 |
| `src/gobby/servers/http.py` | Modify — run discovery in lifespan | 2 |
| `src/gobby/llm/resolver.py` | Modify — use DiscoveryResult | 3 |
| `src/gobby/llm/service.py` | Modify — use DiscoveryResult | 3 |
| `src/gobby/llm/factory.py` | Modify — accept DiscoveryResult | 3 |
| `src/gobby/llm/claude.py` | Modify — auth_mode from discovery | 3 |
| `src/gobby/llm/codex.py` | Modify — auth_mode from discovery | 3 |
| `src/gobby/llm/gemini.py` | Modify — auth_mode from discovery | 3 |
| `src/gobby/llm/litellm.py` | Modify — api_keys from discovery | 3 |
| `src/gobby/mcp_proxy/semantic_search.py` | Modify — update error message | 3 |
| `src/gobby/servers/routes/admin.py` | Modify — new /admin/providers, remove old | 4 |
| `src/gobby/config/app.py` | Modify — remove llm_providers field | 5 |
| `src/gobby/config/llm_providers.py` | **DELETE** | 5 |
| `src/gobby/runner.py` | Modify — defer LLM service to lifespan | 5 |
| `src/gobby/adapters/registry.py` | **CREATE** | 6 |
| `src/gobby/servers/routes/mcp/hooks.py` | Modify — use adapter registry | 6 |
| `src/gobby/cli/install.py` | Modify — import from discovery.py | 7 |
| `web/src/hooks/useSettings.ts` | Modify — multi-provider model | 8 |
| `web/src/components/Settings.tsx` | Modify — grouped selector | 8 |
| `tests/llm/test_discovery.py` | **CREATE** | 9 |
| `tests/servers/routes/test_admin_extended.py` | Modify — update model tests | 9 |

## Verification

1. `uv run gobby restart --verbose` — logs show detected CLIs, auth modes, provider entries
2. `curl localhost:60887/admin/providers` — JSON with subscription-first sorted providers
3. Web UI Settings — provider dropdown with "(subscription)" and "(api)" labels, model dropdown per provider
4. `uv run pytest tests/llm/test_discovery.py -v` — all discovery tests pass
5. `uv run pytest tests/ -v` — no regressions from llm_providers removal
6. `uv run ruff check src/` — no lint errors
7. `uv run mypy src/` — no type errors
8. Confirm `config.yaml` no longer needs `llm_providers` section
