# Type System — Reference

## Enum State Machines

Use enums where each variant carries only the data valid for that state:

```rust
// Bad: optional fields, caller must guess which are set
struct Order {
    status: String,
    items: Vec<Item>,
    shipped_at: Option<DateTime>,  // only valid when shipped
    tracking: Option<String>,      // only valid when shipped
    refund_reason: Option<String>, // only valid when refunded
}

// Good: each state carries exactly its data
enum Order {
    Draft { items: Vec<Item> },
    Confirmed { items: Vec<Item>, confirmed_at: DateTime },
    Shipped { items: Vec<Item>, shipped_at: DateTime, tracking: String },
    Refunded { reason: String },
}
```

## Newtype Pattern

Wrap primitives to prevent type confusion:

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct UserId(u64);

impl UserId {
    pub fn new(id: u64) -> Self {
        Self(id)
    }

    pub fn as_u64(&self) -> u64 {
        self.0
    }
}

// Now the compiler prevents mixing up user IDs and order IDs
fn assign_order(user: UserId, order: OrderId) { /* ... */ }
```

## Generics vs dyn Trait

```rust
// Generics — monomorphized, zero-cost, use by default
fn process<W: Write>(writer: &mut W, data: &[u8]) -> io::Result<()> {
    writer.write_all(data)
}

// dyn Trait — dynamic dispatch, use for:
// - heterogeneous collections
// - plugin systems
// - reducing binary size in non-hot paths
fn log_to(writers: &mut [Box<dyn Write>], msg: &[u8]) -> io::Result<()> {
    for w in writers {
        w.write_all(msg)?;
    }
    Ok(())
}
```

## Derive Best Practices

| Trait | When |
|-------|------|
| `Debug` | Always — required for error messages and logging |
| `Clone` | Value types, config structs, anything that needs copying |
| `PartialEq`, `Eq` | Types used in assertions, tests, or as map keys |
| `Hash` | Types used as `HashMap`/`HashSet` keys (requires `Eq`) |
| `Serialize`, `Deserialize` | Boundary types (API, config, storage) — not internal types |
| `Default` | Types with a meaningful zero/empty state |

## Type Aliases for Readability

```rust
// Crate-level Result alias — used throughout the crate
pub type Result<T> = std::result::Result<T, StorageError>;

// Domain-specific aliases
type NodeIndex = usize;
type AdjacencyList = Vec<Vec<NodeIndex>>;
```

## Typestate Pattern

Enforce protocol correctness at compile time:

```rust
struct Connection<S> { inner: TcpStream, _state: PhantomData<S> }
struct Disconnected;
struct Connected;
struct Authenticated;

impl Connection<Disconnected> {
    fn connect(addr: &str) -> Result<Connection<Connected>> { /* ... */ }
}

impl Connection<Connected> {
    fn authenticate(self, token: &str) -> Result<Connection<Authenticated>> { /* ... */ }
}

impl Connection<Authenticated> {
    fn query(&self, sql: &str) -> Result<Rows> { /* ... */ }
}

// Compile error: can't query without authenticating
// let conn = Connection::connect("localhost")?;
// conn.query("SELECT 1");  // no method `query` on Connection<Connected>
```
