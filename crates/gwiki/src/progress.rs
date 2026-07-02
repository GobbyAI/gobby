#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ProgressPhase {
    IngestFile,
    IngestUrl,
    VaultIndex,
    VectorSync,
    GraphSync,
}

pub(crate) trait ProgressSink {
    fn start(&mut self, phase: ProgressPhase, total: usize);
    fn advance(&mut self, phase: ProgressPhase, item: &str);
    fn finish(&mut self, phase: ProgressPhase);
}

#[derive(Default)]
pub(crate) struct ProgressOptions<'a> {
    pub(crate) sink: Option<&'a mut dyn ProgressSink>,
}

impl<'a> ProgressOptions<'a> {
    pub(crate) fn with_sink(sink: &'a mut dyn ProgressSink) -> Self {
        Self { sink: Some(sink) }
    }

    fn start(&mut self, phase: ProgressPhase, total: usize) {
        if let Some(sink) = self.sink.as_deref_mut() {
            sink.start(phase, total);
        }
    }

    fn advance(&mut self, phase: ProgressPhase, item: &str) {
        if let Some(sink) = self.sink.as_deref_mut() {
            sink.advance(phase, item);
        }
    }

    fn finish(&mut self, phase: ProgressPhase) {
        if let Some(sink) = self.sink.as_deref_mut() {
            sink.finish(phase);
        }
    }
}

pub(crate) struct ActiveProgress<'a, 'b> {
    options: &'a mut ProgressOptions<'b>,
    phase: ProgressPhase,
    started: bool,
}

impl<'a, 'b> ActiveProgress<'a, 'b> {
    pub(crate) fn new(
        options: &'a mut ProgressOptions<'b>,
        phase: ProgressPhase,
        total: usize,
    ) -> Self {
        let progress = Self {
            options,
            phase,
            started: total > 0,
        };
        if progress.started {
            progress.options.start(phase, total);
        }
        progress
    }

    pub(crate) fn advance(&mut self, item: &str) {
        self.options.advance(self.phase, item);
    }
}

impl Drop for ActiveProgress<'_, '_> {
    fn drop(&mut self) {
        if self.started {
            self.options.finish(self.phase);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct RecordingProgress {
        events: Vec<String>,
    }

    impl ProgressSink for RecordingProgress {
        fn start(&mut self, phase: ProgressPhase, total: usize) {
            self.events.push(format!("{phase:?}:start:{total}"));
        }

        fn advance(&mut self, phase: ProgressPhase, item: &str) {
            self.events.push(format!("{phase:?}:advance:{item}"));
        }

        fn finish(&mut self, phase: ProgressPhase) {
            self.events.push(format!("{phase:?}:finish"));
        }
    }

    #[test]
    fn active_progress_reports_start_advance_finish() {
        let mut sink = RecordingProgress::default();
        {
            let mut options = ProgressOptions::with_sink(&mut sink);
            let mut progress = ActiveProgress::new(&mut options, ProgressPhase::VaultIndex, 2);
            progress.advance("a.md");
            progress.advance("b.md");
        }

        assert_eq!(
            sink.events,
            vec![
                "VaultIndex:start:2",
                "VaultIndex:advance:a.md",
                "VaultIndex:advance:b.md",
                "VaultIndex:finish"
            ]
        );
    }
}
