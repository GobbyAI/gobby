use super::*;

impl PaneTerminal {
    #[cfg(test)]
    pub(super) fn note_work_started(&self) {
        if let Some(started) = self.work_started.lock().unwrap().take() {
            let _ = started.send(());
        }
    }
    pub(crate) fn next_resize_revision(&self) -> u64 {
        self.resize_revision.fetch_add(1, Ordering::AcqRel) + 1
    }

    pub(crate) fn resize_revision_current(&self, revision: u64) -> bool {
        self.resize_revision.load(Ordering::Acquire) == revision
    }

    pub(crate) fn resize_for_revision(
        &self,
        revision: u64,
        rows: u16,
        cols: u16,
        enqueue: impl FnOnce(Vec<Bytes>),
    ) -> bool {
        #[cfg(test)]
        self.note_work_started();
        let _mutation = self
            .history_mutation
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if !self.resize_revision_current(revision) {
            return false;
        }
        let responses = self.ghostty.resize(rows, cols, 0, 0);
        self.mark_content_changed();
        enqueue(responses);
        true
    }

    pub(crate) fn apply_host_declaration(
        &self,
        seq: u64,
        theme: &crate::terminal_theme::ThemeDeclaration,
    ) -> Option<Bytes> {
        #[cfg(test)]
        self.note_work_started();
        let _mutation = self
            .history_mutation
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if seq < self.host_theme_seq.load(Ordering::Acquire) {
            return None;
        }
        self.host_theme_seq.store(seq, Ordering::Release);
        self.ghostty
            .apply_host_terminal_theme(theme.terminal_theme());
        let response = self
            .ghostty
            .apply_host_terminal_appearance(theme.appearance());
        self.mark_content_changed();
        response
    }
}
