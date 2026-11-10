//! Rotation-safe tail of `daemon.log`. Each poll returns only complete new lines.

use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;

pub struct LogTail {
    path: PathBuf,
    open: Option<OpenLog>,
    /// True until the log is first seen: lines already present then are history, not new signals.
    skip_existing: bool,
}

struct OpenLog {
    file: File,
    ident: (u64, u64),
    offset: u64,
    partial: Vec<u8>,
}

impl LogTail {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            open: None,
            skip_existing: true,
        }
    }

    /// Complete lines appended since the last poll. Rotation by rename or truncation
    /// drains the old file, then reads the replacement from its start.
    pub fn poll(&mut self) -> io::Result<Vec<String>> {
        let mut lines = Vec::new();
        let meta = match fs::metadata(&self.path) {
            Ok(meta) => Some(meta),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        // A log that did not exist yet has no history: everything it receives is new.
        if meta.is_none() {
            self.skip_existing = false;
        }

        // Drain whatever the old handle still holds, then drop it when the path now names
        // another file or the file shrank.
        if let Some(open) = self.open.as_mut() {
            open.read_new_lines(&mut lines)?;
            let replaced = match &meta {
                Some(meta) => ident_of(meta) != open.ident || meta.len() < open.offset,
                None => true,
            };
            if replaced {
                self.open = None;
            }
        }

        if self.open.is_none() && meta.is_some() {
            let file = File::open(&self.path)?;
            let file_meta = file.metadata()?;
            let offset = if self.skip_existing {
                file_meta.len()
            } else {
                0
            };
            self.skip_existing = false;
            self.open = Some(OpenLog {
                ident: ident_of(&file_meta),
                file,
                offset,
                partial: Vec::new(),
            });
        }
        if let Some(open) = self.open.as_mut() {
            open.read_new_lines(&mut lines)?;
        }
        Ok(lines)
    }
}

impl OpenLog {
    /// Append complete lines past `offset`, keeping any trailing partial line for later.
    fn read_new_lines(&mut self, lines: &mut Vec<String>) -> io::Result<()> {
        self.file.seek(SeekFrom::Start(self.offset))?;
        let mut chunk = Vec::new();
        self.file.read_to_end(&mut chunk)?;
        self.offset += chunk.len() as u64;
        self.partial.extend_from_slice(&chunk);

        let mut start = 0;
        while let Some(newline) = self.partial[start..].iter().position(|byte| *byte == b'\n') {
            let end = start + newline;
            let line = String::from_utf8_lossy(&self.partial[start..end]);
            lines.push(line.trim_end_matches('\r').to_string());
            start = end + 1;
        }
        self.partial.drain(..start);
        Ok(())
    }
}

fn ident_of(meta: &fs::Metadata) -> (u64, u64) {
    (meta.dev(), meta.ino())
}

#[cfg(test)]
#[path = "logtail/tests.rs"]
mod tests;
