# Error Handling — Reference

## thiserror for Libraries

```rust
use thiserror::Error;

#[derive(Debug, Error)]
pub enum StorageError {
    #[error("record not found: {id}")]
    NotFound { id: String },

    #[error("connection failed: {0}")]
    Connection(#[from] std::io::Error),

    #[error("invalid data: {reason}")]
    InvalidData { reason: String },

    #[error("query failed")]
    Query(#[from] sqlx::Error),
}
```

## anyhow for Applications

```rust
use anyhow::{bail, Context, Result};

fn load_config(path: &Path) -> Result<Config> {
    let contents = std::fs::read_to_string(path)
        .context("failed to read config file")?;

    let config: Config = toml::from_str(&contents)
        .context("failed to parse config TOML")?;

    if config.workers == 0 {
        bail!("worker count must be positive");
    }

    Ok(config)
}
```

## The ? Operator Chain

Let `?` do the work — avoid manual match-and-rewrap:

```rust
// Bad: verbose, adds nothing
fn read_setting(path: &Path) -> Result<u32> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => return Err(e.into()),
    };
    let value = match text.trim().parse::<u32>() {
        Ok(v) => v,
        Err(e) => return Err(e.into()),
    };
    Ok(value)
}

// Good: clean chain
fn read_setting(path: &Path) -> Result<u32> {
    let text = std::fs::read_to_string(path)?;
    let value = text.trim().parse::<u32>()?;
    Ok(value)
}
```

## Error Context at Crate Boundaries

Wrap low-level errors with domain meaning:

```rust
pub fn get_user(id: UserId) -> Result<User, AppError> {
    db::fetch_row(id.as_ref())
        .map_err(|e| AppError::UserLookup {
            user_id: id,
            source: e,
        })
}
```

## When .unwrap() Is Acceptable

```rust
// In tests — failure means test failure, which is the point
#[test]
fn test_parse() {
    let result = parse_config(VALID_INPUT).unwrap();
    assert_eq!(result.name, "test");
}

// With a proof comment — the invariant is guaranteed
let first = non_empty_vec.first().expect("vec is non-empty by construction");
```

## Option Combinators

```rust
// Transform the inner value
let upper: Option<String> = name.map(|n| n.to_uppercase());

// Chain fallible operations
let port: Option<u16> = config
    .get("port")
    .and_then(|v| v.parse().ok());

// Convert Option to Result, then unwrap with ?
let port: u16 = config
    .get("port")
    .ok_or(AppError::MissingField("port"))?;

// Provide a default
let timeout = config.timeout.unwrap_or(Duration::from_secs(30));
let name = user.display_name.unwrap_or_default();
```
