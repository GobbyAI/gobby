//! Sidebar states must preserve deliberate pauses and user interrupts.

use gobby_client::app::sidebar_model::agent_state;
use gobby_client::daemon::RosterEntry;
use gobby_client::ui::chrome::{Chrome, RowState};
use gobby_client::ui::keybind_help::legend_groups;
use gobby_client::ui::status::state_dot;

fn seat(status: &str) -> RosterEntry {
    RosterEntry {
        entry_id: "session:seat".into(),
        lifecycle_status: Some(status.into()),
        ..Default::default()
    }
}

#[test]
fn explicit_pause_keeps_a_distinct_state_and_glyph() {
    let palette = Chrome::dark().palette;
    let paused = agent_state(&seat("paused"), None);
    let idle = agent_state(&seat("completed"), None);

    assert_eq!(paused, RowState::Paused);
    assert_ne!(paused, idle);
    assert_ne!(state_dot(paused, &palette).0, state_dot(idle, &palette).0);
}

#[test]
fn user_interrupt_has_a_distinct_state_and_glyph_from_normal_idle() {
    let palette = Chrome::dark().palette;
    let interrupted = agent_state(&seat("interrupted"), None);
    let idle = agent_state(&seat("completed"), None);

    assert_ne!(interrupted, idle, "a user interrupt must remain visible");
    assert_ne!(
        state_dot(interrupted, &palette).0,
        state_dot(idle, &palette).0,
        "the interrupt needs a distinct glyph, independent of color"
    );
}

#[test]
fn help_explains_the_state_drawn_for_a_user_interrupt() {
    let chrome = Chrome::dark();
    let interrupted = agent_state(&seat("interrupted"), None);
    let glyph = state_dot(interrupted, &chrome.palette).0;
    let legend: Vec<String> = legend_groups(&chrome, u16::MAX)
        .iter()
        .flatten()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect()
        })
        .collect();

    assert!(
        legend.iter().any(|row| {
            row.trim_start().starts_with(glyph) && row.to_lowercase().contains("interrupt")
        }),
        "help must explain the daemon's user-interrupted state: {legend:?}"
    );
}
