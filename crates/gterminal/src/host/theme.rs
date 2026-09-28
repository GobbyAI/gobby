//! Client terminal themes on native slots.
//!
//! A frame stream declares its client's colours with
//! `ClientMessage::SetTerminalTheme`. The slot's input grant decides whose
//! declaration a pane shows: with no grant any declaration applies, and with a
//! grant only the holder's does. A stale or observing stream therefore cannot
//! undo the controlling client's newer theme. When the daemon moves the grant,
//! the new holder's declaration applies. Every declaration that applies, live
//! or on a grant move, also becomes the host's `latest_theme`, which a spawn
//! without its own theme starts with.

use super::state::{HostState, Inner, TerminalSlot};
use crate::terminal_theme::ThemeDeclaration;

impl HostState {
    /// Record `theme` for the stream's attachment and apply it when the stream
    /// controls its slot.
    pub async fn declare_terminal_theme(
        &self,
        attachment_id: Option<u64>,
        theme: ThemeDeclaration,
    ) -> Result<(), &'static str> {
        let attachment_id = attachment_id.ok_or("attach_required")?;
        let mut inner = self.inner.lock().await;
        let attachment = inner
            .attachments
            .get_mut(&attachment_id)
            .ok_or("terminal_gone")?;
        attachment.declared_theme = Some(theme.clone());
        let host_terminal_id = attachment.host_terminal_id.clone();
        let bound = attachment.client_attachment_id.clone();
        let Some(slot) = native_slot(&inner, &host_terminal_id) else {
            return Ok(());
        };
        if slot.input_grant.is_some() && slot.input_grant != bound {
            return Ok(());
        }
        apply(slot, &theme);
        // Only a declaration that applied seeds later spawns, so a stale or
        // observing stream cannot recolour them.
        inner.latest_theme = Some(theme);
        Ok(())
    }
}

/// Apply the input-grant holder's newest declaration to its slot, if the
/// holder's stream has declared one. Like a live declaration that applies, it
/// then seeds later spawns.
pub(crate) fn apply_holder_theme(inner: &mut Inner, host_terminal_id: &str) {
    let Some(slot) = native_slot(inner, host_terminal_id) else {
        return;
    };
    let Some(holder) = slot.input_grant.as_deref() else {
        return;
    };
    let declared = inner
        .attachments
        .iter()
        .filter(|(_, att)| {
            att.host_terminal_id == host_terminal_id
                && att.client_attachment_id.as_deref() == Some(holder)
        })
        .filter_map(|(id, att)| Some((*id, att.declared_theme.as_ref()?)))
        .max_by_key(|(id, _)| *id)
        .map(|(_, theme)| theme.clone());
    if let Some(theme) = declared {
        apply(slot, &theme);
        inner.latest_theme = Some(theme);
    }
}

fn native_slot<'a>(inner: &'a Inner, host_terminal_id: &str) -> Option<&'a TerminalSlot> {
    let identity = inner.by_host_id.get(host_terminal_id)?;
    inner
        .terminals
        .get(identity)
        .filter(|slot| slot.locator.is_none())
}

#[cfg(feature = "vt-engine")]
fn apply(slot: &TerminalSlot, theme: &ThemeDeclaration) {
    if let Some(child) = slot.child.as_ref() {
        child
            .runtime
            .apply_host_terminal_theme(theme.terminal_theme());
        child
            .runtime
            .apply_host_terminal_appearance(theme.appearance());
    }
}

#[cfg(not(feature = "vt-engine"))]
fn apply(_slot: &TerminalSlot, _theme: &ThemeDeclaration) {}
