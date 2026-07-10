use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use super::super::{
    AiGenerationSettings, CodewikiAiOutcome, CodewikiDocMeta, CodewikiIndexSnapshot, CodewikiMeta,
    CodewikiPublication, PublicationFingerprint,
};

fn write_file(root: &Path, relative: &str, content: &str) {
    let path = root.join(relative);
    fs::create_dir_all(path.parent().expect("file parent")).expect("create parent");
    fs::write(path, content).expect("write fixture");
}

fn write_meta(root: &Path, pages: &[&str], marker: &str) -> Vec<u8> {
    let docs = pages
        .iter()
        .map(|page| ((*page).to_string(), CodewikiDocMeta::default()))
        .collect::<BTreeMap<_, _>>();
    let meta = CodewikiMeta {
        docs,
        ai_mode: marker.to_string(),
        ..CodewikiMeta::default()
    };
    let mut content = serde_json::to_vec_pretty(&meta).expect("serialize meta");
    content.push(b'\n');
    write_file(
        root,
        "_meta/codewiki.json",
        std::str::from_utf8(&content).expect("utf8 meta"),
    );
    content
}

fn fingerprint(project_root: &Path, scopes: &[&str]) -> PublicationFingerprint {
    fingerprint_with(
        project_root,
        scopes,
        &AiGenerationSettings::default(),
        &CodewikiIndexSnapshot::default(),
    )
}

fn fingerprint_with(
    project_root: &Path,
    scopes: &[&str],
    settings: &AiGenerationSettings,
    snapshot: &CodewikiIndexSnapshot,
) -> PublicationFingerprint {
    PublicationFingerprint::from_run(
        project_root,
        &["src/lib.rs".to_string()],
        "off",
        settings,
        CodewikiAiOutcome::default(),
        CodewikiAiOutcome::default(),
        &scopes
            .iter()
            .map(|scope| (*scope).to_string())
            .collect::<Vec<_>>(),
        None,
        snapshot,
    )
    .expect("fingerprint")
}

fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf) {
    let temp = tempfile::tempdir().expect("tempdir");
    let project = temp.path().join("project");
    let live = temp.path().join("vault");
    write_file(&project, "src/lib.rs", "pub fn initial() {}\n");
    fs::create_dir_all(&live).expect("live vault");
    (temp, project, live)
}

#[test]
fn interrupted_generation_keeps_live_bytes_and_resumes_compatible_stage() {
    let (_temp, project, live) = fixture();
    let live_page = "---\ntitle: Old\n---\n\n# Old\n";
    write_file(&live, "code/old.md", live_page);
    let live_meta = write_meta(&live, &["code/old.md"], "live");
    let fingerprint = fingerprint(&project, &["src"]);

    let first = CodewikiPublication::prepare(&live, &fingerprint).expect("prepare first stage");
    write_file(first.stage_out(), "code/old.md", "staged partial\n");
    drop(first);

    assert_eq!(
        fs::read(live.join("code/old.md")).expect("live page"),
        live_page.as_bytes()
    );
    assert_eq!(
        fs::read(live.join("_meta/codewiki.json")).expect("live meta"),
        live_meta
    );

    let resumed = CodewikiPublication::prepare(&live, &fingerprint).expect("resume stage");
    assert_eq!(
        fs::read_to_string(resumed.stage_out().join("code/old.md")).expect("staged page"),
        "staged partial\n"
    );
}

#[test]
fn changed_source_hash_discards_incompatible_stage() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    write_meta(&live, &["code/old.md"], "live");
    let first_fingerprint = fingerprint(&project, &["src"]);
    let first =
        CodewikiPublication::prepare(&live, &first_fingerprint).expect("prepare first stage");
    write_file(first.stage_out(), "code/old.md", "stale staged work\n");
    drop(first);

    write_file(&project, "src/lib.rs", "pub fn changed() {}\n");
    let changed_fingerprint = fingerprint(&project, &["src"]);
    let reset = CodewikiPublication::prepare(&live, &changed_fingerprint).expect("reset stage");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("reset page"),
        "live\n"
    );
}

#[test]
fn changed_settings_scope_or_snapshot_discards_incompatible_stage() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    write_meta(&live, &["code/old.md"], "live");

    let initial = fingerprint(&project, &["src"]);
    let staged = CodewikiPublication::prepare(&live, &initial).expect("initial stage");
    write_file(staged.stage_out(), "code/old.md", "scope-stale\n");
    drop(staged);
    let scope_changed = CodewikiPublication::prepare(&live, &fingerprint(&project, &["crates"]))
        .expect("scope reset");
    assert_eq!(
        fs::read_to_string(scope_changed.stage_out().join("code/old.md")).expect("scope page"),
        "live\n"
    );
    write_file(scope_changed.stage_out(), "code/old.md", "settings-stale\n");
    drop(scope_changed);

    let settings = AiGenerationSettings {
        register: "agent".to_string(),
        ..AiGenerationSettings::default()
    };
    let settings_changed = CodewikiPublication::prepare(
        &live,
        &fingerprint_with(
            &project,
            &["crates"],
            &settings,
            &CodewikiIndexSnapshot::default(),
        ),
    )
    .expect("settings reset");
    assert_eq!(
        fs::read_to_string(settings_changed.stage_out().join("code/old.md"))
            .expect("settings page"),
        "live\n"
    );
    write_file(
        settings_changed.stage_out(),
        "code/old.md",
        "snapshot-stale\n",
    );
    drop(settings_changed);

    let snapshot = CodewikiIndexSnapshot {
        degraded_sources: vec!["graph".to_string()],
        ..CodewikiIndexSnapshot::default()
    };
    let snapshot_changed = CodewikiPublication::prepare(
        &live,
        &fingerprint_with(&project, &["crates"], &settings, &snapshot),
    )
    .expect("snapshot reset");
    assert_eq!(
        fs::read_to_string(snapshot_changed.stage_out().join("code/old.md"))
            .expect("snapshot page"),
        "live\n"
    );
}

#[test]
fn publication_fails_closed_when_metadata_names_missing_staged_target() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    let live_meta = write_meta(&live, &["code/old.md"], "live");
    let publication =
        CodewikiPublication::prepare(&live, &fingerprint(&project, &[])).expect("prepare stage");
    write_file(
        publication.stage_out(),
        "code/old.md",
        "# Old\n\n[[code/missing|Missing]]\n",
    );
    write_meta(
        publication.stage_out(),
        &["code/old.md", "code/missing.md"],
        "staged",
    );

    let error = publication
        .publish()
        .expect_err("missing staged page must fail");
    assert!(
        error
            .to_string()
            .contains("missing generated target code/missing.md")
    );
    assert_eq!(
        fs::read_to_string(live.join("code/old.md")).expect("live page"),
        "live\n"
    );
    assert_eq!(
        fs::read(live.join("_meta/codewiki.json")).expect("live meta"),
        live_meta
    );
    assert!(!live.join("_meta/codewiki-publication.json").exists());
}

#[test]
fn interrupted_publication_has_resolving_placeholders_and_recovers_journal() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "old live page\n");
    let live_meta = write_meta(&live, &["code/old.md"], "live");
    let fingerprint = fingerprint(&project, &[]);
    let publication =
        CodewikiPublication::prepare(&live, &fingerprint).expect("prepare publication");
    write_file(
        publication.stage_out(),
        "code/a.md",
        "# A\n\n[[code/b|B]]\n",
    );
    write_file(publication.stage_out(), "code/b.md", "# B\n");
    fs::remove_file(publication.stage_out().join("code/old.md")).expect("remove stale stage page");
    write_meta(
        publication.stage_out(),
        &["code/a.md", "code/b.md"],
        "staged",
    );

    let error = publication
        .publish_interrupt_after_replacements(1)
        .expect_err("interrupt publication");
    assert!(
        error
            .to_string()
            .contains("injected codewiki publication interruption")
    );
    assert!(live.join("_meta/codewiki-publication.json").is_file());
    assert!(
        live.join("code/b.md").is_file(),
        "new link target has a placeholder"
    );
    assert!(
        fs::read_to_string(live.join("code/a.md"))
            .expect("published source")
            .contains("[[code/b|B]]")
    );
    assert_eq!(
        fs::read(live.join("_meta/codewiki.json")).expect("old live meta"),
        live_meta
    );

    let recovered =
        CodewikiPublication::prepare(&live, &fingerprint).expect("recover publication journal");
    assert_eq!(
        fs::read_to_string(live.join("code/b.md")).expect("final b"),
        "# B\n"
    );
    assert!(!live.join("code/old.md").exists());
    assert!(!live.join("_meta/codewiki-publication.json").exists());
    let recovered_meta: CodewikiMeta = serde_json::from_slice(
        &fs::read(live.join("_meta/codewiki.json")).expect("recovered meta"),
    )
    .expect("parse recovered meta");
    assert_eq!(recovered_meta.ai_mode, "staged");

    let changed = recovered.publish().expect("stable republish");
    assert!(changed.contains(&"code/a.md".to_string()));
}
