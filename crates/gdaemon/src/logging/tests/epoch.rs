use std::sync::Arc;

use gobby_core::family::ConfigEpoch;
use serde_json::{Value, json};
use tokio::sync::watch;

use super::*;
use crate::logging::epoch::spawn_applier_to;

fn epoch(revision: i64, values: Value) -> ConfigEpoch {
    let Value::Object(active_values) = values else {
        panic!("active values are an object");
    };
    ConfigEpoch {
        revision,
        active_values: Arc::new(active_values),
    }
}

/// Lets the applier task run on this current-thread runtime. The applier only swaps
/// settings, so a bounded number of yields is enough for it to see a sent epoch.
async fn settle() {
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
}

/// A routed layer installed on this thread, with an applier fed by the returned sender.
struct Routed {
    _guard: tracing::dispatcher::DefaultGuard,
    epochs: watch::Sender<ConfigEpoch>,
    applier: tokio::task::JoinHandle<()>,
}

impl Routed {
    async fn start(settings: LogSettings) -> Self {
        let (dispatch, handle) = routed(settings);
        let guard = tracing::dispatcher::set_default(&dispatch);
        let (epochs, rx) = watch::channel(epoch(1, json!({})));
        let applier = spawn_applier_to(handle, rx);
        settle().await;
        Self {
            _guard: guard,
            epochs,
            applier,
        }
    }

    async fn apply(&self, revision: i64, values: Value) {
        self.epochs.send_replace(epoch(revision, values));
        settle().await;
    }
}

impl Drop for Routed {
    fn drop(&mut self) {
        self.applier.abort();
    }
}

#[tokio::test]
async fn epoch_change_applies_level_at_next_epoch() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(settings_in(dir.path())).await;

    tracing::info!(target: "gobby.daemon", "info at revision one");
    routed.apply(2, json!({"logging.level": "warning"})).await;
    tracing::info!(target: "gobby.daemon", "info at revision two");
    tracing::warn!(target: "gobby.daemon", "warning at revision two");

    let daemon = read(dir.path(), "daemon.log");
    assert!(daemon.contains("info at revision one"), "{daemon}");
    assert!(daemon.contains("warning at revision two"), "{daemon}");
    assert!(!daemon.contains("info at revision two"), "{daemon}");
}

#[tokio::test]
async fn epoch_change_moves_records_to_new_dir() {
    let old = tempfile::tempdir().expect("temporary logs directory");
    let new = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(settings_in(old.path())).await;

    tracing::info!(target: "gobby.daemon", "record before the move");
    let old_bytes = read(old.path(), "daemon.log");
    routed
        .apply(2, json!({"logging.dir": new.path().to_string_lossy()}))
        .await;
    tracing::info!(target: "gobby.daemon", "record after the move");

    assert!(old_bytes.contains("record before the move"), "{old_bytes}");
    assert_eq!(read(old.path(), "daemon.log"), old_bytes);
    let moved = read(new.path(), "daemon.log");
    assert!(moved.contains("record after the move"), "{moved}");
}

#[tokio::test]
async fn a_lower_revision_epoch_is_still_applied() {
    // A restore can move the stored revision backwards; the applier follows the value.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(settings_in(dir.path())).await;

    routed.apply(5, json!({"logging.level": "warning"})).await;
    tracing::info!(target: "gobby.daemon", "info while at warning");
    routed.apply(3, json!({"logging.level": "info"})).await;
    tracing::info!(target: "gobby.daemon", "info after the restore");

    let daemon = read(dir.path(), "daemon.log");
    assert!(daemon.contains("info after the restore"), "{daemon}");
    assert!(!daemon.contains("info while at warning"), "{daemon}");
}

#[tokio::test]
async fn remaining_live_keys_apply_at_next_epoch() {
    // logging.max_size_mb: a lower threshold rotates on the next record.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(LogSettings {
        max_size_mb: 4,
        ..settings_in(dir.path())
    })
    .await;
    tracing::info!(target: "gobby.daemon", "{}", large_message('a'));
    routed.apply(2, json!({"logging.max_size_mb": 1})).await;
    tracing::info!(target: "gobby.daemon", "after the size change");
    assert!(
        exists(dir.path(), "daemon.log.1"),
        "logging.max_size_mb did not apply"
    );
    assert!(read(dir.path(), "daemon.log").contains("after the size change"));
    drop(routed);

    // logging.backup_count: one backup until the change, then two.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(LogSettings {
        max_size_mb: 1,
        backup_count: 1,
        ..settings_in(dir.path())
    })
    .await;
    for tag in ['a', 'b', 'c'] {
        tracing::info!(target: "gobby.daemon", "{}", large_message(tag));
    }
    assert!(
        exists(dir.path(), "daemon.log.1"),
        "daemon.log did not rotate"
    );
    assert!(!exists(dir.path(), "daemon.log.2"));
    routed.apply(2, json!({"logging.backup_count": 2})).await;
    tracing::info!(target: "gobby.daemon", "{}", large_message('d'));
    assert!(
        exists(dir.path(), "daemon.log.2"),
        "logging.backup_count did not apply"
    );
    drop(routed);

    // logging.format: text until the change, then JSON.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(settings_in(dir.path())).await;
    tracing::info!(target: "gobby.daemon", "text before the change");
    routed.apply(2, json!({"logging.format": "json"})).await;
    tracing::info!(target: "gobby.daemon", "json after the change");
    let daemon = read(dir.path(), "daemon.log");
    let lines: Vec<&str> = daemon.lines().collect();
    assert_eq!(lines.len(), 2, "{daemon}");
    assert!(!lines[0].starts_with('{'), "{daemon}");
    let parsed: serde_json::Map<String, Value> = serde_json::from_str(lines[1]).unwrap_or_default();
    assert_eq!(
        parsed.get("message"),
        Some(&json!("json after the change")),
        "{daemon}"
    );
    drop(routed);

    // logging.llm_max_size_mb: the llm file rotates on its own key.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(LogSettings {
        llm_max_size_mb: 4,
        ..settings_in(dir.path())
    })
    .await;
    tracing::info!(target: "gobby.ai.text_generation", "{}", large_message('a'));
    routed.apply(2, json!({"logging.llm_max_size_mb": 1})).await;
    tracing::info!(target: "gobby.ai.text_generation", "after the llm size change");
    assert!(
        exists(dir.path(), "llm.log.1"),
        "logging.llm_max_size_mb did not apply"
    );
    drop(routed);

    // logging.llm_backup_count: one llm backup until the change, then two.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let routed = Routed::start(LogSettings {
        llm_max_size_mb: 1,
        llm_backup_count: 1,
        ..settings_in(dir.path())
    })
    .await;
    for tag in ['a', 'b', 'c'] {
        tracing::info!(target: "gobby.ai.text_generation", "{}", large_message(tag));
    }
    assert!(exists(dir.path(), "llm.log.1"), "llm.log did not rotate");
    assert!(!exists(dir.path(), "llm.log.2"));
    routed
        .apply(2, json!({"logging.llm_backup_count": 2}))
        .await;
    tracing::info!(target: "gobby.ai.text_generation", "{}", large_message('d'));
    assert!(
        exists(dir.path(), "llm.log.2"),
        "logging.llm_backup_count did not apply"
    );
}
