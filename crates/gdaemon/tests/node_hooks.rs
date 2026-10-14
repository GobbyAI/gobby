use gobby_daemon::nodes::hooks::ledger::{
    Ledger, PROCESSED_MARKER_PRUNE_MAX_ENTRIES, PROCESSED_MARKER_RETENTION_SECONDS,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, path::Path, process::Command, time::SystemTime};
use tempfile::TempDir;
use time::{Duration, OffsetDateTime};

fn ledger() -> (TempDir, Ledger) {
    let home = TempDir::new().unwrap();
    let ledger = Ledger::new(home.path().join("hooks/inbox"));
    (home, ledger)
}
fn marker_path(ledger: &Ledger, id: &str) -> std::path::PathBuf {
    ledger
        .inbox
        .join("processed")
        .join(format!("{:x}.json", Sha256::digest(id.as_bytes())))
}
fn write_marker(ledger: &Ledger, id: &str, record: Value) {
    fs::create_dir_all(ledger.inbox.join("processed")).unwrap();
    fs::write(marker_path(ledger, id), record.to_string()).unwrap();
}
fn iso(at: OffsetDateTime) -> String {
    at.format(&time::format_description::well_known::Rfc3339)
        .unwrap()
}
#[tokio::test]
async fn ledger_claim_is_exclusive() {
    let (_home, ledger) = ledger();
    let (a, b) = tokio::join!(ledger.claim("exclusive"), ledger.claim("exclusive"));
    let claims = [a.unwrap(), b.unwrap()];
    assert_eq!(claims.iter().filter(|token| token.is_some()).count(), 1);
    let token = claims.into_iter().flatten().next().unwrap();
    let record = ledger.marker("exclusive").await.unwrap().unwrap();
    assert_eq!(record["status"], "processing");
    assert_eq!(record["owner_token"], token);
    assert_eq!(record["owner_pid"], std::process::id());
    assert!(record["owner_create_time"].as_f64().unwrap() > 0.0);
    let claimed = OffsetDateTime::parse(
        record["claimed_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    let expiry = OffsetDateTime::parse(
        record["lease_expires_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    assert_eq!((expiry - claimed).whole_seconds(), 15);
    let raw = fs::read_to_string(marker_path(&ledger, "exclusive")).unwrap();
    assert!(raw.ends_with('\n'));
    assert!(raw.find("claimed_at").unwrap() < raw.find("owner_token").unwrap());
    assert!(ledger.claim("").await.unwrap().is_none());
}
#[tokio::test]
async fn ledger_mutations_require_owner_token() {
    let (_home, ledger) = ledger();
    let token = ledger.claim("owned").await.unwrap().unwrap();
    let initial = ledger.marker("owned").await.unwrap();
    assert!(!ledger.renew("owned", "wrong").await.unwrap());
    assert!(
        !ledger
            .finalize(
                "owned",
                "wrong",
                Some(json!({"decision":"allow"})),
                Some("stop")
            )
            .await
            .unwrap()
    );
    assert!(!ledger.release("owned", "wrong").await.unwrap());
    assert_eq!(ledger.marker("owned").await.unwrap(), initial);
    assert!(ledger.renew("owned", &token).await.unwrap());
    let renewed = ledger.marker("owned").await.unwrap().unwrap();
    let before = OffsetDateTime::parse(
        initial.as_ref().unwrap()["renewed_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    let after = OffsetDateTime::parse(
        renewed["renewed_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    assert!(after >= before);
    let response = json!({"decision":"allow", "nested":{"ok":true}});
    assert!(
        ledger
            .finalize("owned", &token, Some(response.clone()), Some("stop"))
            .await
            .unwrap()
    );
    assert!(!ledger.release("owned", &token).await.unwrap());
    assert!(ledger.is_processed("owned").await.unwrap());
    ledger.mark_processed("owned", None, None).await.unwrap();
    assert_eq!(
        ledger.terminal_response("owned", None).await.unwrap(),
        Some(response)
    );
    let token = ledger.claim("release").await.unwrap().unwrap();
    ledger.mark_processed("release", None, None).await.unwrap();
    assert!(ledger.release("release", &token).await.unwrap());
    assert!(ledger.claim("release").await.unwrap().is_some());
}
#[tokio::test]
async fn ledger_stale_and_malformed_markers() {
    let (_home, ledger) = ledger();
    let now = OffsetDateTime::now_utc();
    ledger.claim("live").await.unwrap().unwrap();
    let mut live = ledger.marker("live").await.unwrap().unwrap();
    live["lease_expires_at"] = json!(iso(now - Duration::seconds(30)));
    write_marker(&ledger, "live", live.clone());
    assert!(ledger.is_active("live", Some(now)).await.unwrap());
    assert!(!ledger.clear_stale("live", Some(now)).await.unwrap());
    live["owner_create_time"] = json!(0.0);
    write_marker(&ledger, "reused-pid", live.clone());
    assert!(!ledger.is_active("reused-pid", Some(now)).await.unwrap());
    assert!(ledger.clear_stale("reused-pid", Some(now)).await.unwrap());
    live["owner_pid"] = json!(i32::MAX);
    live["lease_expires_at"] = json!(iso(now + Duration::seconds(15)));
    write_marker(&ledger, "lease", live);
    assert!(ledger.is_active("lease", Some(now)).await.unwrap());
    assert!(!ledger.clear_stale("lease", Some(now)).await.unwrap());
    assert!(
        ledger
            .clear_stale("lease", Some(now + Duration::seconds(15)))
            .await
            .unwrap()
    );
    for (name, raw, processed) in [
        ("broken", "{garbage", true),
        ("empty", "  ", false),
        ("array", "[]", false),
    ] {
        fs::write(marker_path(&ledger, name), raw).unwrap();
        assert_eq!(
            ledger.is_processed(name).await.unwrap(),
            processed,
            "{name}"
        );
    }
    assert!(
        !ledger
            .clear_stale("broken", Some(now + Duration::days(1)))
            .await
            .unwrap()
    );
    assert!(
        ledger
            .clear_stale("empty", Some(now + Duration::days(1)))
            .await
            .unwrap()
    );
    write_marker(
        &ledger,
        "legacy",
        json!({"status":"processing","claimed_at":iso(now)}),
    );
    assert!(
        ledger
            .is_active("legacy", Some(now + Duration::seconds(119)))
            .await
            .unwrap()
    );
    assert!(
        ledger
            .clear_stale("legacy", Some(now + Duration::seconds(120)))
            .await
            .unwrap()
    );
}
#[tokio::test]
async fn ledger_stop_block_replay_window() {
    let (_home, ledger) = ledger();
    let now = OffsetDateTime::now_utc();
    let response = json!({"decision":"block","reason":"pending work"});
    for hook in [
        "stop",
        "subagent-stop",
        "subagent_end",
        "session_end",
        " TURN_END ",
        "stop_failure",
    ] {
        write_marker(
            &ledger,
            "stop",
            json!({"status":"processed","processed_at":iso(now),"hook_type":hook,"response":response}),
        );
        assert_eq!(
            ledger
                .terminal_response("stop", Some(now + Duration::seconds(119)))
                .await
                .unwrap(),
            Some(response.clone())
        );
        assert!(
            ledger
                .terminal_response("stop", Some(now + Duration::seconds(120)))
                .await
                .unwrap()
                .is_none()
        );
        assert!(ledger.marker("stop").await.unwrap().is_none());
    }
    ledger.bump_stop_epoch(Some(now)).await.unwrap();
    assert_eq!(ledger.read_stop_epoch().await.unwrap(), Some(now));
    for (id, at, replay) in [
        ("before", now - Duration::seconds(1), false),
        ("equal", now, true),
        ("future", now + Duration::seconds(1), false),
    ] {
        write_marker(
            &ledger,
            id,
            json!({"processed_at":iso(at),"hook_type":"stop","response":response}),
        );
        assert_eq!(
            ledger
                .terminal_response(id, Some(now))
                .await
                .unwrap()
                .is_some(),
            replay
        );
    }
    let legacy = json!({"decision":"deny","reason":"[block-terminal-validation-failure] retry"});
    write_marker(
        &ledger,
        "legacy",
        json!({"processed_at":iso(now - Duration::seconds(121)),"response":legacy}),
    );
    assert!(
        ledger
            .terminal_response("legacy", Some(now))
            .await
            .unwrap()
            .is_none()
    );
    let tool = json!({"decision":"deny","reason":"policy"});
    write_marker(
        &ledger,
        "tool",
        json!({"processed_at":iso(now - Duration::days(2)),"hook_type":"pre_tool_use","response":tool}),
    );
    assert_eq!(
        ledger.terminal_response("tool", Some(now)).await.unwrap(),
        Some(tool)
    );
    let allow = json!({"decision":"allow"});
    write_marker(
        &ledger,
        "allow",
        json!({"hook_type":"stop","response":allow}),
    );
    assert_eq!(
        ledger.terminal_response("allow", Some(now)).await.unwrap(),
        Some(allow)
    );
}
fn age(path: &Path, seconds: u64) {
    fs::File::open(path)
        .unwrap()
        .set_times(
            fs::FileTimes::new()
                .set_modified(SystemTime::now() - std::time::Duration::from_secs(seconds)),
        )
        .unwrap();
}
#[tokio::test]
async fn ledger_prune_retention() {
    let (_home, ledger) = ledger();
    let now = OffsetDateTime::now_utc();
    assert_eq!(PROCESSED_MARKER_PRUNE_MAX_ENTRIES, 100_000);
    assert_eq!(PROCESSED_MARKER_RETENTION_SECONDS, 86_400.0);
    for id in ["old-a", "old-b", "old-c", "fresh"] {
        ledger.mark_processed(id, None, None).await.unwrap();
        age(
            &marker_path(&ledger, id),
            if id == "fresh" { 100 } else { 86_500 },
        );
    }
    let pass = ledger
        .prune(Some(now), PROCESSED_MARKER_RETENTION_SECONDS, 2)
        .await
        .unwrap();
    assert!(pass.truncated);
    assert!(pass.deleted <= 2);
    assert!(ledger.is_processed("fresh").await.unwrap());
    let pass2 = ledger
        .prune(
            Some(now),
            PROCESSED_MARKER_RETENTION_SECONDS,
            PROCESSED_MARKER_PRUNE_MAX_ENTRIES,
        )
        .await
        .unwrap();
    assert_eq!(pass.deleted + pass2.deleted, 3);
    assert!(ledger.inbox.join("processed/.locks").is_dir());
    assert!(ledger.is_processed("fresh").await.unwrap());

    // Hardlinks make the production-size bound cheap to exercise without
    // allocating 100,002 separate file bodies. All links retain the old mtime.
    let bound_home = TempDir::new().unwrap();
    let bounded = Ledger::new(bound_home.path().join("inbox"));
    let processed = bounded.inbox.join("processed");
    fs::create_dir_all(&processed).unwrap();
    let seed = processed.join("seed");
    fs::write(&seed, "{}").unwrap();
    age(&seed, 86_500);
    for index in 0..100_001 {
        fs::hard_link(&seed, processed.join(index.to_string())).unwrap();
    }
    let capped = bounded
        .prune(Some(now), PROCESSED_MARKER_RETENTION_SECONDS, usize::MAX)
        .await
        .unwrap();
    assert_eq!(capped.examined, 100_000);
    assert_eq!(capped.deleted, 100_000);
    assert!(capped.truncated);
    let remainder = bounded
        .prune(
            Some(now),
            PROCESSED_MARKER_RETENTION_SECONDS,
            PROCESSED_MARKER_PRUNE_MAX_ENTRIES,
        )
        .await
        .unwrap();
    assert_eq!(remainder.deleted, 2);
}
#[tokio::test]
async fn ledger_reads_python_markers() {
    let (_home, ledger) = ledger();
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let output = Command::new("uv").args(["run", "--no-sync", "python", "-c", "from pathlib import Path; import sys; from gobby.hooks.envelope_dedupe import mark_envelope_processed; mark_envelope_processed('python', processed_dir=Path(sys.argv[1]), response={'decision':'deny','reason':'oracle','nested':{'count':2}}, hook_type='pre_tool_use')"]).arg(ledger.inbox.join("processed")).current_dir(root).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let record = ledger.marker("python").await.unwrap().unwrap();
    assert_eq!(record["status"], "processed");
    assert_eq!(record["hook_type"], "pre_tool_use");
    assert_eq!(
        ledger.terminal_response("python", None).await.unwrap(),
        Some(json!({"decision":"deny","reason":"oracle","nested":{"count":2}}))
    );
}
#[test]
fn ledger_inbox_freshness() {
    let now = OffsetDateTime::now_utc();
    let millis = now.unix_timestamp_nanos() / 1_000_000;
    assert!(Ledger::is_fresh(
        Path::new(&format!("n-{millis}-id.json")),
        Some(now),
        120.0
    ));
    assert!(!Ledger::is_fresh(
        Path::new(&format!("c-{}-id.json", millis - 120_000)),
        Some(now),
        120.0
    ));
    for name in [
        "n-no-id.json",
        "x-123-id.json",
        "n-123-",
        "n-99999999999999999999999999999-id",
    ] {
        assert!(
            !Ledger::is_fresh(Path::new(name), Some(now), 120.0),
            "{name}"
        );
    }
    assert!(!Ledger::is_fresh(
        Path::new(&format!("n-{}-id.json", millis + 1000)),
        Some(now),
        120.0
    ));
}
