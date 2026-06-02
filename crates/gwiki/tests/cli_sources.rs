use std::fs;
use std::path::Path;
use std::process::Output;

mod common;

use gobby_wiki::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest, SourceRecord,
};

fn gwiki(hub: &Path, cwd: &Path, args: &[&str]) -> Output {
    let home = cwd.join("home");
    fs::create_dir_all(&home).expect("create isolated home");
    common::gwiki_command()
        .args(args)
        .env("GOBBY_WIKI_HUB", hub)
        .env("HOME", home)
        .env_remove("GWIKI_DATABASE_URL")
        .env_remove("GWIKI_POSTGRES_TEST_DATABASE_URL")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .env_remove("GCODE_POSTGRES_TEST_DATABASE_URL")
        .current_dir(cwd)
        .output()
        .expect("gwiki binary runs")
}

fn assert_success(output: &Output, label: &str) {
    assert!(
        output.status.success(),
        "{label} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn json_stdout(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).expect("stdout is JSON")
}

fn json_stderr(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stderr).expect("stderr is JSON")
}

fn unique_topic(label: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    format!("{label}-{}-{nanos}", std::process::id())
}

fn initialized_topic(tmp: &Path, label: &str) -> (std::path::PathBuf, String, std::path::PathBuf) {
    let hub = tmp.join("hub");
    let topic = unique_topic(label);
    let init = gwiki(&hub, tmp, &["--format", "json", "init", "--topic", &topic]);
    assert_success(&init, "init");
    let vault = hub.join("topics").join(&topic);
    (hub, topic, vault)
}

fn seed_source(
    vault: &Path,
    label: &str,
    compile_status: CompileStatus,
    source_asset: Option<&str>,
    write_raw: bool,
    write_asset: bool,
) -> SourceRecord {
    let record = SourceManifest::register(
        vault,
        SourceDraft {
            location: format!("https://example.com/{label}"),
            kind: SourceKind::Url,
            fetched_at: "2026-05-30T00:00:00Z".to_string(),
            content: format!("source body {label}").into_bytes(),
            title: Some(format!("Example source {label}")),
            citation: Some(format!("Example citation {label}")),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status,
        },
    )
    .expect("register source");

    if write_raw {
        write_raw_source(vault, &record.id, source_asset);
    }
    if write_asset && let Some(source_asset) = source_asset {
        let path = vault.join(source_asset);
        fs::create_dir_all(path.parent().expect("asset parent")).expect("create asset parent");
        fs::write(path, b"raw asset").expect("write source asset");
    }

    record
}

fn write_raw_source(vault: &Path, id: &str, source_asset: Option<&str>) {
    let path = vault.join("raw").join(format!("{id}.md"));
    fs::create_dir_all(path.parent().expect("raw parent")).expect("create raw parent");
    let asset_line = source_asset
        .map(|path| format!("source_asset: {path}\n"))
        .unwrap_or_default();
    fs::write(
        path,
        format!(
            "---
source_kind: url
{asset_line}---

# Raw source

Source body.
"
        ),
    )
    .expect("write raw source");
}

fn json_array_contains(value: &serde_json::Value, needle: &str) -> bool {
    value
        .as_array()
        .is_some_and(|values| values.iter().any(|value| value == needle))
}

#[test]
fn sources_lists_manifest_entries_raw_path_and_source_asset() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, vault) = initialized_topic(tmp.path(), "sources-list");
    let record = seed_source(
        &vault,
        "list",
        CompileStatus::Pending,
        Some("raw/assets/list.pdf"),
        true,
        true,
    );

    let output = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "sources", "--topic", &topic],
    );
    assert_success(&output, "sources");
    let payload = json_stdout(&output);
    let source = &payload["sources"][0];

    assert_eq!(payload["command"], "sources");
    assert_eq!(payload["status"], "ok");
    assert_eq!(source["id"], record.id);
    assert_eq!(source["kind"], "url");
    assert_eq!(source["title"], "Example source list");
    assert_eq!(source["citation"], "Example citation list");
    assert_eq!(source["content_hash"], record.content_hash);
    assert_eq!(source["fetched_at"], "2026-05-30T00:00:00Z");
    assert_eq!(source["compile_status"], "pending");
    assert_eq!(source["raw_path"], format!("raw/{}.md", record.id));
    assert_eq!(source["raw_exists"], true);
    assert_eq!(source["source_asset"], "raw/assets/list.pdf");
    assert!(
        payload["degradations"]
            .as_array()
            .is_some_and(Vec::is_empty)
    );
}

#[test]
fn remove_source_dry_run_reports_intended_changes_without_mutation() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, vault) = initialized_topic(tmp.path(), "source-dry-run");
    let record = seed_source(
        &vault,
        "dry-run",
        CompileStatus::Pending,
        Some("raw/assets/dry-run.pdf"),
        true,
        true,
    );

    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "remove-source",
            "--topic",
            &topic,
            "--id",
            &record.id,
            "--dry-run",
        ],
    );
    assert_success(&output, "remove-source dry-run");
    let payload = json_stdout(&output);

    assert_eq!(payload["status"], "would_remove");
    assert_eq!(payload["dry_run"], true);
    assert!(json_array_contains(
        &payload["removed_paths"],
        &format!("raw/{}.md", record.id)
    ));
    assert!(json_array_contains(
        &payload["removed_paths"],
        "raw/assets/dry-run.pdf"
    ));
    assert_eq!(payload["index_status"]["status"], "not_run");
    assert_eq!(payload["index_status"]["index_required"], false);
    assert!(
        vault
            .join("raw")
            .join(format!("{}.md", record.id))
            .is_file()
    );
    assert!(vault.join("raw/assets/dry-run.pdf").is_file());
    assert_eq!(
        SourceManifest::read(&vault)
            .expect("read manifest")
            .entries
            .len(),
        1
    );
}

#[test]
fn remove_source_yes_removes_manifest_raw_asset_and_indexes() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, vault) = initialized_topic(tmp.path(), "source-remove");
    let record = seed_source(
        &vault,
        "remove",
        CompileStatus::Compiled,
        Some("raw/assets/remove.pdf"),
        true,
        true,
    );

    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "remove-source",
            "--topic",
            &topic,
            "--id",
            &record.id,
            "--yes",
        ],
    );
    assert_success(&output, "remove-source yes");
    let payload = json_stdout(&output);

    assert_eq!(payload["status"], "removed");
    assert_eq!(payload["dry_run"], false);
    assert_eq!(payload["index_status"]["status"], "indexed");
    assert_eq!(payload["index_status"]["index_required"], false);
    assert!(json_array_contains(
        &payload["follow_up"],
        "audit_recommended"
    ));
    assert!(!vault.join("raw").join(format!("{}.md", record.id)).exists());
    assert!(!vault.join("raw/assets/remove.pdf").exists());
    assert!(
        SourceManifest::read(&vault)
            .expect("read manifest")
            .entries
            .is_empty()
    );
}

#[test]
fn remove_source_keep_asset_preserves_raw_asset() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, vault) = initialized_topic(tmp.path(), "source-keep-asset");
    let record = seed_source(
        &vault,
        "keep-asset",
        CompileStatus::Pending,
        Some("raw/assets/keep.pdf"),
        true,
        true,
    );

    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "remove-source",
            "--topic",
            &topic,
            "--id",
            &record.id,
            "--yes",
            "--keep-asset",
        ],
    );
    assert_success(&output, "remove-source keep-asset");
    let payload = json_stdout(&output);

    assert!(json_array_contains(
        &payload["kept_paths"],
        "raw/assets/keep.pdf"
    ));
    assert!(!vault.join("raw").join(format!("{}.md", record.id)).exists());
    assert!(vault.join("raw/assets/keep.pdf").is_file());
}

#[test]
fn remove_source_missing_id_returns_structured_not_found_error() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, _vault) = initialized_topic(tmp.path(), "source-missing-id");

    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "remove-source",
            "--topic",
            &topic,
            "--id",
            "missing-source",
            "--yes",
        ],
    );

    assert!(!output.status.success());
    let error = json_stderr(&output);
    assert_eq!(error["code"], "not_found");
    assert!(
        error["message"]
            .as_str()
            .is_some_and(|message| message.contains("missing-source")),
        "{error:#}"
    );
}

#[test]
fn remove_source_rejects_unsafe_source_asset_without_mutation() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, vault) = initialized_topic(tmp.path(), "source-unsafe-asset");
    let record = seed_source(
        &vault,
        "unsafe-asset",
        CompileStatus::Pending,
        Some("../escape.pdf"),
        true,
        false,
    );

    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "remove-source",
            "--topic",
            &topic,
            "--id",
            &record.id,
            "--yes",
        ],
    );

    assert!(!output.status.success());
    let error = json_stderr(&output);
    assert_eq!(error["code"], "invalid_input");
    assert!(
        vault
            .join("raw")
            .join(format!("{}.md", record.id))
            .is_file()
    );
    assert_eq!(
        SourceManifest::read(&vault)
            .expect("read manifest")
            .entries
            .len(),
        1
    );
}

#[test]
fn remove_source_tolerates_missing_raw_file_when_manifest_entry_exists() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (hub, topic, vault) = initialized_topic(tmp.path(), "source-missing-raw");
    let record = seed_source(
        &vault,
        "missing-raw",
        CompileStatus::Pending,
        None,
        false,
        false,
    );

    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "remove-source",
            "--topic",
            &topic,
            "--id",
            &record.id,
            "--yes",
        ],
    );
    assert_success(&output, "remove-source missing raw");
    let payload = json_stdout(&output);

    assert_eq!(payload["status"], "removed");
    assert!(json_array_contains(
        &payload["missing_paths"],
        &format!("raw/{}.md", record.id)
    ));
    assert!(json_array_contains(
        &payload["degradations"],
        &format!("raw_missing:raw/{}.md", record.id)
    ));
    assert!(
        SourceManifest::read(&vault)
            .expect("read manifest")
            .entries
            .is_empty()
    );
}
