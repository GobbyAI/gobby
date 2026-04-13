# Performance — Reference

## Profiling Tools

| Tool | When | Command |
|------|------|---------|
| **cargo flamegraph** | CPU profile visualization | `cargo flamegraph --bin myapp` |
| **criterion** | Microbenchmarks with stats | `cargo bench` (with criterion dep) |
| **dhat** | Heap allocation profiling | Instrument with `dhat` crate |
| **cargo-instruments** | macOS Instruments integration | `cargo instruments -t Allocations` |
| **perf** | Linux system profiling | `perf record --call-graph=dwarf target/release/myapp` |

## Iterator Chains

Iterator chains compile to the same machine code as hand-written loops — use them freely:

```rust
// Idiomatic — zero-cost abstraction
let total: u64 = records
    .iter()
    .filter(|r| r.is_active())
    .map(|r| r.amount)
    .sum();

// Equivalent hand-written loop — no faster
let mut total: u64 = 0;
for r in &records {
    if r.is_active() {
        total += r.amount;
    }
}
```

## Pre-Allocation

```rust
// Known size
let mut results = Vec::with_capacity(items.len());
for item in items {
    results.push(transform(item));
}

// Estimated size
let mut map = HashMap::with_capacity(estimated_entries);

// String building
let mut output = String::with_capacity(256);
for line in lines {
    output.push_str(line);
    output.push('\n');
}
```

## Avoiding Allocations in Read Paths

```rust
// Bad: allocates a String the caller doesn't need to own
fn name(&self) -> String { self.name.clone() }

// Good: borrow
fn name(&self) -> &str { &self.name }

// Bad: allocates a Vec for read-only access
fn items(&self) -> Vec<Item> { self.items.clone() }

// Good: return a slice
fn items(&self) -> &[Item] { &self.items }
```

## Criterion Benchmark

```rust
use criterion::{black_box, criterion_group, criterion_main, Criterion};

fn bench_parse(c: &mut Criterion) {
    let input = include_str!("../fixtures/large.json");
    c.bench_function("parse_json", |b| {
        b.iter(|| parse(black_box(input)))
    });
}

fn bench_compare(c: &mut Criterion) {
    let mut group = c.benchmark_group("serialization");
    group.bench_function("serde_json", |b| b.iter(|| serde_json::to_string(&data)));
    group.bench_function("simd_json", |b| b.iter(|| simd_json::to_string(&data)));
    group.finish();
}

criterion_group!(benches, bench_parse, bench_compare);
criterion_main!(benches);
```

## Common Anti-Patterns

```rust
// Bad: clone in a loop
for item in &items {
    let owned = item.clone(); // unnecessary if you only read
    process(&owned);
}

// Good: borrow
for item in &items {
    process(item);
}

// Bad: collect then iterate again
let filtered: Vec<_> = items.iter().filter(|i| i.active).collect();
let total: u64 = filtered.iter().map(|i| i.amount).sum();

// Good: chain without intermediate collection
let total: u64 = items.iter().filter(|i| i.active).map(|i| i.amount).sum();

// Bad: Box<dyn Trait> in a hot loop when the type is known
fn process(handler: &Box<dyn Handler>) { handler.handle(); }

// Good: generic — monomorphized, inlined
fn process(handler: &impl Handler) { handler.handle(); }
```
