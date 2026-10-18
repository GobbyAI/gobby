use super::*;
use std::{
    collections::HashMap,
    sync::{Mutex, OnceLock},
};

type Callback = Box<dyn FnOnce() + Send>;
type Boundaries = HashMap<(PathBuf, &'static str), Callback>;

fn boundaries() -> &'static Mutex<Boundaries> {
    static BOUNDARIES: OnceLock<Mutex<Boundaries>> = OnceLock::new();
    BOUNDARIES.get_or_init(Mutex::default)
}

pub(super) fn at_boundary(path: &Path, boundary: &'static str) {
    let callback = boundaries()
        .lock()
        .unwrap()
        .remove(&(path.to_owned(), boundary));
    if let Some(callback) = callback {
        callback();
    }
}

fn replace_at(path: &Path, boundary: &'static str, marker: Option<PathBuf>) {
    let original = path.to_owned();
    let callback = Box::new(move || {
        let replacement = original.with_extension("replacement.tmp");
        fs::write(
            &replacement,
            br#"{"kind":"delivery-receipt","receipt_id":"new"}"#,
        )
        .unwrap();
        fs::rename(replacement, &original).unwrap();
        if let Some(marker) = marker {
            fs::write(marker, replacement_marker()).unwrap();
        }
    });
    assert!(
        boundaries()
            .lock()
            .unwrap()
            .insert((path.to_owned(), boundary), callback)
            .is_none()
    );
}

fn replacement_marker() -> &'static [u8] {
    br#"{"status":"processing","owner_token":"replacement-owner"}"#
}

fn fixture() -> Result<(tempfile::TempDir, PathBuf, SpoolIdentity, Ledger)> {
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("hook.json");
    fs::write(&path, br#"{"hook_type":"PreToolUse"}"#)?;
    let (_, _, identity) = read_envelope(&path)?;
    let ledger = Ledger::new(directory.path().to_owned());
    Ok((directory, path, identity, ledger))
}

fn assert_replacement(path: &Path) {
    let bytes = fs::read(path).expect("producer replacement must remain at its original path");
    assert_eq!(bytes, br#"{"kind":"delivery-receipt","receipt_id":"new"}"#);
}

#[tokio::test]
async fn drain_boundary_unlink_preserves_producer_replacement() -> Result<()> {
    let (_directory, path, identity, _ledger) = fixture()?;
    replace_at(&path, "unlink", None);
    unlink(&path, &identity).await?;
    assert_replacement(&path);
    Ok(())
}

#[tokio::test]
async fn drain_boundary_quarantine_preserves_producer_replacement() -> Result<()> {
    let (_directory, path, identity, _ledger) = fixture()?;
    replace_at(&path, "quarantine", None);
    quarantine(&path, "superseded_hook", &identity).await?;
    assert_replacement(&path);
    Ok(())
}

async fn marker_boundary(disposition: Disposition, boundary: &'static str) -> Result<Vec<u8>> {
    let (_directory, path, identity, ledger) = fixture()?;
    assert!(ledger.claim("hook").await?.is_some());
    let marker = fs::read_dir(ledger.inbox.join("processed"))?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .expect("claim created one marker");
    replace_at(&path, boundary, Some(marker.clone()));
    apply(&ledger, "hook", &path, disposition, &identity).await?;
    assert_replacement(&path);
    Ok(fs::read(marker).expect("replacement marker must remain owned"))
}

#[tokio::test]
async fn drain_boundary_requeued_preserves_replacement_marker() -> Result<()> {
    assert_eq!(
        marker_boundary(Disposition::Requeued, "forget").await?,
        replacement_marker()
    );
    Ok(())
}

#[tokio::test]
async fn drain_boundary_release_preserves_replacement_marker() -> Result<()> {
    assert_eq!(
        marker_boundary(Disposition::BelowFloor, "release").await?,
        replacement_marker()
    );
    Ok(())
}

#[test]
fn drain_claim_identity_mismatch_restores_captured_replacement() -> Result<()> {
    let (_directory, path, identity, _ledger) = fixture()?;
    replace_at(&path, "replacement", None);
    at_boundary(&path, "replacement");
    assert!(DrainClaim::take(&path, &identity)?.is_none());
    assert_replacement(&path);
    Ok(())
}

#[test]
fn drain_claim_recovery_preserves_newer_original() -> Result<()> {
    let (directory, path, identity, _ledger) = fixture()?;
    let claim = DrainClaim::take(&path, &identity)?.expect("original claimed");
    let private = claim.path.clone();
    replace_at(&path, "replacement", None);
    at_boundary(&path, "replacement");
    drop(claim);
    assert_replacement(&path);
    assert!(
        private.exists(),
        "older claim must survive a producer collision"
    );
    let lock = prepare_drain(directory.path())?.expect("first pass owns lock");
    assert_replacement(&path);
    assert!(private.exists());
    assert!(
        prepare_drain(directory.path())?.is_none(),
        "overlapping pass must skip recovery"
    );
    drop(lock);
    fs::remove_file(&path)?;
    let _lock = prepare_drain(directory.path())?.expect("later pass owns lock");
    assert_eq!(fs::read(&path)?, br#"{"hook_type":"PreToolUse"}"#);
    assert!(!private.exists(), "restored claim must be consumed once");
    Ok(())
}

#[test]
fn drain_claim_recovery_finishes_interrupted_restore() -> Result<()> {
    let (directory, path, identity, _ledger) = fixture()?;
    let claim = DrainClaim::take(&path, &identity)?.expect("original claimed");
    let private = claim.path.clone();
    // A crash after publication but before removing the private link.
    fs::hard_link(&private, &path)?;
    std::mem::forget(claim);
    let _lock = prepare_drain(directory.path())?.expect("recovery owns lock");
    assert_eq!(fs::read(&path)?, br#"{"hook_type":"PreToolUse"}"#);
    assert!(
        !private.exists(),
        "published claim must not be replayed twice"
    );
    Ok(())
}

#[tokio::test]
async fn drain_claim_cancelled_pass_keeps_detached_worker_lock() -> Result<()> {
    let (directory, path, mut identity, _ledger) = fixture()?;
    identity._drain_lock = Some(Arc::new(
        prepare_drain(directory.path())?.expect("pass owns lock"),
    ));
    let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
    let (resume_tx, resume_rx) = std::sync::mpsc::channel();
    assert!(
        boundaries()
            .lock()
            .unwrap()
            .insert(
                (path.clone(), "unlink"),
                Box::new(move || {
                    entered_tx.send(()).unwrap();
                    resume_rx
                        .recv_timeout(Duration::from_secs(5))
                        .expect("test releases detached worker");
                })
            )
            .is_none()
    );
    let worker_path = path.clone();
    let pass = tokio::spawn(async move { unlink(&worker_path, &identity).await });
    tokio::time::timeout(Duration::from_secs(5), entered_rx).await??;
    pass.abort();
    assert!(pass.await.unwrap_err().is_cancelled());
    assert!(
        prepare_drain(directory.path())?.is_none(),
        "detached worker must still exclude a second pass"
    );
    resume_tx.send(())?;
    let lock_path = directory.path().join(".drain.lock");
    // Blocking acquisition synchronizes with completion, without polling or sleeps.
    let finished = tokio::task::spawn_blocking(move || -> io::Result<File> {
        let lock = OpenOptions::new().read(true).write(true).open(lock_path)?;
        loop {
            // SAFETY: the descriptor is owned by lock for the whole flock call.
            if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } == 0 {
                return Ok(lock);
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    });
    let finished_lock = tokio::time::timeout(Duration::from_secs(5), finished).await???;
    drop(finished_lock);
    assert!(
        prepare_drain(directory.path())?.is_some(),
        "next pass can claim after worker completion"
    );
    assert!(!path.exists(), "detached worker completed the unlink");
    Ok(())
}
