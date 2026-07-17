# Testing - Reference

## Test Ownership

Use the repo's existing pytest stack. Respect local markers, daemon isolation rules, fixture style, and wrapper commands. Target changed behavior first; broaden only when the blast radius justifies it.

## Fixtures with Yield

```python
@pytest.fixture
def db() -> Generator[Database, None, None]:
    database = Database(":memory:")
    database.connect()
    yield database
    database.disconnect()
```

Keep fixtures narrow. Prefer fixture factories when each test needs slightly different setup.

## Parameterized Tests

```python
@pytest.mark.parametrize("email,valid", [
    ("user@example.com", True),
    ("no-at-sign", False),
    ("", False),
])
def test_email_validation(email: str, valid: bool) -> None:
    assert is_valid_email(email) == valid
```

Use `pytest.param` for cases that need IDs or marks:

```python
@pytest.mark.parametrize("val,expected", [
    pytest.param(0, False, id="zero"),
    pytest.param(-1, False, id="negative"),
    pytest.param(1, True, id="positive"),
])
def test_is_positive(val: int, expected: bool) -> None:
    assert (val > 0) == expected
```

Use parameterization for cases that share the same behavior. Split tests when setup, behavior, or assertions diverge.

## Mocking External Dependencies

Patch where the thing is **used**, not where it's defined:

```python
# myapp/service.py imports requests
# Patch in myapp.service, not in requests
@patch("myapp.service.requests.get")
def test_fetch_user(mock_get: Mock) -> None:
    mock_get.return_value.json.return_value = {"id": 1, "name": "Test"}
    mock_get.return_value.raise_for_status = Mock()

    user = fetch_user(1)

    assert user["name"] == "Test"
    mock_get.assert_called_once()
```

Prefer fakes or boundary adapters when mocks become large. Do not mock the helper being tested.

## Testing Error Paths

```python
def test_fetch_retries_on_transient_error() -> None:
    client = Mock()
    client.request.side_effect = [
        ConnectionError("down"),
        ConnectionError("still down"),
        {"status": "ok"},
    ]
    service = RetryingService(client, max_retries=3)
    assert service.fetch() == {"status": "ok"}
    assert client.request.call_count == 3

def test_fetch_raises_after_max_retries() -> None:
    client = Mock()
    client.request.side_effect = ConnectionError("down")
    service = RetryingService(client, max_retries=3)
    with pytest.raises(ConnectionError):
        service.fetch()
```

Assert the error type and the important message or machine-readable fields. Test validation failures and cleanup behavior explicitly.

## Async Tests

```python
@pytest.mark.asyncio
async def test_async_fetch() -> None:
    async with AsyncClient(app=app) as client:
        response = await client.get("/users/1")
        assert response.status_code == 200
```

Await every async operation under test. Use fake clocks, controlled events, or short explicit timeouts instead of real sleeps.

## Markers

```python
@pytest.mark.slow          # Expensive tests
@pytest.mark.integration   # Requires external services
@pytest.mark.e2e           # Full system tests

# Run only fast tests:  pytest -m "not slow"
# Run integration:       pytest -m integration
```

Do not hide slow or environment-dependent tests under ordinary unit markers. Tests that need daemons, network, or databases should use isolated fixtures.

## Freezing Time

```python
from freezegun import freeze_time

@freeze_time("2026-01-15 10:00:00")
def test_token_expiry() -> None:
    token = create_token(expires_in=3600)
    assert token.expires_at == datetime(2026, 1, 15, 11, 0, 0)
```

## Filesystem And Paths

Use `tmp_path` for filesystem writes:

```python
def test_writes_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, {"enabled": True})
    assert path.read_text(encoding="utf-8")
```

Avoid touching the user's real home directory, daemon state, cache, credentials, or global temp paths.

## Validation Commands

Use repo wrappers first. Apply formatter fixes with:

```bash
uv run ruff format <files>
```

Collect non-mutating completion evidence with:

```bash
uv run ruff format --check <files>
uv run ruff check <files>
uv run mypy <package-or-files>
GOBBY_TEST_PROTECT=1 uv run pytest <tests> -q
```

Match the repo's validation gates before closing work. If you changed tests, run any configured test-quality audit for those files.
