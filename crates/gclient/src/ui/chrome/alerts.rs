// upstream: none (Gobby-only, D3 of plan issues-i-see-with-typed-bumblebee)
//! Transient alerts: the toast stack over the pane area and the alert log
//! behind [Menu]. Persistent conditions are not alerts; `daemon unreachable`
//! keeps its status-bar segment.

use std::time::Instant;

use crate::ui::status::{ActiveToast, Toast, TOAST_STACK, TOAST_TTL};

use super::Chrome;

/// Alerts the log keeps; the oldest leave first.
pub const ALERT_LOG_CAP: usize = 200;

impl Chrome {
    /// Show `toast` and append it to the alert log. The stack keeps the
    /// newest [`TOAST_STACK`] toasts.
    pub fn notify(&mut self, toast: Toast) {
        self.alert_log.push(toast.clone());
        if self.alert_log.len() > ALERT_LOG_CAP {
            self.alert_log.remove(0);
        }
        self.toasts.push(ActiveToast {
            toast,
            shown_at: Instant::now(),
        });
        if self.toasts.len() > TOAST_STACK {
            self.toasts.remove(0);
        }
    }

    /// Drop every toast older than [`TOAST_TTL`] at `now`.
    pub fn expire_toasts(&mut self, now: Instant) {
        self.toasts
            .retain(|active| now.duration_since(active.shown_at) < TOAST_TTL);
    }

    /// Any keypress clears the stack; the log keeps the alerts.
    pub fn dismiss_toasts(&mut self) {
        self.toasts.clear();
    }

    /// Title of the newest alert, whether or not its toast is still up.
    pub fn last_alert(&self) -> Option<&str> {
        self.alert_log.last().map(|toast| toast.title.as_str())
    }

    /// Roster row of the newest alert that named one, for
    /// `Action::OpenNotificationTarget`.
    ///
    /// The log, not the stack: every keypress clears the stack before the
    /// chord it carried resolves, so a stack read would leave the action with
    /// nothing to open every time a key invoked it. The stack is a prefix of
    /// the log, so while a targeted toast is up this is that toast's target;
    /// afterwards the action still reaches the alert the user just saw.
    pub fn latest_alert_target(&self) -> Option<&str> {
        self.alert_log
            .iter()
            .rev()
            .find_map(|toast| toast.target.as_deref())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn titles(chrome: &Chrome) -> Vec<&str> {
        chrome
            .toasts
            .iter()
            .map(|active| active.toast.title.as_str())
            .collect()
    }

    #[test]
    fn notify_stacks_to_three_and_logs_everything() {
        let mut chrome = Chrome::dark();
        for title in ["one", "two", "three", "four"] {
            chrome.notify(Toast::info(title));
        }
        assert_eq!(titles(&chrome), ["two", "three", "four"]);
        let logged: Vec<&str> = chrome
            .alert_log
            .iter()
            .map(|toast| toast.title.as_str())
            .collect();
        assert_eq!(logged, ["one", "two", "three", "four"]);
    }

    #[test]
    fn toasts_expire_after_the_ttl_and_dismiss_on_demand() {
        let mut chrome = Chrome::dark();
        chrome.notify(Toast::warning("stale"));
        let shown_at = chrome.toasts[0].shown_at;
        chrome.expire_toasts(shown_at + TOAST_TTL - Duration::from_millis(1));
        assert_eq!(titles(&chrome), ["stale"]);
        chrome.expire_toasts(shown_at + TOAST_TTL);
        assert!(chrome.toasts.is_empty());
        assert_eq!(chrome.alert_log.len(), 1, "the log outlives the toast");

        chrome.notify(Toast::error("gone on keypress"));
        chrome.dismiss_toasts();
        assert!(chrome.toasts.is_empty());
        assert_eq!(chrome.alert_log.len(), 2);
    }

    #[test]
    fn latest_alert_target_reads_the_newest_targeted_alert() {
        let mut chrome = Chrome::dark();
        assert_eq!(chrome.latest_alert_target(), None);
        let mut first = Toast::info("first");
        first.target = Some("term-a".to_string());
        chrome.notify(first);
        chrome.notify(Toast::info("untargeted"));
        assert_eq!(chrome.latest_alert_target(), Some("term-a"));
        chrome.dismiss_toasts();
        assert_eq!(chrome.latest_alert_target(), Some("term-a"));
    }
}
