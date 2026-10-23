//! Reports the focused pane's lease to the daemon, and lets that report expire
//! after `focus_hold_seconds` pass with no operator activity.

use std::time::{Duration, Instant};

use crossterm::event::MouseEventKind;
use gobby_terminal::raw_input::RawInputEvent;
use serde_json::{json, Value};

use super::super::{PaneId, Workspace};
use crate::daemon::LiveDaemon;
use crate::ui::Chrome;

/// Input that proves the operator is at the client (decision 16): a key, text,
/// paste, or any mouse event other than pure pointer motion. Scroll counts;
/// host colour replies and outer focus events do not.
pub(super) fn is_activity(event: &RawInputEvent) -> bool {
    match event {
        RawInputEvent::Key(_) | RawInputEvent::Text(_) | RawInputEvent::Paste(_) => true,
        RawInputEvent::Mouse(mouse) => mouse.kind != MouseEventKind::Moved,
        _ => false,
    }
}

/// The `terminal_focus` report `FocusHold::step` asks for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct FocusReport {
    pub(super) pane: PaneId,
    pub(super) attachment_id: String,
    pub(super) focused: bool,
}

pub(super) struct FocusHold {
    reported: Option<(PaneId, String)>,
    /// The report `step` returned last, so a refused send can be undone.
    in_flight: Option<FocusReport>,
    /// An unfocus the daemon refused. It is sent again on the next tick only
    /// while the hold is still idle and its pane is still the focused held key.
    /// A focus move, a replaced attachment or returning activity makes it moot,
    /// so it is dropped and the next report is the normal one.
    owed_unfocus: Option<(PaneId, String)>,
    last_activity: Instant,
}

impl FocusHold {
    pub(super) fn new(now: Instant) -> Self {
        Self {
            reported: None,
            in_flight: None,
            owed_unfocus: None,
            last_activity: now,
        }
    }

    pub(super) fn note_activity(&mut self, now: Instant) {
        self.last_activity = now;
    }

    /// The daemon refused the last report. A refused focus is forgotten, so the
    /// next tick sends it again; a refused unfocus is owed again.
    pub(super) fn unsent(&mut self) {
        match self.in_flight.take() {
            Some(FocusReport {
                pane,
                attachment_id,
                focused: false,
            }) => self.owed_unfocus = Some((pane, attachment_id)),
            _ => self.reported = None,
        }
    }

    /// `0` never expires; otherwise the hold lapses once `hold_seconds` pass
    /// with no activity.
    pub(super) fn is_idle(&self, now: Instant, hold_seconds: u64) -> bool {
        hold_seconds > 0
            && now.saturating_duration_since(self.last_activity)
                >= Duration::from_secs(hold_seconds)
    }

    /// The one report this tick owes. `key` is the focused pane when it is live
    /// and held. Moving focus needs no unfocused report: the release already
    /// cleared the daemon's flag, so the old key is forgotten silently.
    pub(super) fn step(
        &mut self,
        key: Option<(PaneId, String)>,
        idle: bool,
    ) -> Option<FocusReport> {
        let report = match self.owed_unfocus.take() {
            Some(owed) if idle && key.as_ref() == Some(&owed) => {
                let (pane, attachment_id) = owed;
                Some(FocusReport {
                    pane,
                    attachment_id,
                    focused: false,
                })
            }
            _ => self.next_report(key, idle),
        };
        self.in_flight = report.clone();
        report
    }

    fn next_report(&mut self, key: Option<(PaneId, String)>, idle: bool) -> Option<FocusReport> {
        let mut report = None;
        if let Some(reported) = self.reported.as_ref() {
            if idle || key.as_ref() != Some(reported) {
                if idle {
                    report = Some(FocusReport {
                        pane: reported.0,
                        attachment_id: reported.1.clone(),
                        focused: false,
                    });
                }
                self.reported = None;
            }
        }
        if let Some(key) = key.filter(|_| !idle) {
            if self.reported.as_ref() != Some(&key) {
                report = Some(FocusReport {
                    pane: key.0,
                    attachment_id: key.1.clone(),
                    focused: true,
                });
                self.reported = Some(key);
            }
        }
        report
    }

    /// The `terminal_focus` message this tick owes, if any. Runs on every render
    /// tick, so a granted take, a reconnect (the re-taken lease carries a new
    /// attachment) and a return after expiry each report within one tick.
    pub(super) fn sync(
        &mut self,
        workspace: &Workspace<LiveDaemon>,
        chrome: &Chrome,
        now: Instant,
        hold_seconds: u64,
    ) -> Option<Value> {
        if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
            return None;
        }
        let key = chrome.focused_pane().and_then(|pane| {
            let held = workspace.panes.get(&pane)?;
            (held.is_live() && held.is_held()).then(|| (pane, held.attachment_id().to_string()))
        });
        let idle = self.is_idle(now, hold_seconds);
        let report = self.step(key, idle)?;
        let pane = workspace.panes.get(&report.pane)?;
        // An unfocused report for a pane that has since closed would name a
        // flag the daemon already dropped.
        if !report.focused && !pane.is_live() {
            return None;
        }
        Some(json!({
            "type": "terminal_focus",
            "terminal_id": pane.terminal_id,
            "attachment_id": report.attachment_id,
            "focused": report.focused,
        }))
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::{FocusHold, FocusReport, PaneId};

    #[test]
    fn zero_hold_never_expires_and_focus_moves_forget_the_key() {
        let start = Instant::now();
        let hold = FocusHold::new(start);
        assert!(
            hold.is_idle(start + Duration::from_secs(2), 1),
            "a one-second hold expires after two quiet seconds"
        );
        assert!(
            !hold.is_idle(start + Duration::from_secs(86_400), 0),
            "a zero hold never expires"
        );

        let mut hold = FocusHold::new(start);
        let (first, second) = (PaneId(1), PaneId(2));
        assert_eq!(
            hold.step(Some((first, "attach-1".to_string())), false),
            Some(FocusReport {
                pane: first,
                attachment_id: "attach-1".to_string(),
                focused: true,
            }),
            "a granted take reports its pane focused"
        );
        assert_eq!(
            hold.step(Some((first, "attach-1".to_string())), false),
            None,
            "an unchanged focus sends nothing"
        );
        assert_eq!(
            hold.step(Some((second, "attach-2".to_string())), false),
            Some(FocusReport {
                pane: second,
                attachment_id: "attach-2".to_string(),
                focused: true,
            }),
            "a focus move reports only the new pane"
        );
        hold.unsent();
        assert_eq!(
            hold.step(Some((second, "attach-2".to_string())), false),
            Some(FocusReport {
                pane: second,
                attachment_id: "attach-2".to_string(),
                focused: true,
            }),
            "a report the daemon refused goes out again"
        );
        assert_eq!(
            hold.step(None, false),
            None,
            "leaving for an unheld pane sends no unfocused report"
        );
    }

    #[test]
    fn an_unfocus_the_daemon_refused_is_sent_again() {
        let mut hold = FocusHold::new(Instant::now());
        let pane = PaneId(1);
        hold.step(Some((pane, "attach-1".to_string())), false);
        assert_eq!(
            hold.step(Some((pane, "attach-1".to_string())), true),
            Some(FocusReport {
                pane,
                attachment_id: "attach-1".to_string(),
                focused: false,
            }),
            "an idle hold reports the held pane unfocused"
        );
        hold.unsent();
        assert_eq!(
            hold.step(Some((pane, "attach-1".to_string())), true),
            Some(FocusReport {
                pane,
                attachment_id: "attach-1".to_string(),
                focused: false,
            }),
            "an unfocus the daemon refused is owed again while its pane is still held"
        );
        assert_eq!(
            hold.step(Some((pane, "attach-1".to_string())), true),
            None,
            "an unfocus the daemon accepted is not repeated"
        );
    }

    #[test]
    fn a_refused_unfocus_for_a_replaced_attachment_is_dropped() {
        let mut hold = FocusHold::new(Instant::now());
        let pane = PaneId(1);
        hold.step(Some((pane, "attach-1".to_string())), false);
        hold.step(Some((pane, "attach-1".to_string())), true);
        hold.unsent();
        assert_eq!(
            hold.step(Some((pane, "attach-2".to_string())), false),
            Some(FocusReport {
                pane,
                attachment_id: "attach-2".to_string(),
                focused: true,
            }),
            "a refused unfocus for a replaced attachment does not block the new focus"
        );
    }

    #[test]
    fn a_refused_unfocus_is_not_resent_once_activity_returns() {
        let mut hold = FocusHold::new(Instant::now());
        let pane = PaneId(1);
        hold.step(Some((pane, "attach-1".to_string())), false);
        hold.step(Some((pane, "attach-1".to_string())), true);
        hold.unsent();
        assert_eq!(
            hold.step(Some((pane, "attach-1".to_string())), false),
            Some(FocusReport {
                pane,
                attachment_id: "attach-1".to_string(),
                focused: true,
            }),
            "an owed unfocus is dropped once activity returns, and the pane is reported focused"
        );
    }
}
