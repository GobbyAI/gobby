use super::{kill_group, KillGroupError};
use crate::host::config::HostConfig;
use crate::host::snapshot::{snapshot_mode, truncate_snapshot};
use crate::host::state::{insert_native_slot, HostState};
use crate::protocol::SnapshotMode;
use serde_json::{json, Map, Value};
use std::os::unix::process::CommandExt;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;

fn object(value: Value) -> Map<String, Value> {
    value.as_object().cloned().expect("json object")
}

#[test]
fn zero_line_tail_counts_do_not_exceed_full_bytes() {
    let snapshot = truncate_snapshot("a\nb", 0, 100);
    assert_eq!(snapshot.text, "");
    assert_eq!(snapshot.total_bytes, Some(3));
    assert_eq!(snapshot.dropped_bytes, Some(3));
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

struct LiveGroup(std::process::Child);

impl Drop for LiveGroup {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn live_group() -> LiveGroup {
    let mut command = std::process::Command::new("sleep");
    command.arg("30").process_group(0);
    LiveGroup(command.spawn().expect("spawn owned process group"))
}

#[tokio::test]
async fn unproven_kill_stays_listed_and_refuses_overlap() {
    let mut child = live_group();
    let pgid = i32::try_from(child.0.id()).expect("pid fits pgid");
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    insert_native_slot(&state, "ht-unprovable", 24, 80).await;
    {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["ht-unprovable"].clone();
        inner.terminals.get_mut(&identity).expect("slot").pgid = pgid;
    }
    let target = object(json!({"host_terminal_id": "ht-unprovable"}));
    let (started, ready) = tokio::sync::oneshot::channel();
    let (release, held) = tokio::sync::oneshot::channel();
    let first = tokio::spawn({
        let state = Arc::clone(&state);
        let target = target.clone();
        async move {
            state
                .kill_with(&target, Some(0), move |target_pgid, _, _| async move {
                    assert_eq!(target_pgid, pgid);
                    started.send(()).expect("announce proof");
                    held.await.expect("release proof");
                    false
                })
                .await
        }
    });
    tokio::time::timeout(Duration::from_secs(1), ready)
        .await
        .expect("proof starts")
        .expect("proof announced");

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

    release.send(()).expect("release unproven result");
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
    child.0.kill().expect("kill owned group");
    child.0.wait().expect("reap owned group");
}

#[tokio::test]
async fn leader_exit_after_unproven_kill_waits_for_its_group() {
    let mut child = live_group();
    let pgid = i32::try_from(child.0.id()).expect("pid fits pgid");
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
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
        // A kill that could not prove the owned group gone.
        slot.kill_unproven = true;
        slot.pgid = pgid;
        identity
    };

    assert!(
        !state
            .settle_leader_exit(&identity, "ht-lingering", Some(0), false)
            .await,
        "leader exit settled a live group after an unproven kill"
    );
    assert!(
        listed(&state, "ht-lingering").await,
        "slot of a live group dropped"
    );

    child.0.kill().expect("kill owned group");
    child.0.wait().expect("reap owned group");
    assert!(
        state
            .settle_leader_exit(&identity, "ht-lingering", Some(0), false)
            .await,
        "a dead group's exit was withheld"
    );
    assert!(!listed(&state, "ht-lingering").await, "dead slot kept");
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn kill_during_the_exit_drain_emits_no_terminal_exited() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("skipped: as root, process group 1 probes as signallable");
        return;
    }
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    insert_native_slot(&state, "ht-drain", 24, 80).await;
    // A group that has already exited, so the kill proves it gone at once.
    let mut reaped = std::process::Command::new("true")
        .spawn()
        .expect("spawn true");
    let dead_pgid = i32::try_from(reaped.id()).expect("pid fits pgid");
    reaped.wait().expect("reap true");
    // The exit drain waits on this runtime. Its drain stays open until the
    // runtime drops, so only removing the slot ends the drain.
    let (runtime, _received) = crate::pane::PaneRuntime::test_with_channel(80, 24);
    let identity = {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["ht-drain"].clone();
        let slot = inner.terminals.get_mut(&identity).expect("slot");
        slot.pgid = dead_pgid;
        slot.child = Some(crate::host::spawn::PreparedChild::from_test_runtime(
            runtime,
        ));
        identity
    };
    let target = object(json!({"host_terminal_id": "ht-drain"}));
    // The leader's exit settles first and parks in its drain; the kill then
    // proves the group gone and removes the slot while that drain is parked.
    let (settled, killed) = tokio::join!(
        state.settle_leader_exit(&identity, "ht-drain", Some(0), false),
        state.kill(&target, Some(0)),
    );
    assert_eq!(killed["killed"], true, "{killed}");
    assert!(
        !settled,
        "exit drain emitted terminal_exited after a kill settled the slot"
    );
    assert!(
        !listed(&state, "ht-drain").await,
        "killed slot still listed"
    );
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn kill_that_removes_the_slot_first_leaves_the_exit_watcher_silent() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("skipped: as root, process group 1 probes as signallable");
        return;
    }
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
        "epoch".to_string(),
        crate::host::image::PinnedImage::for_tests(),
        1,
        shutdown,
    );
    insert_native_slot(&state, "ht-won", 24, 80).await;
    let mut reaped = std::process::Command::new("true")
        .spawn()
        .expect("spawn true");
    let dead_pgid = i32::try_from(reaped.id()).expect("pid fits pgid");
    reaped.wait().expect("reap true");
    let identity = {
        let mut inner = state.inner.lock().await;
        let identity = inner.by_host_id["ht-won"].clone();
        inner.terminals.get_mut(&identity).expect("slot").pgid = dead_pgid;
        identity
    };
    // The kill proves the group gone and removes the slot before the exit
    // watcher settles. Nothing removed the slot for an exit at commit, so the
    // watcher must emit nothing.
    let target = object(json!({"host_terminal_id": "ht-won"}));
    let killed = state.kill(&target, Some(0)).await;
    assert_eq!(killed["killed"], true, "{killed}");
    let settled = state
        .settle_leader_exit(&identity, "ht-won", Some(0), false)
        .await;
    assert!(
        !settled,
        "exit watcher emitted terminal_exited after a kill had removed the slot"
    );
}

#[cfg(feature = "vt-engine")]
#[tokio::test]
async fn shutdown_drain_reaches_a_group_retained_after_an_unproven_kill() {
    let (shutdown, _) = watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".to_string(),
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
fn kill_group_refuses_broadcast_and_non_positive_pgid() {
    // Signal zero safely exercises the broadcast selector before the guard exists.
    assert!(matches!(kill_group(1, 0), Err(KillGroupError::InvalidPgid)));
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
    assert_eq!(whole.dropped_bytes, Some(0));
    assert_eq!(whole.total_bytes, Some(full.len() as u64));

    let tail = truncate_snapshot(full, 2, 1024);
    assert_eq!(tail.text, "two\nthree");
    assert!(tail.truncated);
    assert_eq!(tail.dropped_bytes, Some("one\n".len() as u64));
    assert_eq!(tail.total_bytes, Some(full.len() as u64));
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
    assert_eq!(split.dropped_bytes, Some(4));
    assert_eq!(split.total_bytes, Some(text.len() as u64));

    let both = truncate_snapshot("aaa\n\u{e9}\u{e9}\u{e9}\n", 1, 3);
    assert_eq!(both.text, "\u{e9}");
    assert!(both.truncated);
    assert_eq!(both.dropped_bytes, Some(8));
    assert_eq!(both.total_bytes, Some(11));
}
