# API Versioning and Robustness Plan

This plan outlines the steps to introduce versioning (`/api/v1.0`) to the Gobby Daemon API, replacing the current flat structure. This is a **breaking change** prioritized for codebase cleanliness.

## Goal
Migrate all API endpoints from root-level prefixes (e.g., `/mcp`, `/sessions`) to a versioned namespace: `/api/v1.0/...`.

## 1. Constants and Configuration
*   **File**: `src/gobby/servers/routes/__init__.py` (or new `constants.py`)
*   **Change**: Define `API_V1_PREFIX = "/api/v1.0"`.

## 2. Server Update
*   **File**: `src/gobby/servers/http.py`
*   **Method**: `_register_routes`
*   **Change**: Wrap the inclusion of routers to use the prefix.
    ```python
    # Before
    app.include_router(create_mcp_router())
    
    # After
    from gobby.servers.routes import API_V1_PREFIX
    api_v1_router = APIRouter(prefix=API_V1_PREFIX)
    api_v1_router.include_router(create_mcp_router()) # resulting in /api/v1.0/mcp/...
    # ... include others ...
    app.include_router(api_v1_router)
    ```
    *Alternative*: Pass `prefix` to `create_mcp_router` if flexible, but nesting routers is cleaner for grouping.

## 3. Client and SDK Updates
*   **File**: `src/gobby/utils/daemon_client.py` (and potentially others like `GobbyClient` if exists)
*   **Change**: Update the base URL or request construction to include `/api/v1.0`.

## 4. Test Updates
*   **Scope**: Global `grep` for `/mcp/`, `/sessions/`, `/hooks/`, etc.
*   **Action**: Update all test fixtures and hardcoded URL strings to use the new prefix or a shared test constant.
*   **Affected Files**:
    *   `tests/servers/test_http_server.py`
    *   `tests/e2e/*`
    *   `tests/cli/*`

## 5. Backward Compatibility and Safe Transition Plan

A safe transition strategy ensures external consumers and SDKs can migrate without disruption.

### 5.1 Deprecation Timeline

| Phase | Duration | Action |
|-------|----------|--------|
| **Phase A: Redirect** | 4 weeks | Old paths return `301 Moved Permanently` to `/api/v1.0/...` |
| **Phase B: Deprecate** | 4 weeks | Redirects continue + `Deprecation` header + warning logs |
| **Phase C: Remove** | After Phase B | Old paths return `410 Gone` with migration guide URL |

### 5.2 301 Redirect Implementation

```python
# src/gobby/servers/routes/compat.py
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from gobby.servers.routes import API_V1_PREFIX

DEPRECATED_PREFIXES = ["/mcp", "/sessions", "/hooks", "/tasks", "/workflows"]

def create_compat_router() -> APIRouter:
    """Temporary router for backward-compatible redirects."""
    router = APIRouter()

    for prefix in DEPRECATED_PREFIXES:
        @router.api_route(f"{prefix}/{{path:path}}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        async def redirect_handler(request: Request, path: str, _prefix: str = prefix):
            new_url = f"{API_V1_PREFIX}{_prefix}/{path}"
            if request.query_params:
                new_url += f"?{request.query_params}"
            logger.warning(f"Deprecated path accessed: {request.url.path} -> {new_url}")
            return RedirectResponse(url=new_url, status_code=301)

    return router
```

### 5.3 Deprecation Headers

During Phase B, add headers to redirect responses:

```python
headers = {
    "Deprecation": "true",
    "Sunset": "2025-06-01",  # Adjust based on timeline
    "Link": f'<{API_V1_PREFIX}{path}>; rel="successor-version"',
}
```

### 5.4 Feature Flag Rollout

Enable versioning gradually using `DaemonConfig`:

```yaml
# ~/.gobby/config.yaml
api:
  versioning:
    enabled: true           # Master switch for /api/v1.0 paths
    redirect_legacy: true   # Enable 301 redirects for old paths
    deprecation_warnings: true  # Log warnings for legacy access
```

```python
# src/gobby/config/app.py
class ApiVersioningConfig(BaseModel):
    enabled: bool = False
    redirect_legacy: bool = True
    deprecation_warnings: bool = True
```

### 5.5 Communication Plan

| Audience | Channel | Content |
|----------|---------|---------|
| Internal team | `CHANGELOG.md` | Version bump with migration notes |
| SDK consumers | GitHub Release | Breaking change announcement, migration guide link |
| API users | `/api/v1.0/docs` | Interactive migration helper in OpenAPI docs |
| Monitoring | Logs + metrics | Track legacy endpoint usage for removal timing |

### 5.6 Rollback Procedure

If issues arise after enabling versioned paths:

1. **Immediate**: Set `api.versioning.enabled: false` in config, restart daemon
2. **Verify**: Confirm old paths respond without redirect
3. **Investigate**: Check logs for redirect loop or client compatibility issues
4. **Communicate**: Post status update if external consumers affected

### 5.7 Migration Checklist for Consumers

- [ ] Update base URL from `http://localhost:PORT/` to `http://localhost:PORT/api/v1.0/`
- [ ] Test all API calls against new paths
- [ ] Update any hardcoded paths in scripts or configs
- [ ] Verify SDK version supports new paths (if applicable)
- [ ] Remove reliance on redirects before Phase C deadline

## Execution Order
1.  Define Constant.
2.  Update Server mounting.
3.  Update Client.
4.  Fix Tests (bulk update).
