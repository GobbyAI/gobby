use super::super::embedding_keys;

#[test]
fn embedding_keys_centralized() {
    if std::env::var("RUN_SLOW_TESTS").is_err() {
        eprintln!("skipping slow workspace embedding key scan; set RUN_SLOW_TESTS=1 to run");
        return;
    }

    let workspace = workspace_root();
    let offenders = embedding_key_literal_offenders(&workspace.join("crates"));

    assert!(
        offenders.is_empty(),
        "embedding config key literals must stay in gobby_core::config::embedding_keys: {offenders:?}"
    );
}

#[test]
fn ci_guard_rejects_stray_literal() {
    let dir = tempfile::tempdir().expect("tempdir");
    let src = dir.path().join("src");
    std::fs::create_dir_all(&src).expect("create src");
    std::fs::write(
        src.join("bad.rs"),
        format!(r#"const BAD: &str = "{}";"#, embedding_keys::AI_API_BASE),
    )
    .expect("write bad source");

    let offenders = embedding_key_literal_offenders(dir.path());

    assert_eq!(offenders.len(), 1);
    assert!(offenders[0].ends_with("bad.rs"));
}

fn workspace_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(std::path::Path::parent)
        .expect("workspace root")
        .to_path_buf()
}

fn embedding_key_literal_offenders(root: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut offenders = Vec::new();
    visit_embedding_key_literal_sources(root, &mut offenders);
    offenders
}

fn visit_embedding_key_literal_sources(
    path: &std::path::Path,
    offenders: &mut Vec<std::path::PathBuf>,
) {
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries {
        let entry = entry.expect("directory entry");
        let path = entry.path();
        if path.is_dir() {
            if should_skip_embedding_key_scan_dir(&path) {
                continue;
            }
            visit_embedding_key_literal_sources(&path, offenders);
            continue;
        }
        if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
            continue;
        }
        if embedding_key_literal_allowed_path(&path) {
            continue;
        }
        let source = std::fs::read_to_string(&path).expect("read source file");
        if guarded_embedding_keys()
            .iter()
            .any(|key| source.contains(key.as_str()))
        {
            offenders.push(path);
        }
    }
}

fn should_skip_embedding_key_scan_dir(path: &std::path::Path) -> bool {
    matches!(
        path.file_name().and_then(|name| name.to_str()),
        Some(
            ".git"
                | "target"
                | "node_modules"
                | "dist"
                | "build"
                | ".venv"
                | "venv"
                | "__pycache__"
        )
    )
}

fn guarded_embedding_keys() -> Vec<String> {
    vec![
        embedding_keys::AI_PROVIDER,
        embedding_keys::AI_API_BASE,
        embedding_keys::AI_MODEL,
        embedding_keys::AI_API_KEY,
        embedding_keys::AI_QUERY_PREFIX,
        embedding_keys::AI_DIM,
        embedding_keys::AI_TIMEOUT_SECONDS,
    ]
    .into_iter()
    .map(str::to_string)
    .collect::<Vec<_>>()
}

fn embedding_key_literal_allowed_path(path: &std::path::Path) -> bool {
    let path = path.to_string_lossy();
    path.ends_with("crates/gcore/src/config/types.rs")
        || path.ends_with("tests.rs")
        || path.contains("/tests/")
}
