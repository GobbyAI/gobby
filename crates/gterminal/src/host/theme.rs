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
        let work = slot_theme_work(&inner, &host_terminal_id);
        drop(inner);
        apply_theme_work(work).await;
        Ok(())
    }

    pub(super) async fn inherited_theme(&self, host_id: &str) -> Option<ThemeDeclaration> {
        #[cfg(feature = "vt-engine")]
        {
            let terminal = {
                let inner = self.inner.lock().await;
                native_slot(&inner, host_id)?
                    .child
                    .as_ref()?
                    .runtime
                    .terminal_handle()
            };
            tokio::task::spawn_blocking(move || {
                let theme = terminal.host_terminal_theme();
                (!theme.is_empty()).then(|| ThemeDeclaration::from(&theme))
            })
            .await
            .ok()
            .flatten()
        }
        #[cfg(not(feature = "vt-engine"))]
        {
            let _ = host_id;
            None
        }
    }
}

/// Apply the newest declaration among the slot's streams, if any has declared one.
pub(super) fn slot_theme_work(inner: &Inner, host_terminal_id: &str) -> Option<ThemeWork> {
    let slot = native_slot(inner, host_terminal_id)?;
    let declared = inner
        .attachments
        .iter()
        .filter(|(_, att)| att.host_terminal_id == host_terminal_id)
        .filter_map(|(_, att)| att.declared_theme.as_ref())
        .max_by_key(|(seq, _)| *seq)
        .cloned();
    #[cfg(feature = "vt-engine")]
    {
        let (seq, theme) = declared?;
        let child = slot.child.as_ref()?;
        Some(ThemeWork {
            terminal: child.runtime.terminal_handle(),
            writer: child.runtime.input_writer(),
            seq,
            theme,
        })
    }
    #[cfg(not(feature = "vt-engine"))]
    {
        let _ = (slot, declared);
        None
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
pub(super) struct ThemeWork {
    #[cfg(feature = "vt-engine")]
    terminal: std::sync::Arc<crate::pane::PaneTerminal>,
    #[cfg(feature = "vt-engine")]
    writer: crate::pane::PaneInputWriter,
    #[cfg(feature = "vt-engine")]
    seq: u64,
    #[cfg(feature = "vt-engine")]
    theme: ThemeDeclaration,
}

pub(super) async fn apply_theme_work(work: Option<ThemeWork>) {
    #[cfg(feature = "vt-engine")]
    if let Some(work) = work {
        let _ = tokio::task::spawn_blocking(move || {
            work.writer.write_terminal_response(|| {
                work.terminal.apply_host_declaration(work.seq, &work.theme)
            });
        })
        .await;
    }
    #[cfg(not(feature = "vt-engine"))]
    let _ = work;
}
