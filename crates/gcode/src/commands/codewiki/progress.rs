#[derive(Debug)]
enum CodewikiProgressSink {
    Silent,
    Stderr,
    #[cfg(test)]
    Capture(Vec<String>),
}

#[derive(Debug)]
pub(crate) struct CodewikiProgress {
    sink: CodewikiProgressSink,
}

impl CodewikiProgress {
    pub(crate) fn silent() -> Self {
        Self {
            sink: CodewikiProgressSink::Silent,
        }
    }

    pub(crate) fn stderr(enabled: bool) -> Self {
        if enabled {
            Self {
                sink: CodewikiProgressSink::Stderr,
            }
        } else {
            Self::silent()
        }
    }

    #[cfg(test)]
    pub(crate) fn capture() -> Self {
        Self {
            sink: CodewikiProgressSink::Capture(Vec::new()),
        }
    }

    pub(crate) fn emit(&mut self, message: impl AsRef<str>) {
        let line = format!("codewiki: {}", message.as_ref());
        match &mut self.sink {
            CodewikiProgressSink::Silent => {}
            CodewikiProgressSink::Stderr => eprintln!("{line}"),
            #[cfg(test)]
            CodewikiProgressSink::Capture(lines) => lines.push(line),
        }
    }

    #[cfg(test)]
    pub(crate) fn into_lines(self) -> Vec<String> {
        match self.sink {
            CodewikiProgressSink::Silent | CodewikiProgressSink::Stderr => Vec::new(),
            CodewikiProgressSink::Capture(lines) => lines,
        }
    }
}
