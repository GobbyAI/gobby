# #22360 — Windows cross-target Clippy and CI failure fixes

## Candidate scope

Prepared in the primary `lane-5-rust` worktree on branch `lane-5-22360`,
from `677567f3086e231b308f30430a59c53b6c7ddd65`.

- Box front-door authentication and challenge rejection responses; move the
  response out of its box at the HTTP boundary. This addresses the CI
  `result_large_err` diagnostics without suppressions or response changes.
  The current base renamed `hygiene.rs` to `challenge.rs`.
- Bump `gobby-daemon` from `0.4.11` to `0.4.12`, including Cargo.lock and its
  managed binary pin. Activation is restart class and belongs to the Orchestrator.
- Derive the checkout-mode fixture's applied count from the registered migrations
  remaining after its pre-460 boundary. Preserve mode/settings checks and the
  second-apply zero-migration assertion.
- Add the `terminal_ws` native parity entry and an HTTP backend-down corpus case.
  Scope the negative parity unit fixture to its explicitly registered health
  family; the separate coverage precheck still checks every production family.
- Hold non-listening derived backend sockets throughout the TLS certificate
  reuse fixture. Reject public or derived ports 60891/60892 and overlaps between
  a new backend and an already selected public port. Previously the
  fixture assumed unreserved backend ports were down and intermittently got 200
  where it expected 503.

Source evidence uses scoped direct reads/diffs: installed gcode targets the
production hub, which this lane is prohibited from accessing.

## Observed GitHub failures

Run: https://github.com/GobbyAI/gobby/actions/runs/37875536068
Pushed SHA: `011337b7f48bc89869ae1168b118f32f77f7f75f`.

- Check & Test job `113643155051`: daemon Clippy failed with Rust 1.99.0
  `result_large_err` at `front_door/auth.rs:126` and `front_door/hygiene.rs:42`.
  Windows cross-target Clippy was **skipped**, not successful.
- PostgreSQL-backed Rust tests job `113643154987`: checkout-mode regression
  expected one applied migration and got five; 48 passed, one failed.

These are failure diagnostics, not criterion-satisfying successful CI evidence.
Task closure still requires the successful GitHub run after the candidate lands
and Josh authorizes the next push. No toolchain was installed, and this session
did not push, restart, or promote installed binaries.

## Local validation

Installed toolchain: `rustc 1.97.0 (2d8144b78 2026-07-07)`, configured stable.
The Windows target and Clippy were already installed. All heavy commands were
Monitor-admitted, sequential, nice 15, and limited to four Cargo build jobs.

```sh
nice -n 15 env CARGO_BUILD_JOBS=4 GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-core --features postgres -E 'test(checkout_mode_migration_preserves_modes_and_agent_settings)' --status-level fail
nice -n 15 env CARGO_BUILD_JOBS=4 cargo clippy -p gobby-daemon --all-targets -- -D warnings
nice -n 15 env CARGO_BUILD_JOBS=4 cargo nextest run -p gobby-daemon --test front_door --test http_contracts --status-level fail
nice -n 15 env CARGO_BUILD_JOBS=4 cargo clippy -p gobby-terminal --target x86_64-pc-windows-msvc --all-targets -- -D warnings
cargo fmt -p gobby-core -p gobby-daemon -- --check
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_version_pins.py
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -k 'loader or mask_vector or manifest_contract or secret_redaction'
uv run ruff check src/gobby/install/version_pins.py
uv run ruff format --check src/gobby/install/version_pins.py
uv run mypy src/gobby/install/version_pins.py
uv run gobby test-types suppressions src/gobby/install/version_pins.py --baseline .gobby/python-suppressions-baseline.json
git diff --check
```

- Schema regression RED: six applied versus one expected, exit 100.
  Final formatted GREEN: one passed, exit 0;
  nextest run `43588ec8-8a1c-439d-a75e-634bdcebf915`.
- Initial HTTP run: 26 passed, two terminal parity prechecks failed.
  Intermediate scoped rerun: TLS fixture returned 200 instead of 503.
  Final unfiltered GREEN: 28 passed, zero skipped, exit 0;
  nextest run `d32b961a-023a-4d5d-9456-273bd0112460` after the final port-overlap guard.
- Final daemon and Windows Clippy: no issues, exit 0.
- Cargo formatting, Ruff, mypy and diff checks: exit 0.
- Version-pin pytest: four passed. Corpus unit pytest: five passed.
- Suppression ratchet: one file, zero suppressions, zero new/stale entries.

The local Clippy result does not claim to reproduce Rust 1.99.0. The next
successful CI run remains the independent toolchain/platform confirmation.
