//! Integration suite for `gobby-client`.
//!
//! Each top-level test file is one module of this program, so the crate builds a
//! single integration test binary. A module that needs a feature carries its `cfg`
//! here, where its former `required-features` gated a separate binary.

// Shared helpers load once here; every test module reaches them through `super`.
#[path = "mock_daemon/mod.rs"]
mod mock_daemon;

#[path = "mock_daemon/draw.rs"]
mod draw;

#[path = "arrange.rs"]
mod arrange;

#[path = "arrange_refs.rs"]
mod arrange_refs;

#[path = "attention_flow.rs"]
mod attention_flow;

#[path = "client_loop.rs"]
mod client_loop;

#[path = "command_mode.rs"]
mod command_mode;

#[path = "copy_paste.rs"]
mod copy_paste;

#[path = "daemon_live.rs"]
mod daemon_live;

#[path = "frame_delivery.rs"]
mod frame_delivery;

#[path = "frame_source_live.rs"]
mod frame_source_live;

#[path = "held_workspace_ops.rs"]
mod held_workspace_ops;

#[path = "host_upgrade_recovery.rs"]
mod host_upgrade_recovery;

#[path = "info_dialogs.rs"]
mod info_dialogs;

#[path = "keybind_help.rs"]
mod keybind_help;

#[path = "keymap.rs"]
mod keymap;

#[path = "lease_control.rs"]
mod lease_control;

#[path = "logging.rs"]
mod logging;

#[path = "loop_liveness.rs"]
mod loop_liveness;

#[path = "marks.rs"]
mod marks;

#[path = "menu_bar.rs"]
mod menu_bar;

#[path = "outage_direct_input.rs"]
mod outage_direct_input;

// 4.1: upstream keep-set render-test parity suite. The harness lives in `parity/mod.rs`, so
// nextest lists every ported test as `parity::<module>::<name>`.
#[path = "parity/mod.rs"]
mod parity;

#[path = "placed_agent.rs"]
mod placed_agent;

#[path = "project_flows.rs"]
mod project_flows;

#[path = "proxy_promotion.rs"]
mod proxy_promotion;

#[path = "reconciliation.rs"]
mod reconciliation;

#[path = "roster_machines.rs"]
mod roster_machines;

#[path = "row_states.rs"]
mod row_states;

#[path = "screens.rs"]
mod screens;

#[path = "select_live_switch.rs"]
mod select_live_switch;

#[path = "sidebar_model.rs"]
mod sidebar_model;

#[path = "source_size.rs"]
mod source_size;

#[path = "splash.rs"]
mod splash;

#[path = "startup.rs"]
mod startup;

#[path = "startup_latency.rs"]
mod startup_latency;

#[path = "startup_stages.rs"]
mod startup_stages;

#[path = "teardown.rs"]
mod teardown;

#[path = "terminal_links.rs"]
mod terminal_links;

#[path = "terminal_naming.rs"]
mod terminal_naming;

#[path = "theme.rs"]
mod theme;

#[path = "theme_catalog.rs"]
mod theme_catalog;

#[path = "turn_status.rs"]
mod turn_status;

#[path = "ui_carve_guard.rs"]
mod ui_carve_guard;

#[path = "workspace.rs"]
mod workspace;

#[path = "workspace_rows.rs"]
mod workspace_rows;

#[path = "ws_golden.rs"]
mod ws_golden;
