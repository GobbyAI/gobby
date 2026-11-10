//! Integration suite for `gobby-terminal`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary. Shared
//! helpers are declared once here, so each module imports them from `crate`.

#[cfg(all(unix, feature = "vt-engine"))]
mod embed_support;

#[cfg(all(unix, feature = "vt-engine"))]
mod host_support;

#[path = "batch_paste.rs"]
mod batch_paste;

#[path = "build_env.rs"]
mod build_env;

#[path = "carve_guard.rs"]
mod carve_guard;

#[cfg(all(feature = "vt-engine", feature = "test-seams"))]
#[path = "control_protocol.rs"]
mod control_protocol;

#[cfg(feature = "vt-engine")]
#[path = "embed.rs"]
mod embed;

#[cfg(feature = "vt-engine")]
#[path = "frame_producer.rs"]
mod frame_producer;

#[cfg(feature = "vt-engine")]
#[path = "frame_protocol.rs"]
mod frame_protocol;

#[cfg(feature = "vt-engine")]
#[path = "handover_display.rs"]
mod handover_display;

#[cfg(feature = "vt-engine")]
#[path = "host_cli_args.rs"]
mod host_cli_args;

#[cfg(all(feature = "vt-engine", feature = "test-seams"))]
#[path = "host_handover.rs"]
mod host_handover;

#[cfg(feature = "vt-engine")]
#[path = "host_image.rs"]
mod host_image;

#[cfg(all(feature = "vt-engine", feature = "test-seams"))]
#[path = "host_lifecycle.rs"]
mod host_lifecycle;

#[cfg(all(unix, feature = "vt-engine"))]
#[path = "host_startup_wait.rs"]
mod host_startup_wait;

#[cfg(feature = "vt-engine")]
#[path = "link_artifacts.rs"]
mod link_artifacts;

#[path = "source_size.rs"]
mod source_size;

#[path = "terminal_theme.rs"]
mod terminal_theme;

#[path = "wire_codec_async.rs"]
mod wire_codec_async;

#[path = "wire_golden.rs"]
mod wire_golden;
