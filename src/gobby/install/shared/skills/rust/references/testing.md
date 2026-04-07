# Testing — Reference

## Unit Test Layout

```rust
// src/parser.rs
pub fn parse_value(input: &str) -> Result<Value> {
    // ...
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_integer() {
        let result = parse_value("42").unwrap();
        assert_eq!(result, Value::Integer(42));
    }

    #[test]
    fn parse_empty_input() {
        assert!(parse_value("").is_err());
    }
}
```

## Result-Returning Tests

Prefer `-> Result<()>` over `.unwrap()` chains for cleaner failure output:

```rust
#[test]
fn roundtrip_serialization() -> anyhow::Result<()> {
    let original = Config { workers: 4, port: 8080 };
    let json = serde_json::to_string(&original)?;
    let restored: Config = serde_json::from_str(&json)?;
    assert_eq!(original, restored);
    Ok(())
}
```

## Integration Test Structure

```text
tests/
├── common/
│   └── mod.rs          # Shared helpers, test fixtures
├── api_integration.rs  # One file per feature area
└── storage_tests.rs
```

```rust
// tests/common/mod.rs
pub fn test_db() -> Database {
    Database::open(":memory:").expect("test db")
}

// tests/storage_tests.rs
mod common;

#[test]
fn insert_and_retrieve() {
    let db = common::test_db();
    db.insert("key", "value").unwrap();
    assert_eq!(db.get("key").unwrap(), Some("value".into()));
}
```

## Test Fixtures with Builders

```rust
#[cfg(test)]
fn test_user() -> User {
    User {
        id: UserId::new(1),
        name: "Test User".into(),
        email: "test@example.com".into(),
        role: Role::Member,
    }
}

#[test]
fn admin_can_delete() {
    let admin = User { role: Role::Admin, ..test_user() };
    assert!(admin.can_delete(&test_user()));
}
```

## Property-Based Testing with proptest

```rust
use proptest::prelude::*;

proptest! {
    #[test]
    fn roundtrip_encode_decode(input in any::<Vec<u8>>()) {
        let encoded = encode(&input);
        let decoded = decode(&encoded).unwrap();
        prop_assert_eq!(input, decoded);
    }

    #[test]
    fn parse_never_panics(s in "\\PC*") {
        // We don't care about the result, just that it doesn't panic
        let _ = parse_value(&s);
    }
}
```

## Trait-Based Mocking

Define a trait for the dependency, implement it for tests:

```rust
trait Clock {
    fn now(&self) -> DateTime<Utc>;
}

struct SystemClock;
impl Clock for SystemClock {
    fn now(&self) -> DateTime<Utc> { Utc::now() }
}

#[cfg(test)]
struct FixedClock(DateTime<Utc>);

#[cfg(test)]
impl Clock for FixedClock {
    fn now(&self) -> DateTime<Utc> { self.0 }
}

#[test]
fn token_expires_after_one_hour() {
    let clock = FixedClock(Utc.with_ymd_and_hms(2026, 1, 15, 10, 0, 0).unwrap());
    let token = Token::create(&clock, Duration::hours(1));
    assert_eq!(token.expires_at, Utc.with_ymd_and_hms(2026, 1, 15, 11, 0, 0).unwrap());
}
```

## Snapshot Testing with insta

```rust
use insta::assert_snapshot;

#[test]
fn format_error_message() {
    let err = ValidationError::new("email", "must contain @");
    assert_snapshot!(err.to_string());
}

#[test]
fn serialize_response() {
    let resp = build_response(test_data());
    insta::assert_json_snapshot!(resp);
}
```
