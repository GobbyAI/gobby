//! Git blob IDs recorded on code-index selectors.
//!
//! A selector records its file's Git blob ID only while the working bytes equal
//! the blob at HEAD for that path. Dirty, untracked, and filter-transformed paths
//! record none, because their bytes never equal the stored blob.

use std::collections::HashMap;
use std::io;
use std::path::Path;

use postgres::GenericClient;

use crate::git::git_output;
use crate::index::{api, hasher};

#[derive(Debug, Default)]
pub(super) struct HeadBlobs {
    blobs: HashMap<String, String>,
}

impl HeadBlobs {
    /// Reads the blob ID of every path tracked at HEAD under `root_path`.
    /// Without a readable HEAD no path is clean, so no blob is recorded.
    pub(super) fn read(root_path: &Path) -> Self {
        let blobs = git_output(root_path, &["ls-tree", "-r", "-z", "HEAD"])
            .map(|listing| parse_ls_tree(&listing))
            .unwrap_or_default();
        Self { blobs }
    }

    /// The blob ID of `bytes` when those bytes are the HEAD blob for `rel`.
    pub(super) fn clean_blob(&self, rel: &str, bytes: &[u8]) -> Option<String> {
        let head = self.blobs.get(rel)?;
        let blob = blob_id(bytes)?;
        (blob == *head).then_some(blob)
    }
}

/// A file's content hash and clean blob ID, both computed from one read of its bytes.
pub(super) struct FileSnapshot {
    pub(super) content_hash: String,
    pub(super) clean_blob: Option<String>,
}

pub(super) fn read_snapshot(path: &Path, rel: &str, head: &HeadBlobs) -> io::Result<FileSnapshot> {
    let bytes = std::fs::read(path)?;
    Ok(FileSnapshot {
        content_hash: hasher::content_hash(&bytes),
        clean_blob: head.clean_blob(rel, &bytes),
    })
}

/// Stores the selector's clean blob ID for `content_hash`. `None` clears a stale
/// ID, so a file that became dirty stops claiming its old blob.
pub(super) fn record_selector_blob(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
    rel: &str,
    content_hash: &str,
    clean_blob: Option<&str>,
) -> anyhow::Result<()> {
    api::file_state::set_selector_git_blob(
        conn,
        machine_id,
        project_id,
        rel,
        content_hash,
        clean_blob,
    )?;
    Ok(())
}

/// Git's blob ID for `bytes`. A SHA-1 collision-detection trip yields no ID, so
/// the path is recorded as not clean.
fn blob_id(bytes: &[u8]) -> Option<String> {
    gix::objs::compute_hash(gix::hash::Kind::Sha1, gix::objs::Kind::Blob, bytes)
        .ok()
        .map(|oid| oid.to_string())
}

/// Parses `git ls-tree -r -z` records, keeping only blob entries.
fn parse_ls_tree(listing: &str) -> HashMap<String, String> {
    listing
        .split('\0')
        .filter_map(|entry| {
            let (meta, path) = entry.split_once('\t')?;
            let mut fields = meta.split(' ');
            let _mode = fields.next()?;
            if fields.next()? != "blob" {
                return None;
            }
            Some((path.to_string(), fields.next()?.to_string()))
        })
        .collect()
}
