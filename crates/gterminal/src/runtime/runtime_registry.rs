use std::collections::HashMap;

use super::{TerminalId, TerminalRuntime};

/// Server-owned live terminal runtimes, keyed by durable terminal id.
///
/// This sits outside `AppState` so pure state can stay focused on workspace,
/// pane, and terminal metadata while the server/application layer owns PTYs,
/// parser backends, detector tasks, and channels.
#[derive(Default)]
pub(crate) struct TerminalRuntimeRegistry {
    runtimes: HashMap<TerminalId, TerminalRuntime>,
}

impl TerminalRuntimeRegistry {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn get(&self, terminal_id: &TerminalId) -> Option<&TerminalRuntime> {
        self.runtimes.get(terminal_id)
    }

    pub(crate) fn len(&self) -> usize {
        self.runtimes.len()
    }

    pub(crate) fn drain(&mut self) -> impl Iterator<Item = (TerminalId, TerminalRuntime)> + '_ {
        self.runtimes.drain()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime::TerminalId;

    #[test]
    fn registry_inserts_and_reports_len() {
        let mut registry = TerminalRuntimeRegistry::new();
        assert_eq!(registry.len(), 0);
        assert!(registry.get(&TerminalId::alloc()).is_none());
        let _ = registry.drain();
    }
}
