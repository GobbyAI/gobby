//! Declaring gclient's terminal colours to the gterm host.
//!
//! A native pane's child asks its terminal for colours with OSC 10/11 and
//! watches mode 2031 for dark/light changes. The host answers from the theme a
//! client declared on the pane's direct frame stream, or that the daemon
//! relayed for a proxied pane (`terminal_set_theme`). The live loop calls
//! [`Workspace::sync_terminal_themes`] every tick, so a freshly attached pane
//! and a dark/light toggle reach the host the same way: any capable pane whose
//! last declaration differs from the chrome's theme gets it.
//!
//! The declaration also binds the stream to its daemon attachment. The host
//! applies a declaration only from the input-grant holder's bound stream (or
//! to an ungranted pane), and gclient otherwise binds on the first keystroke,
//! which would leave a controlled pane on the old colours until someone typed.

use gobby_terminal::protocol::ClientMessage;
use gobby_terminal::raw_input::HostColorQueryArm;
use gobby_terminal::terminal_theme::{ThemeDeclaration, HOST_COLOR_QUERY_SEQUENCE};
use std::io::Write;

use super::attach::AttachState;
use super::pane::Pane;
use super::Workspace;
use crate::daemon::Daemon;
use crate::frame_source::{FrameError, FrameSource, Transport};
use serde_json::json;

/// The attach-reply capability that says the pane's host accepts
/// `ClientMessage::SetTerminalTheme`.
pub(super) const TERMINAL_THEME_CAPABILITY: &str = "terminal_theme";

/// Ask the hosting terminal for its default colours (OSC 10/11). The answer
/// arrives as `RawInputEvent::HostDefaultColor`, which the live loop records
/// on the chrome for System mode's declaration. `arm` readies the input
/// reader first, so a reply split at its ESC never leaks as an Escape key.
pub(crate) fn query_host_colors(
    arm: &HostColorQueryArm,
    output: &mut impl Write,
) -> std::io::Result<()> {
    arm.query_sent();
    output.write_all(HOST_COLOR_QUERY_SEQUENCE.as_bytes())?;
    output.flush()
}

/// Whether an attach reply's `host_capabilities` include terminal themes.
pub(super) fn host_accepts_themes(reply: &serde_json::Value) -> bool {
    reply
        .get("host_capabilities")
        .and_then(serde_json::Value::as_array)
        .is_some_and(|features| {
            features
                .iter()
                .any(|feature| feature.as_str() == Some(TERMINAL_THEME_CAPABILITY))
        })
}

impl Pane {
    /// The transport that owes this pane's host `theme`: `None` unless the
    /// pane is attached, its route accepts themes and `theme` is new to it.
    fn theme_due(&self, theme: &ThemeDeclaration) -> Option<Transport> {
        if !self.host_themes
            || self.declared_theme.as_ref() == Some(theme)
            || !matches!(self.attach, AttachState::Attached { .. })
        {
            return None;
        }
        self.frame_source.as_ref().map(|source| source.transport())
    }

    /// Declare `theme` on this pane's direct stream when its host accepts it
    /// and it is not already the declared theme. A full write queue leaves the
    /// pane undeclared, so the next tick tries again.
    pub(super) fn sync_terminal_theme(&mut self, theme: &ThemeDeclaration) {
        if self.theme_due(theme) != Some(Transport::Direct) {
            return;
        }
        let AttachState::Attached { attachment_id, .. } = &self.attach else {
            return;
        };
        let attachment_id = attachment_id.clone();
        let Some(source) = self.frame_source.as_mut() else {
            return;
        };
        let sent = (|| -> Result<(), FrameError> {
            if self.host_bound_attachment.as_deref() != Some(attachment_id.as_str()) {
                source.send_input(&ClientMessage::BindAttachment {
                    attachment_id: attachment_id.clone(),
                })?;
                self.host_bound_attachment = Some(attachment_id);
            }
            source.send_input(&ClientMessage::SetTerminalTheme {
                theme: theme.clone(),
            })
        })();
        // Any other failure retires the stream; its reader reports that.
        if sent.is_ok() {
            self.declared_theme = Some(theme.clone());
        }
    }
}

impl<D: Daemon> Workspace<D> {
    /// Bring every capable pane's host up to `theme`. A proxied pane has no
    /// stream of its own, so the daemon relays its declaration on the host
    /// stream it holds for the pane's attachment; the host's input grant
    /// still decides whether it applies.
    pub(crate) async fn sync_terminal_themes(&mut self, theme: &ThemeDeclaration) {
        let mut relayed = Vec::new();
        for (id, pane) in self.panes.iter_mut() {
            match pane.theme_due(theme) {
                Some(Transport::Direct) => pane.sync_terminal_theme(theme),
                Some(Transport::Proxy) => relayed.push((
                    *id,
                    json!({
                        "type": "terminal_set_theme",
                        "terminal_id": pane.terminal_id,
                        "attachment_id": pane.attachment_id(),
                        "theme": theme,
                    }),
                )),
                None => {}
            }
        }
        for (id, message) in relayed {
            // A failed relay leaves the pane undeclared for the next tick.
            if self.daemon.notify(message).await.is_ok() {
                if let Some(pane) = self.panes.get_mut(&id) {
                    pane.declared_theme = Some(theme.clone());
                }
            }
        }
    }
}

#[cfg(test)]
#[path = "theme_sync/tests.rs"]
mod tests;
