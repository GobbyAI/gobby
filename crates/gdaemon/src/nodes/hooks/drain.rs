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
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime},
};

const REPLAY: &str = "/api/nodes/hooks/replay";
const MAX_ENVELOPE_BYTES: u64 = 16 * 1024 * 1024;

/// Receipts settle before any hub replay decision, including for live envelopes.
pub async fn drain_once(node: &NodeServices, ledger: &Ledger) -> Result<()> {
    receipts::sweep_receipts(node, ledger).await?;
    let inbox = ledger.inbox.clone();
    let paths = tokio::task::spawn_blocking(move || hook_paths(&inbox)).await??;
    for path in paths {
        if let Err(error) = drain_file(node, ledger, &path).await {
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

fn read_envelope(path: &Path) -> Result<(Value, f64)> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > MAX_ENVELOPE_BYTES {
        bail!("invalid spool file size/type");
    }
    let mut bytes = Vec::new();
    file.take(MAX_ENVELOPE_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_ENVELOPE_BYTES {
        bail!("spool envelope exceeds size limit");
    }
    let envelope = serde_json::from_slice(&bytes)?;
    let age = SystemTime::now()
        .duration_since(metadata.modified()?)
        .unwrap_or_default()
        .as_secs_f64();
    Ok((envelope, age))
}

async fn drain_file(node: &NodeServices, ledger: &Ledger, path: &Path) -> Result<()> {
    let owned_path = path.to_owned();
    let (envelope, age) = tokio::task::spawn_blocking(move || read_envelope(&owned_path)).await??;
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
    let Some(disposition) = replay(node, id, &envelope, age, processed, live).await? else {
        release_claim(ledger, id).await?;
        return quarantine(path, "replay_auth_rejected").await;
    };
    if disposition != Disposition::Execute {
        return apply(ledger, id, path, disposition).await;
    }
    ledger.clear_stale(id, None).await?;
    let request = request(node, "/api/hooks/execute", &envelope, Some(id)).await?;
    let outcome = forward_claimed(node, ledger, id, request).await;
    if outcome.response.status() == StatusCode::UNAUTHORIZED {
        release_claim(ledger, id).await?;
        return quarantine(path, "replay_auth_rejected").await;
    }
    if outcome.response.status().is_success() {
        // forward_claimed finalizes the owned claim before returning. It has no
        // HTTP delivery body, so dropping this outcome never releases a receipt.
        ledger
            .mark_processed(id, None, envelope["hook_type"].as_str())
            .await?;
        match replay(node, id, &envelope, age, true, false).await? {
            Some(disposition) => apply(ledger, id, path, disposition).await?,
            None => {
                release_claim(ledger, id).await?;
                quarantine(path, "replay_auth_rejected").await?;
            }
        }
    }
    Ok(())
}

async fn release_claim(ledger: &Ledger, id: &str) -> Result<()> {
    if let Some(marker) = ledger.marker(id).await?
        && let Some(token) = marker["owner_token"].as_str()
    {
        ledger.release(id, token).await?;
    }
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

async fn apply(ledger: &Ledger, id: &str, path: &Path, disposition: Disposition) -> Result<()> {
    match disposition {
        Disposition::Retain | Disposition::Execute => Ok(()),
        Disposition::Requeued => {
            unlink(path).await?;
            ledger.forget(id).await?;
            Ok(())
        }
        Disposition::Unlink => unlink(path).await,
        Disposition::Archived => quarantine(path, "superseded_hook").await,
        Disposition::BelowFloor => {
            release_claim(ledger, id).await?;
            quarantine(path, "below_floor_response_capability").await
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

async fn unlink(path: &Path) -> Result<()> {
    let path = path.to_owned();
    tokio::task::spawn_blocking(move || remove_if_present(&path)).await??;
    Ok(())
}

async fn quarantine(path: &Path, reason: &'static str) -> Result<()> {
    let path = path.to_owned();
    tokio::task::spawn_blocking(move || {
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
        fs::rename(&path, directory.join(format!("{stem}.json")))?;
        File::open(&directory)?.sync_all()?;
        File::open(path.parent().context("spool has no parent")?)?.sync_all()?;
        Ok::<_, anyhow::Error>(())
    })
    .await??;
    Ok(())
}
