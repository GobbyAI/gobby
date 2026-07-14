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

fn fingerprint_variant(
    fingerprint: &PublicationFingerprint,
    field: &str,
    value: serde_json::Value,
) -> PublicationFingerprint {
    let mut encoded = serde_json::to_value(fingerprint).expect("serialize fingerprint");
    encoded[field] = value;
    serde_json::from_value(encoded).expect("deserialize fingerprint variant")
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
fn changed_source_hash_reconciles_stage_and_sticks_full_scan() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    write_meta(&live, &["code/old.md"], "live");
    let first_fingerprint = fingerprint(&project, &["src"]);
    let first =
        CodewikiPublication::prepare(&live, &first_fingerprint).expect("prepare first stage");
    write_file(first.stage_out(), "code/old.md", "retained staged work\n");
    drop(first);

    write_file(&project, "src/lib.rs", "pub fn changed() {}\n");
    let changed_fingerprint = fingerprint(&project, &["src"]);
    let reconciled =
        CodewikiPublication::prepare(&live, &changed_fingerprint).expect("reconcile stage");
    assert_eq!(
        fs::read_to_string(reconciled.stage_out().join("code/old.md")).expect("staged page"),
        "retained staged work\n"
    );
    assert!(reconciled.requires_full_hash_scan());
    drop(reconciled);

    let resumed =
        CodewikiPublication::prepare(&live, &changed_fingerprint).expect("resume reconciled stage");
    assert!(
        resumed.requires_full_hash_scan(),
        "an exact-fingerprint resume must preserve the sticky full-scan flag"
    );
}

#[test]
fn settings_outcomes_snapshot_sources_and_since_reconcile_stage() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    write_meta(&live, &["code/old.md"], "live");

    let mut current = fingerprint(&project, &["src"]);
    let staged = CodewikiPublication::prepare(&live, &current).expect("initial stage");
    write_file(staged.stage_out(), "code/old.md", "retained\n");
    drop(staged);

    let drifts = [
        ("ai_mode", serde_json::json!("symbols")),
        ("ai_prose_depth", serde_json::json!("deep")),
        ("ai_register", serde_json::json!("agent")),
        ("ai_aggregate_profile", serde_json::json!("operator")),
        (
            "ai_aggregate_candidates",
            serde_json::json!(["model-a", "model-b"]),
        ),
        (
            "leaf_ai_outcome",
            serde_json::json!("daemon:false:generated"),
        ),
        (
            "aggregate_ai_outcome",
            serde_json::json!("direct:false:generated"),
        ),
        ("index_snapshot_hash", serde_json::json!("new-snapshot")),
        (
            "source_hashes",
            serde_json::json!({"src/lib.rs": "new-source-hash"}),
        ),
        ("since_changed", serde_json::json!(["src/changed.rs"])),
    ];
    for (field, value) in drifts {
        current = fingerprint_variant(&current, field, value);
        let reconciled = CodewikiPublication::prepare(&live, &current)
            .unwrap_or_else(|error| panic!("reconcile {field}: {error:#}"));
        assert_eq!(
            fs::read_to_string(reconciled.stage_out().join("code/old.md")).expect("retained page"),
            "retained\n",
            "{field} drift must preserve staged bytes"
        );
        assert!(reconciled.requires_full_hash_scan(), "drift field: {field}");
        drop(reconciled);
    }

    let exact = CodewikiPublication::prepare(&live, &current).expect("exact resume");
    assert!(exact.requires_full_hash_scan());
}

#[test]
fn scope_project_root_version_missing_and_corrupt_manifests_reseed() {
    let (temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    write_meta(&live, &["code/old.md"], "live");

    let initial = fingerprint(&project, &["src", "crates"]);
    let staged = CodewikiPublication::prepare(&live, &initial).expect("initial stage");
    write_file(staged.stage_out(), "code/old.md", "staged\n");
    drop(staged);

    let normalized = fingerprint(&project, &["crates", "src", "src"]);
    let resumed = CodewikiPublication::prepare(&live, &normalized).expect("normalized resume");
    assert_eq!(
        fs::read_to_string(resumed.stage_out().join("code/old.md")).expect("resumed page"),
        "staged\n"
    );
    assert!(!resumed.requires_full_hash_scan());
    drop(resumed);

    let scope_changed = fingerprint(&project, &["src"]);
    let reset = CodewikiPublication::prepare(&live, &scope_changed).expect("scope reset");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("scope page"),
        "live\n"
    );
    assert!(!reset.requires_full_hash_scan());
    write_file(reset.stage_out(), "code/old.md", "wrong-root\n");
    drop(reset);

    let other_project = temp.path().join("other-project");
    write_file(&other_project, "src/lib.rs", "pub fn initial() {}\n");
    let other_root = fingerprint(&other_project, &["src"]);
    let reset = CodewikiPublication::prepare(&live, &other_root).expect("project-root reset");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("root page"),
        "live\n"
    );
    write_file(reset.stage_out(), "code/old.md", "wrong-version\n");
    drop(reset);

    let manifest_path = live.join("_meta/codewiki-stage/manifest.json");
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).expect("read manifest"))
            .expect("parse manifest");
    manifest["fingerprint"]["version"] = serde_json::json!(1);
    fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&manifest).expect("serialize old manifest"),
    )
    .expect("write old manifest");
    let reset = CodewikiPublication::prepare(&live, &other_root).expect("version reset");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("version page"),
        "live\n"
    );
    write_file(reset.stage_out(), "code/old.md", "missing-manifest\n");
    drop(reset);

    fs::remove_file(&manifest_path).expect("remove manifest");
    let reset = CodewikiPublication::prepare(&live, &other_root).expect("missing reset");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("missing page"),
        "live\n"
    );
    write_file(reset.stage_out(), "code/old.md", "legacy-manifest\n");
    drop(reset);

    fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&fingerprint_variant(
            &other_root,
            "version",
            serde_json::json!(1),
        ))
        .expect("serialize v1 fingerprint"),
    )
    .expect("write v1 manifest");
    let reset = CodewikiPublication::prepare(&live, &other_root).expect("v1 reset");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("v1 page"),
        "live\n"
    );
    write_file(reset.stage_out(), "code/old.md", "corrupt-manifest\n");
    drop(reset);

    fs::write(&manifest_path, b"not json").expect("corrupt manifest");
    let reset = CodewikiPublication::prepare(&live, &other_root).expect("corrupt reset");
    assert_eq!(
        fs::read_to_string(reset.stage_out().join("code/old.md")).expect("corrupt page"),
        "live\n"
    );
}

#[test]
fn publication_fails_closed_when_metadata_names_missing_staged_target() {
    let (_temp, project, live) = fixture();
    write_file(&live, "code/old.md", "live\n");
    let live_meta = write_meta(&live, &["code/old.md"], "live");
    let initial = fingerprint(&project, &[]);
    let publication = CodewikiPublication::prepare(&live, &initial).expect("prepare stage");
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
    drop(publication);

    write_file(&project, "src/lib.rs", "pub fn drifted() {}\n");
    let reconciled = CodewikiPublication::prepare(&live, &fingerprint(&project, &[]))
        .expect("reconcile broken stage");
    assert!(reconciled.requires_full_hash_scan());

    let error = reconciled
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

#[test]
fn publish_ignores_wikilinks_quoted_in_code_spans_and_fences() {
    // A generated page may QUOTE wikilink syntax when documenting the link
    // renderer itself (#17823); quoted examples are not links and must not
    // fail validation, while real links keep resolving.
    let (_temp, project, live) = fixture();
    let publication =
        CodewikiPublication::prepare(&live, &fingerprint(&project, &[])).expect("prepare stage");
    write_file(
        publication.stage_out(),
        "code/renderer.md",
        "# Renderer\n\n\
         Renders a `Module: [[code/modules/<file.module>]]` link per file.\n\n\
         ``inline [[code/double-quoted|X]] span``\n\n\
         ```rust\n\
         let link = \"[[code/fenced-example]]\";\n\
         ```\n\n\
         ~~~\n\
         [[code/tilde-fenced]]\n\
         ~~~\n\n\
         Real link: [[code/real|Real]]\n",
    );
    write_file(publication.stage_out(), "code/real.md", "# Real\n");
    write_meta(
        publication.stage_out(),
        &["code/renderer.md", "code/real.md"],
        "staged",
    );

    let changed = publication
        .publish()
        .expect("quoted wikilinks in code spans must not block publish");

    assert!(changed.contains(&"code/renderer.md".to_string()));
    assert!(live.join("code/real.md").is_file());
    assert!(
        !live.join("code/modules").exists(),
        "no placeholder may be created for a quoted example link"
    );
}

#[test]
fn publish_still_fails_on_real_broken_link_after_unmatched_backtick() {
    // An unmatched backtick run is literal text, so extraction must continue
    // past it and still catch genuinely broken links later in the page.
    let (_temp, project, live) = fixture();
    let publication =
        CodewikiPublication::prepare(&live, &fingerprint(&project, &[])).expect("prepare stage");
    write_file(
        publication.stage_out(),
        "code/page.md",
        "# Page\n\nstray ` backtick\n\n[[code/missing|Missing]]\n",
    );
    write_meta(publication.stage_out(), &["code/page.md"], "staged");

    let error = publication
        .publish()
        .expect_err("real broken link must still fail publish");
    assert!(
        error
            .to_string()
            .contains("has no staged target code/missing.md")
    );
}

#[test]
fn fingerprint_skips_sources_missing_from_disk() {
    // The index can list files deleted (or not yet born on a frozen checkout)
    // between indexing and the run; fingerprinting must skip them like the
    // snapshot builder does instead of aborting the run (#18248, cf. #18109).
    let (_temp, project, _live) = fixture();
    let fingerprint = PublicationFingerprint::from_run(
        &project,
        &[
            "src/lib.rs".to_string(),
            "src/deleted_since_indexing.py".to_string(),
        ],
        "off",
        &AiGenerationSettings::default(),
        CodewikiAiOutcome::default(),
        CodewikiAiOutcome::default(),
        &[],
        None,
        &CodewikiIndexSnapshot::default(),
    )
    .expect("fingerprint must skip missing sources, not abort");

    let json = serde_json::to_value(&fingerprint).expect("serialize fingerprint");
    let hashes = json["source_hashes"]
        .as_object()
        .expect("source_hashes map");
    assert!(hashes.contains_key("src/lib.rs"));
    assert!(!hashes.contains_key("src/deleted_since_indexing.py"));
}
