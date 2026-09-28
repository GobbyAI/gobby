//! Declaring gclient's terminal colours to the gterm host.
//!
//! A native pane's child asks its terminal for colours with OSC 10/11 and
//! watches mode 2031 for dark/light changes. The host answers from the theme a
//! client declared on the pane's direct frame stream. The live loop calls
//! [`Workspace::sync_terminal_themes`] every tick, so a freshly attached pane
//! and a dark/light toggle reach the host the same way: any capable pane whose
//! last declaration differs from the chrome's theme gets it.
//!
//! The declaration also binds the stream to its daemon attachment. The host
//! applies a declaration only from the input-grant holder's bound stream (or
//! to an ungranted pane), and gclient otherwise binds on the first keystroke,
//! which would leave a controlled pane on the old colours until someone typed.

use gobby_terminal::protocol::ClientMessage;
use gobby_terminal::terminal_theme::ThemeDeclaration;

use super::attach::AttachState;
use super::pane::Pane;
use super::Workspace;
use crate::daemon::Daemon;
use crate::frame_source::{FrameError, FrameSource, Transport};

/// The attach-reply capability that says the pane's host accepts
/// `ClientMessage::SetTerminalTheme`.
pub(super) const TERMINAL_THEME_CAPABILITY: &str = "terminal_theme";

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
    /// Declare `theme` on this pane's direct stream when its host accepts it
    /// and it is not already the declared theme. A full write queue leaves the
    /// pane undeclared, so the next tick tries again.
    pub(super) fn sync_terminal_theme(&mut self, theme: &ThemeDeclaration) {
        if !self.host_themes || self.declared_theme.as_ref() == Some(theme) {
            return;
        }
        let AttachState::Attached { attachment_id, .. } = &self.attach else {
            return;
        };
        let attachment_id = attachment_id.clone();
        let Some(source) = self
            .frame_source
            .as_mut()
            .filter(|source| source.transport() == Transport::Direct)
        else {
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
    /// Bring every capable pane's host up to `theme`.
    pub(crate) fn sync_terminal_themes(&mut self, theme: &ThemeDeclaration) {
        for pane in self.panes.values_mut() {
            pane.sync_terminal_theme(theme);
        }
    }
}

#[cfg(test)]
#[path = "theme_sync/tests.rs"]
mod tests;
