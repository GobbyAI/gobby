//! Replay node-local spool entries through the hub's authoritative decisions.
use super::{
    ingress::forward_claimed,
    ledger::{
        ENVELOPE_REPLAY_GRACE_SECONDS, Ledger, PROCESSED_MARKER_PRUNE_MAX_ENTRIES,
        PROCESSED_MARKER_RETENTION_SECONDS,
    },
    receipts,
};
use crate::{heartbeat::HeartbeatHost, state::NodeServices};
use anyhow::{Context, Result, bail};
use axum::{
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::{
        fd::AsRawFd,
        unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    },
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime},
};

const REPLAY: &str = "/api/nodes/hooks/replay";
const MAX_ENVELOPE_BYTES: u64 = 16 * 1024 * 1024;

/// Receipts settle before any hub replay decision, including for live envelopes.
pub async fn drain_once(node: &NodeServices, ledger: &Ledger) -> Result<()> {
    let inbox = ledger.inbox.clone();
    let Some(lock) = tokio::task::spawn_blocking(move || prepare_drain(&inbox)).await?? else {
        return Ok(());
    };
    let lock = Arc::new(lock);
    receipts::sweep_receipts(node, ledger).await?;
    let inbox = ledger.inbox.clone();
    let paths = tokio::task::spawn_blocking(move || hook_paths(&inbox)).await??;
    for path in paths {
        if let Err(error) = drain_file(node, ledger, &path, lock.clone()).await {
            eprintln!("node hook drain retaining {}: {error:#}", path.display());
        }
    }
    Ok(())
}

pub async fn boot_release(ledger: &Ledger) -> Result<()> {
    ledger.release_previous_process().await?;
    Ok(())
}

pub async fn prune(ledger: &Ledger) -> Result<()> {
    ledger
        .prune(
            None,
            PROCESSED_MARKER_RETENTION_SECONDS,
            PROCESSED_MARKER_PRUNE_MAX_ENTRIES,
        )
        .await?;
    let inbox = ledger.inbox.clone();
    tokio::task::spawn_blocking(move || {
        let now = SystemTime::now();
        for entry in directory(&inbox)?.take(PROCESSED_MARKER_PRUNE_MAX_ENTRIES) {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.ends_with(".tmp")
                && !name.ends_with(".claimed.tmp")
                && entry.file_type()?.is_file()
            {
                let stale = now
                    .duration_since(entry.metadata()?.modified()?)
                    .unwrap_or_default()
                    > Duration::from_secs(3600);
                if stale {
                    remove_if_present(&entry.path())?;
                }
            }
        }
        Ok::<_, io::Error>(())
    })
    .await??;
    Ok(())
}

pub fn register_jobs(host: &mut HeartbeatHost, node: Arc<NodeServices>) -> Result<()> {
    let drain_node = node.clone();
    host.register("node-hook-drain", Duration::from_secs(60), move || {
        let node = drain_node.clone();
        async move { drain_once(&node, &node.hook_ledger).await }
    })?;
    host.register("node-hook-prune", Duration::from_secs(3600), move || {
        let ledger = node.hook_ledger.clone();
        async move { prune(&ledger).await }
    })?;
    Ok(())
}

fn directory(path: &Path) -> io::Result<impl Iterator<Item = io::Result<fs::DirEntry>>> {
    match fs::read_dir(path) {
        Ok(entries) => Ok(Some(entries).into_iter().flatten()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None.into_iter().flatten()),
        Err(error) => Err(error),
    }
}

fn hook_paths(inbox: &Path) -> io::Result<Vec<PathBuf>> {
    let mut paths = Vec::new();
    for entry in directory(inbox)? {
        let entry = entry?;
        if entry.file_type()?.is_file() && entry.path().extension().is_some_and(|ext| ext == "json")
        {
            paths.push((entry.metadata()?.modified()?, entry.path()));
        }
    }
    paths.sort();
    Ok(paths.into_iter().map(|(_, path)| path).collect())
}

#[derive(Clone)]
struct SpoolIdentity {
    // Keep the inode alive so an atomic replacement cannot reuse its identity.
    file: Arc<File>,
    // A cancelled async pass must not let recovery steal a blocking worker's claim.
    _drain_lock: Option<Arc<File>>,
}

impl SpoolIdentity {
    fn matches(&self, path: &Path) -> io::Result<bool> {
        let original = self.file.metadata()?;
        match fs::symlink_metadata(path) {
            Ok(current) => Ok(current.is_file()
                && current.dev() == original.dev()
                && current.ino() == original.ino()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error),
        }
    }

    async fn is_current(&self, path: &Path) -> Result<bool> {
        let identity = self.clone();
        let path = path.to_owned();
        Ok(tokio::task::spawn_blocking(move || identity.matches(&path)).await??)
    }
}

fn read_envelope(path: &Path) -> Result<(Value, f64, SpoolIdentity)> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > MAX_ENVELOPE_BYTES {
        bail!("invalid spool file size/type");
    }
    let mut bytes = Vec::new();
    (&file)
        .take(MAX_ENVELOPE_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_ENVELOPE_BYTES {
        bail!("spool envelope exceeds size limit");
    }
    let envelope = serde_json::from_slice(&bytes)?;
    let age = SystemTime::now()
        .duration_since(metadata.modified()?)
        .unwrap_or_default()
        .as_secs_f64();
    Ok((
        envelope,
        age,
        SpoolIdentity {
            file: Arc::new(file),
            _drain_lock: None,
        },
    ))
}

async fn drain_file(
    node: &NodeServices,
    ledger: &Ledger,
    path: &Path,
    lock: Arc<File>,
) -> Result<()> {
    let owned_path = path.to_owned();
    let (envelope, age, mut identity) =
        tokio::task::spawn_blocking(move || read_envelope(&owned_path)).await??;
    identity._drain_lock = Some(lock);
    if envelope["kind"] == "delivery-receipt" {
        return Ok(());
    }
    let id = path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .context("invalid envelope filename")?;
    let live_path = path.to_owned();
    let fresh = tokio::task::spawn_blocking(move || {
        Ledger::is_fresh(&live_path, None, ENVELOPE_REPLAY_GRACE_SECONDS)
    })
    .await?;
    let live = fresh || ledger.is_active(id, None).await?;
    let processed = ledger.is_processed(id).await?;
    let disposition = replay(node, id, &envelope, age, processed, live).await?;
    if !identity.is_current(path).await? {
        return Ok(());
    }
    let Some(disposition) = disposition else {
        release_claim(ledger, id, path, &identity).await?;
        return quarantine(path, "replay_auth_rejected", &identity).await;
    };
    if disposition != Disposition::Execute {
        return apply(ledger, id, path, disposition, &identity).await;
    }
    ledger.clear_stale(id, None).await?;
    let request = request(node, "/api/hooks/execute", &envelope, Some(id)).await?;
    if !identity.is_current(path).await? {
        return Ok(());
    }
    let outcome = forward_claimed(node, ledger, id, request).await;
    if !identity.is_current(path).await? {
        return Ok(());
    }
    if outcome.response.status() == StatusCode::UNAUTHORIZED {
        release_claim(ledger, id, path, &identity).await?;
        return quarantine(path, "replay_auth_rejected", &identity).await;
    }
    if outcome.response.status().is_success() {
        // forward_claimed finalizes the owned claim before returning. It has no
        // HTTP delivery body, so dropping this outcome never releases a receipt.
        ledger
            .mark_processed(id, None, envelope["hook_type"].as_str())
            .await?;
        let disposition = replay(node, id, &envelope, age, true, false).await?;
        if !identity.is_current(path).await? {
            return Ok(());
        }
        match disposition {
            Some(disposition) => apply(ledger, id, path, disposition, &identity).await?,
            None => {
                release_claim(ledger, id, path, &identity).await?;
                quarantine(path, "replay_auth_rejected", &identity).await?;
            }
        }
    }
    Ok(())
}

async fn release_claim(
    ledger: &Ledger,
    id: &str,
    path: &Path,
    identity: &SpoolIdentity,
) -> Result<()> {
    let expected = ledger.marker(id).await?;
    #[cfg(test)]
    tests::at_boundary(&ledger.inbox.join(format!("{id}.json")), "release");
    let (path, identity) = (path.to_owned(), identity.clone());
    ledger
        .remove_spool_marker(id, expected, true, move || identity.matches(&path))
        .await?;
    Ok(())
}

#[derive(Debug, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Disposition {
    Retain,
    Requeued,
    Unlink,
    Archived,
    BelowFloor,
    Execute,
}

async fn apply(
    ledger: &Ledger,
    id: &str,
    path: &Path,
    disposition: Disposition,
    identity: &SpoolIdentity,
) -> Result<()> {
    if !identity.is_current(path).await? {
        return Ok(());
    }
    match disposition {
        Disposition::Retain | Disposition::Execute => Ok(()),
        Disposition::Requeued => {
            let expected = ledger.marker(id).await?;
            if unlink(path, identity).await? {
                #[cfg(test)]
                tests::at_boundary(path, "forget");
                let path = path.to_owned();
                ledger
                    .remove_spool_marker(id, expected, false, move || {
                        match fs::symlink_metadata(&path) {
                            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(true),
                            Ok(_) => Ok(false),
                            Err(error) => Err(error),
                        }
                    })
                    .await?;
            }
            Ok(())
        }
        Disposition::Unlink => unlink(path, identity).await.map(|_| ()),
        Disposition::Archived => quarantine(path, "superseded_hook", identity).await,
        Disposition::BelowFloor => {
            release_claim(ledger, id, path, identity).await?;
            quarantine(path, "below_floor_response_capability", identity).await
        }
    }
}

async fn request(
    node: &NodeServices,
    route: &str,
    payload: &Value,
    id: Option<&str>,
) -> Result<Request<Body>> {
    let home = node
        .bootstrap_path
        .parent()
        .context("bootstrap has no parent")?
        .to_owned();
    let key = tokio::task::spawn_blocking(move || gobby_core::local_token::read_api_key_at(&home))
        .await??;
    let mut request = Request::builder()
        .method("POST")
        .uri(route)
        .header("authorization", format!("Bearer {key}"))
        .header("content-type", "application/json");
    if let Some(id) = id {
        request = request.header("x-gobby-envelope-id", id);
    }
    Ok(request.body(Body::from(serde_json::to_vec(payload)?))?)
}

async fn replay(
    node: &NodeServices,
    id: &str,
    envelope: &Value,
    age: f64,
    processed: bool,
    live: bool,
) -> Result<Option<Disposition>> {
    let request = request(
        node,
        REPLAY,
        &json!({"v":1, "envelope_id":id, "envelope":envelope,
        "age_seconds":age, "processed":processed, "live":live}),
        None,
    )
    .await?;
    tokio::time::timeout(Duration::from_secs(10), async {
        let response = node.relay.forward(request).await;
        if response.status() == StatusCode::UNAUTHORIZED {
            return Ok(None);
        }
        if !response.status().is_success() {
            bail!("replay hub returned {}", response.status());
        }
        #[derive(Deserialize)]
        struct Decision {
            disposition: Disposition,
        }
        let bytes = to_bytes(response.into_body(), 1024 * 1024).await?;
        Ok(Some(
            serde_json::from_slice::<Decision>(&bytes)?.disposition,
        ))
    })
    .await?
}

fn remove_if_present(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

async fn unlink(path: &Path, identity: &SpoolIdentity) -> Result<bool> {
    let path = path.to_owned();
    let identity = identity.clone();
    Ok(tokio::task::spawn_blocking(move || {
        let Some(claim) = DrainClaim::take(&path, &identity)? else {
            return Ok(false);
        };
        #[cfg(test)]
        tests::at_boundary(&path, "unlink");
        remove_if_present(&claim.path)?;
        File::open(
            claim
                .path
                .parent()
                .ok_or_else(|| io::Error::other("claim has no parent"))?,
        )?
        .sync_all()?;
        Ok::<_, io::Error>(true)
    })
    .await??)
}

async fn quarantine(path: &Path, reason: &'static str, identity: &SpoolIdentity) -> Result<()> {
    let path = path.to_owned();
    let identity = identity.clone();
    tokio::task::spawn_blocking(move || {
        let Some(claim) = DrainClaim::take(&path, &identity)? else {
            return Ok(());
        };
        let directory = path
            .parent()
            .context("spool has no parent")?
            .join("quarantine");
        fs::create_dir_all(&directory)?;
        let stem = format!(
            "{}-{}",
            path.file_stem().unwrap_or_default().to_string_lossy(),
            uuid::Uuid::new_v4()
        );
        let meta = directory.join(format!("{stem}.meta.json"));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&meta)?;
        file.write_all(&serde_json::to_vec(&json!({"reason":reason}))?)?;
        file.sync_all()?;
        #[cfg(test)]
        tests::at_boundary(&path, "quarantine");
        fs::rename(&claim.path, directory.join(format!("{stem}.json")))?;
        File::open(&directory)?.sync_all()?;
        File::open(claim.path.parent().context("claim has no parent")?)?.sync_all()?;
        Ok::<_, anyhow::Error>(())
    })
    .await??;
    Ok(())
}

const DRAIN_CLAIMS: &str = ".drain-claims";

/// A private pathname that producers never replace. Drop retains unsent data.
struct DrainClaim {
    original: PathBuf,
    path: PathBuf,
}

impl DrainClaim {
    fn take(original: &Path, identity: &SpoolIdentity) -> io::Result<Option<Self>> {
        let directory = original
            .parent()
            .ok_or_else(|| io::Error::other("spool has no parent"))?
            .join(DRAIN_CLAIMS);
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&directory)?;
        let name = original
            .file_name()
            .ok_or_else(|| io::Error::other("spool has no filename"))?;
        let path = directory.join(format!(
            "{}.{}",
            uuid::Uuid::new_v4(),
            name.to_string_lossy()
        ));
        match fs::rename(original, &path) {
            Ok(()) => (),
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        }
        let claim = Self {
            original: original.to_owned(),
            path,
        };
        File::open(&directory)?.sync_all()?;
        File::open(
            original
                .parent()
                .ok_or_else(|| io::Error::other("spool has no parent"))?,
        )?
        .sync_all()?;
        // Open the renamed file, then compare both descriptors with fstat.
        let claimed = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&claim.path)?;
        let (captured, expected) = (claimed.metadata()?, identity.file.metadata()?);
        if !captured.is_file()
            || captured.dev() != expected.dev()
            || captured.ino() != expected.ino()
        {
            claim.restore()?;
            return Ok(None);
        }
        Ok(Some(claim))
    }

    fn restore(&self) -> io::Result<()> {
        // link is an atomic no-replace publication. A newer producer wins.
        match fs::hard_link(&self.path, &self.original) {
            Ok(()) => (),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let (original, private) = (
                    fs::symlink_metadata(&self.original)?,
                    fs::symlink_metadata(&self.path)?,
                );
                if original.dev() != private.dev() || original.ino() != private.ino() {
                    return Ok(());
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        }
        File::open(
            self.original
                .parent()
                .ok_or_else(|| io::Error::other("spool has no parent"))?,
        )?
        .sync_all()?;
        fs::remove_file(&self.path)?;
        File::open(
            self.path
                .parent()
                .ok_or_else(|| io::Error::other("claim has no parent"))?,
        )?
        .sync_all()
    }
}

impl Drop for DrainClaim {
    fn drop(&mut self) {
        if let Err(error) = self.restore() {
            eprintln!(
                "node hook drain retaining claim {}: {error}",
                self.path.display()
            );
        }
    }
}

fn prepare_drain(inbox: &Path) -> io::Result<Option<File>> {
    fs::create_dir_all(inbox)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(inbox.join(".drain.lock"))?;
    // SAFETY: the owned descriptor remains open for the entire drain pass.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        let error = io::Error::last_os_error();
        return if error.kind() == io::ErrorKind::WouldBlock {
            Ok(None)
        } else {
            Err(error)
        };
    }
    for entry in directory(&inbox.join(DRAIN_CLAIMS))? {
        let entry = entry?;
        if !entry.file_type()?.is_file() {
            continue;
        }
        let name = entry.file_name();
        let Some((owner, original)) = name.to_str().and_then(|name| name.split_once('.')) else {
            continue;
        };
        if uuid::Uuid::parse_str(owner).is_err() || !original.ends_with(".json") {
            continue;
        }
        let claim = DrainClaim {
            original: inbox.join(original),
            path: entry.path(),
        };
        claim.restore()?;
    }
    Ok(Some(lock))
}

#[cfg(test)]
#[path = "drain/tests.rs"]
mod tests;
