//! Client terminal themes on native slots.
//!
//! A frame stream declares its client's colours with
//! `ClientMessage::SetTerminalTheme`. Each declaration applies to its slot as it
//! arrives, so the newest declaration wins. Binding a stream re-applies the
//! newest declaration among the slot's streams. A declaration only ever reaches
//! its own attachment's pane: a spawn starts with the theme its request carries,
//! or with the current ground of the pane its `theme_from` names (a daemon
//! spawn's spawner), never another client's.

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
        let Ok(_gate) = self.mutation_gate.try_read() else {
            return Err("host_upgrading");
        };
        let attachment_id = attachment_id.ok_or("attach_required")?;
        let mut inner = self.inner.lock().await;
        inner.next_theme_seq += 1;
        let seq = inner.next_theme_seq;
        let attachment = inner
            .attachments
            .get_mut(&attachment_id)
            .ok_or("terminal_gone")?;
        attachment.declared_theme = Some((seq, theme.clone()));
        let host_terminal_id = attachment.host_terminal_id.clone();
        let Some(slot) = native_slot(&inner, &host_terminal_id) else {
            return Ok(());
        };
        apply(slot, &theme);
        Ok(())
    }
}

/// Apply the newest declaration among the slot's streams, if any has declared one.
pub(crate) fn apply_slot_theme(inner: &Inner, host_terminal_id: &str) {
    let Some(slot) = native_slot(inner, host_terminal_id) else {
        return;
    };
    let declared = inner
        .attachments
        .iter()
        .filter(|(_, att)| att.host_terminal_id == host_terminal_id)
        .filter_map(|(_, att)| att.declared_theme.as_ref())
        .max_by_key(|(seq, _)| *seq)
        .map(|(_, theme)| theme);
    if let Some(theme) = declared {
        apply(slot, theme);
    }
}

fn native_slot<'a>(inner: &'a Inner, host_terminal_id: &str) -> Option<&'a TerminalSlot> {
    let identity = inner.by_host_id.get(host_terminal_id)?;
    inner
        .terminals
        .get(identity)
        .filter(|slot| slot.locator.is_none())
}

/// The ground a live native pane answers OSC 10/11 with, as a declaration a
/// spawn can start from. `None` when the pane is gone or its theme is unset.
#[cfg(feature = "vt-engine")]
pub(crate) fn slot_theme(inner: &Inner, host_terminal_id: &str) -> Option<ThemeDeclaration> {
    let child = native_slot(inner, host_terminal_id)?.child.as_ref()?;
    let theme = child.runtime.host_terminal_theme();
    (!theme.is_empty()).then(|| ThemeDeclaration::from(&theme))
}

#[cfg(not(feature = "vt-engine"))]
pub(crate) fn slot_theme(_inner: &Inner, _host_terminal_id: &str) -> Option<ThemeDeclaration> {
    None
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
