---
name: rust
description: "Enforces default Rust coding standards for agents writing or refactoring Rust: ownership, error handling, types, testing, async, and API design. Use before editing Rust unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: rust, cargo, clippy, rustfmt, tokio, async rust, lifetime, borrow checker
---

# Rust

Default coding standards for Rust. **Repo conventions and configured tooling take precedence** — if `Cargo.toml`, `clippy.toml`, `CLAUDE.md`, or project rules specify stricter or different standards, follow those instead.

---

## Tooling

Run the repo's configured lint, format, and test commands before finishing. If none are configured, use:

- **Format**: `cargo fmt --check` (fix with `cargo fmt`)
- **Lint**: `cargo clippy -- -D warnings`
- **Tests**: `cargo test` targeting changed crates, not the full workspace
- **Bench**: `cargo bench` if performance-sensitive code changed

Don't suppress clippy lints with `#[allow(...)]` without a documented reason.

## Ownership & Borrowing

- Prefer borrowing (`&T`, `&mut T`) over cloning — clone only when ownership transfer is genuinely needed
- Accept `&str` not `&String`, `&[T]` not `&Vec<T>`, `&Path` not `&PathBuf` in function parameters
- Use `Cow<'_, str>` when a function sometimes allocates and sometimes does not
- Keep lifetimes simple — if you need more than two named lifetime parameters, restructure
- Never `.clone()` to silence the borrow checker without understanding why it complains

For patterns and examples: `get_skill_file(name="rust", path="references/ownership.md")`

## Error Handling

- Libraries: define error enums with `thiserror`. Applications: use `anyhow` with `.context()`
- Propagate with `?` — avoid manual `match` on `Result` just to re-wrap
- No `.unwrap()` outside tests and cases with a proof comment explaining why it cannot fail
- Use `Option` for absence, `Result` for failure — do not conflate them
- Convert foreign errors at crate boundaries with `From` impls

For error type patterns: `get_skill_file(name="rust", path="references/error-handling.md")`

## Type System

- Use enums to make invalid states unrepresentable — not structs with optional fields
- Newtype pattern for domain concepts (`struct UserId(u64)`) to prevent type confusion
- Prefer generics + trait bounds over `dyn Trait` unless you need runtime polymorphism
- Derive `Debug` on everything. Add `Clone`, `PartialEq`, `Eq`, `Hash` where appropriate
- Do not introduce `Box<dyn Any>` — find the concrete type or use an enum

For type modeling patterns: `get_skill_file(name="rust", path="references/types.md")`

## Testing

- Unit tests in `#[cfg(test)] mod tests` in the same file
- Integration tests in `tests/` directory, one file per major feature
- Use `-> Result<()>` for tests with fallible operations instead of `.unwrap()` chains
- Property-based testing with `proptest` for parsing, serialization, or algorithmic code
- Mock external I/O with trait objects or generics — not heavy mock frameworks

For testing patterns: `get_skill_file(name="rust", path="references/testing.md")`

## Async

- Use tokio as the default runtime unless the project specifies otherwise
- CPU-bound work goes in `spawn_blocking`, never in async tasks
- All async I/O must have explicit timeouts (`tokio::time::timeout`)
- Understand `Send + Sync` bounds — do not sprinkle them to silence the compiler
- Use `tokio::select!` with cancellation safety awareness

For async patterns: `get_skill_file(name="rust", path="references/async.md")`

## Performance

- **Profile before optimizing** — use `cargo flamegraph` or `criterion` benchmarks
- Prefer iterator chains over indexed loops — they compile to the same machine code
- Pre-allocate with `Vec::with_capacity` when size is known or estimable
- Avoid unnecessary allocations: `&str` over `String`, `&[T]` over `Vec<T>` in read paths

For profiling tools and optimization patterns: `get_skill_file(name="rust", path="references/performance.md")`

## API & Design

- Implement `Display` for user-facing types, `Debug` for all types
- Use builder pattern for structs with more than 3-4 optional fields
- Implement `From`, not `Into` — you get `Into` for free
- Accept generics at boundaries: `impl AsRef<Path>`, `impl Into<String>`
- Unsafe: isolate in minimal blocks, document invariants with `// SAFETY:` comments, prefer safe abstractions from established crates (`bytemuck`, `zerocopy`, `crossbeam`)

## Before You Finish

If you touched Rust: verify `cargo fmt`, `cargo clippy`, and targeted `cargo test` pass before closing your work.
