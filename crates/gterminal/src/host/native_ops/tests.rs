use super::{kill_group, snapshot_mode, truncate_snapshot, KillGroupError};
use crate::host::config::HostConfig;
use crate::host::state::{insert_native_slot, HostState};
use crate::protocol::SnapshotMode;
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;

fn object(value: Value) -> Map<String, Value> {
    value.as_object().cloned().expect("json object")
}

async fn slot_killing(state: &HostState, host_terminal_id: &str) -> Option<bool> {
    let inner = state.inner.lock().await;
    let identity = inner.by_host_id.get(host_terminal_id)?;
    inner.terminals.get(identity).map(|slot| slot.killing)
}

async fn listed(state: &HostState, host_terminal_id: &str) -> bool {
    let listing = state.list_json().await;
    listing["terminals"]
        .as_array()
        .expect("terminal rows")
        .iter()
        .any(|row| row["host_terminal_id"] == host_terminal_id)
}

#[tokio::test]
async fn unproven_kill_stays_listed_and_refuses_overlap() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("skipped: as root, signalling process group 1 would reach init");
        return;
    }
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "local".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    insert_native_slot(&state, "ht-unprovable", 24, 80).await;
    {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["ht-unprovable"].clone();
        // A group this user may not signal answers every probe with EPERM,
        // so its death can never be proven.
        inner.terminals.get_mut(&identity).expect("slot").pgid = 1;
    }
    let target = object(json!({"host_terminal_id": "ht-unprovable"}));
    let first = tokio::spawn({
        let state = Arc::clone(&state);
        let target = target.clone();
        async move { state.kill(&target, Some(0)).await }
    });
    tokio::time::timeout(Duration::from_secs(1), async {
        while slot_killing(&state, "ht-unprovable").await != Some(true) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("kill marks the slot in flight");

    assert!(
        listed(&state, "ht-unprovable").await,
        "in-flight kill read as absence"
    );
    let overlapping = state.kill(&target, Some(0)).await;
    assert_eq!(overlapping["error"], "kill_in_progress", "{overlapping}");
    let commit = state
        .spawn_commit(&object(json!({
            "terminal_id": "term-ht-unprovable",
            "spawn_key": "spawn-ht-unprovable",
        })))
        .await;
    assert_eq!(commit["error"], "not_found", "{commit}");

    let outcome = first.await.expect("kill task");
    assert_eq!(outcome["error"], "kill_unproven", "{outcome}");
    assert!(
        listed(&state, "ht-unprovable").await,
        "unproven kill dropped the slot"
    );
    assert_eq!(slot_killing(&state, "ht-unprovable").await, Some(false));
    let inner = state.inner.lock().await;
    let identity = &inner.by_host_id["ht-unprovable"];
    assert!(
        inner.terminals[identity].kill_unproven,
        "unproven kill left no mark for a later leader exit"
    );
}

#[tokio::test]
async fn leader_exit_after_unproven_kill_waits_for_its_group() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("skipped: as root, process group 1 probes as signallable");
        return;
    }
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "local".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    insert_native_slot(&state, "ht-lingering", 24, 80).await;
    let identity = {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["ht-lingering"].clone();
        let slot = inner.terminals.get_mut(&identity).expect("slot");
        // A kill that could not prove the group gone, whose group still
        // answers probes (EPERM reads as alive).
        slot.kill_unproven = true;
        slot.pgid = 1;
        identity
    };

    assert!(
        !state
            .settle_leader_exit(&identity, "ht-lingering", Some(0))
            .await,
        "leader exit settled a live group after an unproven kill"
    );
    assert!(
        listed(&state, "ht-lingering").await,
        "slot of a live group dropped"
    );

    let mut reaped = std::process::Command::new("true")
        .spawn()
        .expect("spawn true");
    let dead_pgid = i32::try_from(reaped.id()).expect("pid fits pgid");
    reaped.wait().expect("reap true");
    {
        let mut inner = state.inner.lock().await;
        inner.terminals.get_mut(&identity).expect("slot").pgid = dead_pgid;
    }
    assert!(
        state
            .settle_leader_exit(&identity, "ht-lingering", Some(0))
            .await,
        "a dead group's exit was withheld"
    );
    assert!(!listed(&state, "ht-lingering").await, "dead slot kept");
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn shutdown_drain_reaches_a_group_retained_after_an_unproven_kill() {
    use std::os::unix::process::CommandExt;

    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "local".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    let mut survivor = std::process::Command::new("sleep")
        .arg("30")
        .process_group(0)
        .spawn()
        .expect("spawn sleep");
    let survivor_pgid = i32::try_from(survivor.id()).expect("pid fits pgid");
    insert_native_slot(&state, "ht-retained", 24, 80).await;
    {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["ht-retained"].clone();
        let slot = inner.terminals.get_mut(&identity).expect("slot");
        // The leader is gone (no live child), but the unproven kill left
        // this group alive, so the slot is retained for it.
        slot.kill_unproven = true;
        slot.pgid = survivor_pgid;
    }

    state
        .drain_native_children(Duration::from_millis(200))
        .await;

    let status = survivor.try_wait().expect("probe survivor");
    if status.is_none() {
        survivor.kill().expect("clean up survivor");
        survivor.wait().expect("reap survivor");
    }
    assert!(
        status.is_some(),
        "shutdown drain skipped a retained live group"
    );
}

#[test]
fn kill_group_refuses_non_positive_pgid() {
    assert!(matches!(
        kill_group(0, libc::SIGTERM),
        Err(KillGroupError::InvalidPgid)
    ));
    assert!(matches!(
        kill_group(-1, libc::SIGKILL),
        Err(KillGroupError::InvalidPgid)
    ));
}

#[test]
fn snapshot_mode_defaults_to_text_and_refuses_everything_else() {
    assert_eq!(snapshot_mode(&Map::new()).unwrap(), SnapshotMode::Text);
    for (wire, expected) in [("text", SnapshotMode::Text), ("ansi", SnapshotMode::Ansi)] {
        let mut extra = Map::new();
        extra.insert("mode".into(), Value::String(wire.into()));
        assert_eq!(snapshot_mode(&extra).unwrap(), expected);
    }
    for value in [
        json!("ANSI"),
        json!("plain"),
        json!(""),
        Value::Null,
        json!(1),
        json!(["ansi"]),
        json!({"mode": "ansi"}),
    ] {
        let mut extra = Map::new();
        extra.insert("mode".into(), value.clone());
        let refusal = snapshot_mode(&extra).expect_err("unsupported mode must be refused");
        assert_eq!(refusal["ok"], false, "{value}");
        assert_eq!(refusal["error"], "invalid_mode", "{value}");
        assert_eq!(refusal["valid_modes"], json!(["text", "ansi"]), "{value}");
    }
}

#[test]
fn truncate_snapshot_keeps_the_line_tail_and_counts_what_it_dropped() {
    let full = "one\ntwo\nthree\n";
    let whole = truncate_snapshot(full, 10, 1024);
    assert_eq!(whole.text, "one\ntwo\nthree");
    assert!(!whole.truncated);
    assert_eq!(whole.dropped_bytes, 0);
    assert_eq!(whole.total_bytes, full.len() as u64);

    let tail = truncate_snapshot(full, 2, 1024);
    assert_eq!(tail.text, "two\nthree");
    assert!(tail.truncated);
    assert_eq!(tail.dropped_bytes, "one\n".len() as u64);
    assert_eq!(tail.total_bytes, full.len() as u64);
}

#[test]
fn truncate_snapshot_trims_multibyte_text_on_char_boundaries() {
    let text = "\u{e9}\u{e9}\u{e9}";
    let exact = truncate_snapshot(text, 10, text.len());
    assert_eq!(exact.text, text);
    assert!(!exact.truncated);

    let split = truncate_snapshot(text, 10, 3);
    assert_eq!(split.text, "\u{e9}");
    assert!(split.text.len() <= 3);
    assert!(split.truncated);
    assert_eq!(split.dropped_bytes, 4);
    assert_eq!(split.total_bytes, text.len() as u64);

    let both = truncate_snapshot("aaa\n\u{e9}\u{e9}\u{e9}\n", 1, 3);
    assert_eq!(both.text, "\u{e9}");
    assert!(both.truncated);
    assert_eq!(both.dropped_bytes, 8);
    assert_eq!(both.total_bytes, 11);
}
