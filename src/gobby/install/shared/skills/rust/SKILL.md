---
name: rust
description: "Enforces default Rust coding standards for agents writing or refactoring Rust: Cargo and toolchain configuration, ownership, error handling, types, testing, async, and API design. Use before editing Rust unless the repo provides stricter local rules."
version: "1.2.0"
category: development
triggers: rust, cargo, cargo.toml, rust-toolchain, clippy, rustfmt, tokio, async rust, lifetime, borrow checker
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

## Configuration

- Preserve workspace and package boundaries in `Cargo.toml`; do not move dependency, feature, or resolver settings without checking all affected crates
- Keep feature flags additive and composable; avoid mutually exclusive features unless the crate already documents that pattern
- Respect `rust-toolchain.toml`, MSRV, edition, target triples, and platform-specific `.cargo/config.toml` settings before choosing language features
- Keep `rustfmt.toml` and `clippy.toml` aligned with the repository; do not relax lint or formatting settings to make a local change pass
- Validate the smallest affected crate and feature combination with `-p <package>` and explicit feature flags when applicable

For Cargo, toolchain, feature, and lint configuration patterns: `get_skill_file(name="rust", path="references/configuration.md")`

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
- Prefer `cargo nextest run` for normal test execution; run `cargo test --doc` separately because nextest does not run doctests
- Use `assert_cmd` for CLI subprocess tests, `pretty_assertions` for high-signal equality diffs, and `cargo-llvm-cov` for coverage reports
- Use `rstest` narrowly for genuine case tables and fixtures; do not replace clear single-case tests or helper loops with parameterization
- Use `insta` for stable text/JSON snapshots with redactions for nondeterministic fields
- Use `proptest` for parsers, serialization, deterministic IDs, and other pure algorithmic contracts
- In async tests, pause/control time with the runtime's time facilities instead of sleeping on wall-clock time
- Mock external I/O with trait objects or generics; do not default to heavy mock frameworks

Pytest-to-Rust mapping:

| Python/pytest | Rust default |
| --- | --- |
| `pytest` runner | `cargo nextest run` |
| doctests | `cargo test --doc` |
| `CliRunner` / subprocess assertions | `assert_cmd` |
| `@pytest.mark.parametrize` | narrow `rstest` `#[case]` tables |
| `@pytest.fixture` | `rstest` fixtures, explicit builders, and `Drop` teardown |
| `syrupy` snapshots | `insta` snapshots with redactions |
| `hypothesis` | `proptest` |
| `pytest.approx` / rich diffs | `pretty_assertions` plus domain-specific assertions |
| `coverage.py` | `cargo-llvm-cov` |

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
