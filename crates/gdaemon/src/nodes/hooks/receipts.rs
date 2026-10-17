//! Forward node-local delivery receipts to the hub.
use super::ledger::Ledger;
use crate::state::NodeServices;
use anyhow::{Context, Result, bail};
use axum::{body::Body, http::Request};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::OnceLock,
    time::Duration,
};

const ROUTE: &str = "/api/nodes/receipts";
const CLAIM_SUFFIX: &str = ".claimed.tmp";

fn owner() -> &'static str {
    static OWNER: OnceLock<String> = OnceLock::new();
    OWNER.get_or_init(|| uuid::Uuid::new_v4().simple().to_string())
}

/// Sweep before replaying hooks, so acknowledgments retain their generation.
pub async fn sweep_receipts(node: &NodeServices, ledger: &Ledger) -> Result<()> {
    let inbox = ledger.inbox.clone();
    let paths = tokio::task::spawn_blocking(move || receipt_paths(&inbox)).await??;
    for path in paths {
        let Ok(Some(claim)) = tokio::task::spawn_blocking(move || Claim::take(path)).await? else {
            continue;
        };
        let envelope_id = claim
            .paths
            .as_ref()
            .and_then(|(original, _)| original.file_stem())
            .and_then(|stem| stem.to_str())
            .filter(|stem| !stem.is_empty());
        // A cleanup failure belongs to this claim and must not block its peers.
        let _ = match acknowledge(node, ledger, &claim.envelope, envelope_id).await {
            Ok(()) => claim.remove().await,
            Err(error) => {
                eprintln!("node receipt forwarding failed; retaining acknowledgment: {error:#}");
                claim.restore().await
            }
        };
    }
    Ok(())
}

#[derive(Deserialize)]
struct Acknowledgment {
    acknowledged: bool,
    carrying_envelope_ids: Vec<String>,
}

async fn acknowledge(
    node: &NodeServices,
    ledger: &Ledger,
    receipt: &Value,
    envelope_id: Option<&str>,
) -> Result<()> {
    let acknowledgment: Acknowledgment = serde_json::from_value(post(node, receipt, "ack").await?)?;
    // Stale generations are successful no-ops at the hub; their local ack still
    // terminalizes, while only an acknowledged delivery supplies carrying ids.
    if acknowledgment.acknowledged {
        for id in acknowledgment.carrying_envelope_ids {
            ledger.mark_processed(&id, None, None).await?;
        }
    }
    if let Some(id) = envelope_id {
        ledger.mark_processed(id, None, None).await?;
    }
    Ok(())
}

/// Release a delivery that cannot be carried by the current hook response.
pub async fn release_receipt(node: &NodeServices, receipt: &Value) -> Result<bool> {
    #[derive(Deserialize)]
    struct Release {
        released: bool,
    }
    Ok(serde_json::from_value::<Release>(post(node, receipt, "release").await?)?.released)
}

async fn post(node: &NodeServices, receipt: &Value, op: &str) -> Result<Value> {
    let receipt_id = receipt["receipt_id"]
        .as_str()
        .context("missing receipt_id")?;
    uuid::Uuid::parse_str(receipt_id).context("invalid receipt_id")?;
    let generation = receipt["delivery_generation"]
        .as_u64()
        .filter(|generation| *generation > 0)
        .context("invalid delivery_generation")?;
    let home = node
        .bootstrap_path
        .parent()
        .context("bootstrap has no parent")?
        .to_owned();
    let key = tokio::task::spawn_blocking(move || gobby_core::local_token::read_api_key_at(&home))
        .await??;
    let request = Request::builder()
        .method("POST")
        .uri(ROUTE)
        .header("authorization", format!("Bearer {key}"))
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&json!({
            "v": 1, "op": op, "receipt_id": receipt_id, "delivery_generation": generation
        }))?))?;
    tokio::time::timeout(Duration::from_secs(10), async {
        let response = node.relay.forward(request).await;
        if !response.status().is_success() {
            bail!("receipt hub returned {}", response.status());
        }
        let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024).await?;
        Ok(serde_json::from_slice(&bytes)?)
    })
    .await
    .context("receipt hub request timed out")?
}

fn receipt_paths(inbox: &Path) -> io::Result<Vec<PathBuf>> {
    let entries = match fs::read_dir(inbox) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    for entry in entries {
        let Ok(entry) = entry else {
            continue;
        };
        if !entry.file_type().is_ok_and(|kind| kind.is_file()) {
            continue;
        }
        let name = entry.file_name();
        let Some((original, claim_owner)) = name
            .to_str()
            .and_then(|name| name.strip_suffix(CLAIM_SUFFIX))
            .and_then(|name| name.rsplit_once('.'))
        else {
            continue;
        };
        if claim_owner != owner()
            && !claim_owner.is_empty()
            && claim_owner.bytes().all(|byte| byte.is_ascii_hexdigit())
            && original.ends_with(".json")
        {
            // Leave a failed orphan recovery for a later pass, without blocking its peers.
            let _ = rename_if_present(&entry.path(), &inbox.join(original));
        }
    }
    Ok(fs::read_dir(inbox)?
        .filter_map(Result::ok)
        .filter(|entry| {
            entry.file_type().is_ok_and(|kind| kind.is_file())
                && entry.path().extension().is_some_and(|ext| ext == "json")
        })
        .map(|entry| entry.path())
        .collect())
}

fn rename_if_present(from: &Path, to: &Path) -> io::Result<()> {
    match fs::rename(from, to) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}

struct Claim {
    paths: Option<(PathBuf, PathBuf)>,
    envelope: Value,
}

impl Claim {
    fn take(original: PathBuf) -> Result<Option<Self>> {
        let envelope: Value = match fs::read(&original) {
            Ok(bytes) => match serde_json::from_slice(&bytes) {
                Ok(envelope) => envelope,
                Err(_) => return Ok(None),
            },
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if envelope["kind"] != "delivery-receipt" {
            return Ok(None);
        }
        let claimed = original.with_file_name(format!(
            "{}.{}{CLAIM_SUFFIX}",
            original
                .file_name()
                .context("receipt has no filename")?
                .to_string_lossy(),
            owner()
        ));
        match fs::rename(&original, &claimed) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        }
        let mut claim = Self {
            paths: Some((original, claimed.clone())),
            envelope,
        };
        // The producer can replace an envelope at the same path. Read the bytes
        // we actually claimed rather than acknowledging a stale pre-rename read.
        claim.envelope = serde_json::from_slice(&fs::read(claimed)?)?;
        if claim.envelope["kind"] != "delivery-receipt" {
            return Ok(None);
        }
        Ok(Some(claim))
    }

    async fn restore(mut self) -> Result<()> {
        let (original, claimed) = self.paths.take().expect("live claim");
        tokio::task::spawn_blocking(move || rename_if_present(&claimed, &original)).await??;
        Ok(())
    }

    async fn remove(mut self) -> Result<()> {
        let (original, claimed) = self.paths.take().expect("live claim");
        tokio::task::spawn_blocking(move || {
            if let Err(error) = fs::remove_file(&claimed) {
                rename_if_present(&claimed, &original)?;
                return Err(error);
            }
            Ok(())
        })
        .await??;
        Ok(())
    }
}

impl Drop for Claim {
    fn drop(&mut self) {
        if let Some((original, claimed)) = self.paths.take() {
            // Cancellation must not strand a current-owner claim forever.
            tokio::task::spawn_blocking(move || {
                if let Err(error) = rename_if_present(&claimed, &original) {
                    eprintln!("failed to restore cancelled node receipt claim: {error:#}");
                }
            });
        }
    }
}
