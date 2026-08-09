use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::{CodewikiDocMeta, CodewikiMeta};

pub(super) const PROGRESS_JOURNAL_PATH: &str = "_meta/codewiki-progress.jsonl";

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum ProgressRecord {
    Upsert {
        path: String,
        meta: Box<CodewikiDocMeta>,
    },
    Remove {
        path: String,
    },
}

#[derive(Debug)]
pub(super) struct ProgressJournal {
    path: PathBuf,
    file: File,
}

impl ProgressJournal {
    pub(super) fn open(out_dir: &Path) -> anyhow::Result<Self> {
        let path = out_dir.join(PROGRESS_JOURNAL_PATH);
        let parent = path.parent().ok_or_else(|| {
            anyhow::anyhow!(
                "codewiki progress journal path has no parent: {}",
                path.display()
            )
        })?;
        std::fs::create_dir_all(parent)?;
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        Ok(Self { path, file })
    }

    pub(super) fn upsert(&mut self, path: &str, meta: &CodewikiDocMeta) -> anyhow::Result<()> {
        self.append(&ProgressRecord::Upsert {
            path: path.to_string(),
            meta: Box::new(meta.clone()),
        })
    }

    pub(super) fn remove(&mut self, path: &str) -> anyhow::Result<()> {
        self.append(&ProgressRecord::Remove {
            path: path.to_string(),
        })
    }

    fn append(&mut self, record: &ProgressRecord) -> anyhow::Result<()> {
        serde_json::to_writer(&mut self.file, record)?;
        self.file.write_all(b"\n")?;
        self.file.flush()?;
        self.file.sync_data()?;
        Ok(())
    }

    pub(super) fn compact(mut self) -> anyhow::Result<()> {
        self.file.flush()?;
        self.file.sync_data()?;
        drop(self.file);
        let parent = self.path.parent().ok_or_else(|| {
            anyhow::anyhow!(
                "codewiki progress journal path has no parent: {}",
                self.path.display()
            )
        })?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
        temporary.as_file_mut().sync_data()?;
        temporary.persist(&self.path).map_err(|error| error.error)?;
        Ok(())
    }
}

pub(super) fn replay(out_dir: &Path, meta: &mut CodewikiMeta) -> anyhow::Result<()> {
    let path = out_dir.join(PROGRESS_JOURNAL_PATH);
    let mut raw = Vec::new();
    match File::open(&path) {
        Ok(mut file) => file.read_to_end(&mut raw)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    if raw.is_empty() {
        return Ok(());
    }

    let mut generated = meta.generated_docs.iter().cloned().collect::<BTreeSet<_>>();
    let mut offset = 0;
    while offset < raw.len() {
        let remainder = &raw[offset..];
        let newline = remainder.iter().position(|byte| *byte == b'\n');
        let (line, complete) = match newline {
            Some(index) => (&remainder[..index], true),
            None => (remainder, false),
        };
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        match serde_json::from_slice::<ProgressRecord>(line) {
            Ok(record) => apply_record(record, meta, &mut generated),
            Err(_) if !complete => break,
            Err(error) => {
                return Err(anyhow::anyhow!(
                    "invalid complete CodeWiki progress journal record at byte {offset}: {error}"
                ));
            }
        }
        offset += line.len() + usize::from(complete);
        if !complete {
            break;
        }
    }
    meta.generated_docs = generated.into_iter().collect();
    Ok(())
}

fn apply_record(record: ProgressRecord, meta: &mut CodewikiMeta, generated: &mut BTreeSet<String>) {
    match record {
        ProgressRecord::Upsert { path, meta: doc } => {
            generated.insert(path.clone());
            meta.docs.insert(path, *doc);
        }
        ProgressRecord::Remove { path } => {
            generated.remove(&path);
            meta.docs.remove(&path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replay_ignores_only_a_truncated_final_record() -> anyhow::Result<()> {
        let dir = tempfile::tempdir()?;
        let mut journal = ProgressJournal::open(dir.path())?;
        journal.upsert("code/files/a.md", &CodewikiDocMeta::default())?;
        drop(journal);
        OpenOptions::new()
            .append(true)
            .open(dir.path().join(PROGRESS_JOURNAL_PATH))?
            .write_all(br#"{"op":"upsert","path":"code/files/b.md""#)?;

        let mut meta = CodewikiMeta::default();
        replay(dir.path(), &mut meta)?;

        assert!(meta.docs.contains_key("code/files/a.md"));
        assert!(!meta.docs.contains_key("code/files/b.md"));
        Ok(())
    }

    #[test]
    fn replay_rejects_a_malformed_complete_record() -> anyhow::Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join(PROGRESS_JOURNAL_PATH);
        std::fs::create_dir_all(path.parent().expect("journal parent"))?;
        std::fs::write(&path, b"broken\n")?;

        let error = replay(dir.path(), &mut CodewikiMeta::default())
            .expect_err("complete malformed records must fail");
        assert!(error.to_string().contains("invalid complete"));
        Ok(())
    }
}
