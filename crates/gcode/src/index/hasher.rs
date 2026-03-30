//! Content hashing for incremental indexing.
//! Ports logic from src/gobby/code_index/hasher.py.

use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::Path;

/// SHA-256 hash of entire file contents.
pub fn file_content_hash(path: &Path) -> anyhow::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// SHA-256 hash of a byte slice (symbol source).
pub fn symbol_content_hash(source: &[u8], start: usize, end: usize) -> String {
    let mut hasher = Sha256::new();
    hasher.update(&source[start..end]);
    format!("{:x}", hasher.finalize())
}
