//! Source walking and the identifier scan shared by the census sections.

use std::path::Path;

use anyhow::{Context, anyhow};
use walkdir::{DirEntry, WalkDir};

/// The directory whose top-level packages the census describes.
pub const GOBBY_PREFIX: &str = "src/gobby/";

/// A UTF-8 text file under a census root. `rel` is its path from the root, with `/` separators.
pub struct TextFile {
    pub rel: String,
    pub text: String,
}

impl TextFile {
    /// The path below `src/gobby/`, or the relative path itself for a file outside it.
    pub fn inner(&self) -> &str {
        self.rel.strip_prefix(GOBBY_PREFIX).unwrap_or(&self.rel)
    }

    /// The top-level package: the first directory below `src/gobby/`. `None` for a file directly
    /// in that directory and for a file outside it.
    pub fn package(&self) -> Option<&str> {
        self.rel
            .strip_prefix(GOBBY_PREFIX)?
            .split_once('/')
            .map(|(dir, _)| dir)
    }

    /// Whether the file is a Python module under `src/gobby/`.
    pub fn is_gobby_python(&self) -> bool {
        self.rel.starts_with(GOBBY_PREFIX) && self.rel.ends_with(".py")
    }
}

/// Every UTF-8 text file under `root/sub`, sorted by path. Binary files and `__pycache__`
/// directories are skipped. A missing `sub` yields no files.
pub fn text_files(root: &Path, sub: &str) -> anyhow::Result<Vec<TextFile>> {
    let base = root.join(sub);
    if !base.exists() {
        return Ok(Vec::new());
    }
    let mut files = Vec::new();
    let walk = WalkDir::new(&base)
        .sort_by_file_name()
        .into_iter()
        .filter_entry(|entry| !is_pycache(entry));
    for entry in walk {
        let entry = entry.with_context(|| format!("cannot walk {}", base.display()))?;
        if !entry.file_type().is_file() {
            continue;
        }
        let bytes = std::fs::read(entry.path())
            .with_context(|| format!("cannot read {}", entry.path().display()))?;
        let Ok(text) = String::from_utf8(bytes) else {
            continue;
        };
        files.push(TextFile {
            rel: relative(root, entry.path())?,
            text,
        });
    }
    files.sort_by(|a, b| a.rel.cmp(&b.rel));
    Ok(files)
}

fn is_pycache(entry: &DirEntry) -> bool {
    entry.depth() > 0 && entry.file_type().is_dir() && entry.file_name() == "__pycache__"
}

fn relative(root: &Path, path: &Path) -> anyhow::Result<String> {
    let inner = path
        .strip_prefix(root)
        .with_context(|| format!("{} is outside {}", path.display(), root.display()))?;
    let mut parts = Vec::new();
    for part in inner.components() {
        parts.push(
            part.as_os_str()
                .to_str()
                .ok_or_else(|| anyhow!("non-UTF-8 path {}", path.display()))?,
        );
    }
    Ok(parts.join("/"))
}

/// The top-level package directories under `src/gobby/`, sorted and without repeats.
pub fn packages(files: &[TextFile]) -> Vec<String> {
    let mut names: Vec<String> = files
        .iter()
        .filter_map(TextFile::package)
        .map(str::to_owned)
        .collect();
    names.sort();
    names.dedup();
    names
}

/// An identifier in source text and, when a `.` sits directly before it, the identifier before
/// that dot (`functools` in `functools.cache`).
pub struct Token<'a> {
    pub ident: &'a str,
    pub qualifier: Option<&'a str>,
}

/// Every identifier in `text`, in order. A run of word characters that starts with a digit is not
/// an identifier.
pub fn tokens(text: &str) -> Vec<Token<'_>> {
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    let mut previous: Option<(&str, usize)> = None;
    let mut at = 0;
    while at < bytes.len() {
        if !is_word(bytes[at]) {
            at += 1;
            continue;
        }
        let start = at;
        while at < bytes.len() && is_word(bytes[at]) {
            at += 1;
        }
        if bytes[start].is_ascii_digit() {
            previous = None;
            continue;
        }
        let ident = &text[start..at];
        let qualifier = match previous {
            Some((name, end)) if start > 0 && bytes[start - 1] == b'.' && end == start - 1 => {
                Some(name)
            }
            _ => None,
        };
        found.push(Token { ident, qualifier });
        previous = Some((ident, at));
    }
    found
}

fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}
