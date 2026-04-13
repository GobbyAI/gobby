# Ownership & Borrowing — Reference

## Borrow Over Clone

```rust
// Bad: forces caller to allocate
fn greet(name: String) -> String {
    format!("Hello, {name}!")
}

// Good: borrows — caller keeps ownership
fn greet(name: &str) -> String {
    format!("Hello, {name}!")
}
```

## Slice Parameters

Accept the most general borrowed form:

```rust
// Bad: needlessly restrictive
fn sum(v: &Vec<i32>) -> i32 { v.iter().sum() }
fn starts_with_hello(s: &String) -> bool { s.starts_with("Hello") }

// Good: accepts Vec, arrays, slices
fn sum(v: &[i32]) -> i32 { v.iter().sum() }
fn starts_with_hello(s: &str) -> bool { s.starts_with("Hello") }
```

## Cow for Conditional Allocation

When a function sometimes modifies and sometimes passes through:

```rust
use std::borrow::Cow;

fn normalize_whitespace(input: &str) -> Cow<'_, str> {
    if input.contains("  ") {
        // Must allocate — input needs modification
        Cow::Owned(input.split_whitespace().collect::<Vec<_>>().join(" "))
    } else {
        // No allocation — return borrowed reference
        Cow::Borrowed(input)
    }
}
```

## Lifetime Elision Rules

You can omit lifetimes when:

1. Each reference parameter gets its own lifetime
2. If there's exactly one input lifetime, it applies to all output references
3. If `&self` or `&mut self` is a parameter, its lifetime applies to all output references

```rust
// Elided — rule 3 applies
fn name(&self) -> &str { &self.name }

// Must annotate — two input lifetimes, compiler can't choose
fn longest<'a>(a: &'a str, b: &'a str) -> &'a str {
    if a.len() >= b.len() { a } else { b }
}
```

## Restructuring to Avoid Lifetime Complexity

When a struct accumulates lifetime parameters, own the data instead:

```rust
// Before: two lifetimes, painful to propagate
struct Query<'a, 'b> {
    table: &'a str,
    filter: &'b str,
}

// After: owns its data, simpler API
struct Query {
    table: String,
    filter: String,
}
```

Use references in structs only when the struct is short-lived (iterators, parsers over a buffer).

## Split Borrow Pattern

Borrow different fields of a struct simultaneously:

```rust
struct State {
    items: Vec<Item>,
    log: Vec<String>,
}

fn process(state: &mut State) {
    // This works — compiler sees disjoint borrows
    for item in &mut state.items {
        state.log.push(format!("Processing {}", item.name));
    }
}
```

If the compiler can't see the split, destructure explicitly:

```rust
let State { items, log } = &mut state;
for item in items.iter_mut() {
    log.push(format!("Processing {}", item.name));
}
```
