---
name: rust
description: "Enforces default Rust coding standards for agents writing or refactoring Rust: Cargo and toolchain configuration, ownership, error handling, types, testing, async, and API design. Use before editing Rust unless the repo provides stricter local rules."
version: "1.4.0"
category: development
triggers: rust, cargo, cargo.toml, rust-toolchain, clippy, rustfmt, tokio, async rust, lifetime, borrow checker
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Rust workspace, ownership, and async conventions."
  - "Secondary: Rust API guidelines, clippy/rustfmt policy, and the tokio/thiserror/anyhow idioms used across Gobby's crates/ workspace."
---

# Rust

Apply repository Cargo, toolchain, MSRV, feature, lint, and target rules first.

## Tooling

- Use configured `cargo fmt`, Clippy with repository severity, focused `cargo test`
  or nextest, doctests, and benchmarks for performance-sensitive changes.

## Configuration

- Preserve workspace/package boundaries, resolver, feature composition, edition,
  MSRV, target triples, and `.cargo` configuration.
- Diagnostic hook: treat borrow-checker and Clippy findings as ownership evidence;
  avoid cloning, added `Send + Sync`, or `#[allow]` solely to silence diagnostics.

For Cargo, toolchain, features, and lint:
`get_skill_file(name="rust", path="references/configuration.md")`

## Ownership And Borrowing

- Prefer borrowing to cloning and accept slices, paths, and string slices at read-only
  boundaries.
- Use `Cow` for conditional ownership and restructure APIs when lifetimes become opaque.

For ownership patterns:
`get_skill_file(name="rust", path="references/ownership.md")`

## Error Handling

- Use typed library errors and contextual application errors, propagating with `?`.
- Reserve `unwrap` for tests or locally proven invariants and convert foreign errors
  at crate boundaries.

For error contracts:
`get_skill_file(name="rust", path="references/error-handling.md")`

## Type System

- Use enums and newtypes to prevent invalid states and domain confusion.
- Prefer generics and trait bounds until runtime polymorphism is a real requirement.

For type modeling:
`get_skill_file(name="rust", path="references/types.md")`

## Testing

- Keep unit tests with the module and integration tests at crate boundaries.
- Use controlled runtime time, `assert_cmd`, snapshots with redaction, property tests,
  and repository coverage tooling where each matches the changed contract.

For Rust test patterns:
`get_skill_file(name="rust", path="references/testing.md")`

## Concurrency

- Use the repository runtime, put CPU-bound work behind blocking pools, and apply
  timeouts to owned I/O.
- Make cancellation safety and `Send + Sync` requirements explicit around spawned work.

For async and cancellation patterns:
`get_skill_file(name="rust", path="references/async.md")`

## Performance

- Use flamegraphs or benchmarks to justify allocation, preallocation, iterator,
  representation, or dispatch changes.

For profiling and optimization:
`get_skill_file(name="rust", path="references/performance.md")`

## API Design

- Implement `Display` for user-facing values and `Debug` for diagnostic values.
- Use builders when optional construction state would otherwise obscure invariants.
- Implement `From` and accept narrow ergonomic bounds such as `AsRef<Path>`.
- Isolate unsafe code and document each required invariant with `// SAFETY:`.
