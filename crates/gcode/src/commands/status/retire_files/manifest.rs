use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context as _, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Manifest {
    pub backends: super::backend::BackendIdentity,
    pub version: u32,
    pub source_inventory_digest: String,
    pub project_id: String,
    pub machine_id: String,
    pub root_path: PathBuf,
    pub files: Vec<RetiredFile>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RetiredFile {
    pub file_path: String,
    pub versions: Vec<ContentVersion>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ContentVersion {
    pub id: String,
    pub content_hash: String,
    pub symbol_ids: Vec<String>,
}

pub(super) fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub(super) fn read(path: &Path) -> anyhow::Result<(Manifest, String)> {
    no_symlinks(path)?;
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    let file = options.open(path).context("open retirement manifest")?;
    require_private_file(&file)?;
    ensure!(
        file.metadata()?.len() <= 64 * 1024 * 1024,
        "manifest exceeds 64 MiB"
    );
    let mut bytes = Vec::new();
    file.take(64 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 64 * 1024 * 1024, "manifest exceeds 64 MiB");
    let manifest: Manifest = serde_json::from_slice(&bytes).context("parse retirement manifest")?;
    manifest.validate()?;
    Ok((manifest, digest(&bytes)))
}

impl Manifest {
    pub(super) fn validate(&self) -> anyhow::Result<()> {
        self.backends.validate()?;
        ensure!(self.version == 1, "unsupported retirement manifest version");
        ensure!(
            is_hash(&self.source_inventory_digest),
            "invalid source inventory digest"
        );
        canonical_uuid(&self.project_id)?;
        canonical_uuid(&self.machine_id)?;
        ensure!(
            self.root_path.is_absolute(),
            "manifest root must be absolute"
        );
        ensure!(
            !self.files.is_empty(),
            "manifest must name at least one file"
        );
        let mut paths = BTreeSet::new();
        let mut version_ids = BTreeSet::new();
        let mut symbol_ids = BTreeSet::new();
        for file in &self.files {
            let path = Path::new(&file.file_path);
            ensure!(
                !file.file_path.is_empty()
                    && path
                        .components()
                        .all(|part| matches!(part, Component::Normal(_)))
                    && path.components().collect::<PathBuf>().to_string_lossy() == file.file_path,
                "file path must be a normalized relative file: {}",
                file.file_path
            );
            ensure!(paths.insert(&file.file_path), "duplicate manifest path");
            let mut hashes = BTreeSet::new();
            for version in &file.versions {
                canonical_uuid(&version.id)?;
                ensure!(
                    version_ids.insert(&version.id),
                    "duplicate content identity"
                );
                ensure!(is_hash(&version.content_hash), "invalid content hash");
                ensure!(
                    hashes.insert(&version.content_hash),
                    "duplicate file content hash"
                );
                for id in &version.symbol_ids {
                    canonical_uuid(id)?;
                    ensure!(symbol_ids.insert(id), "duplicate symbol identity");
                }
            }
        }
        Ok(())
    }
}

fn canonical_uuid(value: &str) -> anyhow::Result<()> {
    ensure!(
        uuid::Uuid::parse_str(value)?.to_string() == value,
        "noncanonical UUID"
    );
    Ok(())
}

fn is_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn no_symlinks(path: &Path) -> anyhow::Result<()> {
    ensure!(
        path.is_absolute(),
        "path must be absolute: {}",
        path.display()
    );
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(metadata) => ensure!(
                !metadata.file_type().is_symlink(),
                "symlink path refused: {}",
                ancestor.display()
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

pub(super) fn require_absent(root: &Path, relative: &str) -> anyhow::Result<()> {
    let path = root.join(relative);
    no_symlinks(&path)?;
    match fs::symlink_metadata(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
        Ok(_) => anyhow::bail!("retired path is still present: {}", path.display()),
    }
}

pub(super) fn require_private_file(file: &File) -> anyhow::Result<()> {
    let metadata = file.metadata()?;
    ensure!(metadata.is_file(), "expected a regular private file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: getuid has no arguments and no memory-safety preconditions.
        let uid = unsafe { libc::getuid() };
        ensure!(
            metadata.uid() == uid && metadata.mode() & 0o077 == 0,
            "file must be owner-only and owned by this user"
        );
    }
    #[cfg(not(unix))]
    anyhow::bail!("retirement requires Unix ownership validation");
    Ok(())
}
