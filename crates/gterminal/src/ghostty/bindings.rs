//! Checked-in rust-bindgen output for the vendored libghostty-vt C ABI.
//!
//! Regenerate after re-pinning `vendor/libghostty-vt`, from the workspace root:
//!
//! ```sh
//! LIBCLANG_PATH=/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib \
//!   bindgen crates/gterminal/vendor/libghostty-vt/include/ghostty/vt.h \
//!     --allowlist-item '^(ghostty|GHOSTTY|Ghostty).*' \
//!     --blocklist-item '^GHOSTTY_ENUM_MAX_VALUE$' \
//!     --with-derive-default \
//!     -- -I crates/gterminal/vendor/libghostty-vt/include
//! ```
//!
//! Pinned to `bindgen 0.72.1` (`cargo install bindgen-cli --version 0.72.1`).
//! `ghostty.h` is deliberately not an input: it declares the full application
//! API, which this crate does not link. `GHOSTTY_ENUM_MAX_VALUE` is an
//! enum-width padding macro with no meaning on the Rust side.
//!
//! The single output is split in order into `bindings/generated_*.rs`, each
//! under 850 lines, cut only between complete top-level items; concatenating
//! them reproduces the bindgen output byte for byte. Edit nothing here by
//! hand — rerun the command above and re-split.

#![allow(
    dead_code,
    non_camel_case_types,
    non_snake_case,
    non_upper_case_globals,
    clippy::all,
    rustdoc::all
)]

include!("bindings/generated_01.rs");
include!("bindings/generated_02.rs");
include!("bindings/generated_03.rs");
include!("bindings/generated_04.rs");
include!("bindings/generated_05.rs");
include!("bindings/generated_06.rs");
include!("bindings/generated_07.rs");
include!("bindings/generated_08.rs");
