use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use super::super::{ConfigSource, DaemonServedConfig, EnvOnlySource};

fn crate_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn crate_file_exists(relative: &str) -> bool {
    crate_root().join(relative).exists()
}

fn secret_marker(name: &str) -> String {
    format!("${}:{name}", "secret")
}

#[test]
fn standalone_modules_and_assets_are_deleted() {
    for relative in [
        "src/runtime_mode.rs",
        "src/secrets.rs",
        "src/setup.rs",
        "src/provisioning/mod.rs",
        "src/provisioning/bootstrap.rs",
        "src/provisioning/hub.rs",
        "src/provisioning/docker.rs",
        "src/provisioning/tests.rs",
        "assets/docker-compose.services.yml",
        "tests/runtime_mode_process.rs",
    ] {
        assert!(
            !crate_file_exists(relative),
            "standalone surface still present: {relative}"
        );
    }
}

#[test]
fn lib_rs_drops_standalone_declarations() {
    let lib_rs = std::fs::read_to_string(crate_root().join("src/lib.rs")).expect("read lib.rs");
    for needle in [
        "pub mod runtime_mode;",
        "pub mod setup;",
        "pub mod provisioning;",
        "pub mod secrets;",
    ] {
        assert!(
            !lib_rs.contains(needle),
            "gcore crate root still declares {needle}"
        );
    }
}

#[test]
fn config_source_survives_with_grant_backed_daemon_implementor() {
    let mut source = DaemonServedConfig::new(
        11,
        BTreeMap::from([(
            "databases.falkordb.host".to_string(),
            "grant-host".to_string(),
        )]),
    );
    assert_eq!(source.snapshot_revision().expect("revision"), Some(11));
    assert_eq!(
        source.config_value("databases.falkordb.host"),
        Some("grant-host".to_string())
    );
    let resolved = source
        .resolve_value("plain-value")
        .expect("non-secret values resolve");
    assert_eq!(resolved, "plain-value");
}

#[test]
fn secret_markers_fail_typed_as_grant_issuance_bugs() {
    let marker = secret_marker("embedding_api_key");
    let mut env = EnvOnlySource;
    let env_error = env
        .resolve_value(&marker)
        .expect_err("secret marker must not resolve on EnvOnlySource");
    assert!(
        format!("{env_error:#}").contains("grant-issuance"),
        "EnvOnlySource must reject secret markers: {env_error:#}"
    );

    let mut daemon = DaemonServedConfig::new(1, BTreeMap::new());
    let daemon_error = daemon
        .resolve_value(&marker)
        .expect_err("secret marker must not resolve on DaemonServedConfig");
    assert!(
        format!("{daemon_error:#}").contains("grant-issuance"),
        "DaemonServedConfig must reject secret markers: {daemon_error:#}"
    );
}

#[test]
fn gwiki_system_model_runtime_mode_enum_is_untouched() {
    let path = crate_root().join("../gwiki/src/commands/code/system_model.rs");
    let source = std::fs::read_to_string(&path).expect("read system_model.rs");
    assert!(
        source.contains("pub enum RuntimeMode"),
        "gwiki system-model RuntimeMode must remain: {}",
        path.display()
    );
}

#[test]
fn client_crates_have_no_qualified_standalone_surfaces() {
    let roots = [
        crate_root(),
        crate_root().join("../gcode"),
        crate_root().join("../gwiki"),
    ];
    let needles = removed_surface_needles();
    let mut hits = Vec::new();
    for root in roots {
        for path in rust_and_doc_sources(&root) {
            if path.ends_with("standalone_removed.rs") {
                continue;
            }
            let source = std::fs::read_to_string(&path).expect("read source");
            for needle in &needles {
                if source.contains(needle) {
                    hits.push(format!("{}: {needle}", path.display()));
                }
            }
        }
    }
    assert!(
        hits.is_empty(),
        "removed standalone surfaces remain:\n{}",
        hits.join("\n")
    );
}

fn removed_surface_needles() -> Vec<String> {
    vec![
        format!("{}::{}", "gobby_core", "runtime_mode"),
        format!("{}{}", "Standalone", "Config"),
        format!("{}{}", "gcore", ".yaml"),
        format!("{}{}", "GCODE_", "DATABASE_URL"),
        format!("{}{}", "GWIKI_", "DATABASE_URL"),
        format!("{}{}", "GOBBY_", "POSTGRES_DSN"),
        secret_marker(""),
    ]
}

fn rust_and_doc_sources(root: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    visit(root, &mut files);
    files
}

fn visit(dir: &Path, files: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries {
        let Ok(entry) = entry else {
            continue;
        };
        let path = entry.path();
        if path.is_dir() {
            if path
                .file_name()
                .is_some_and(|name| matches!(name.to_str(), Some("target" | ".git")))
            {
                continue;
            }
            visit(&path, files);
        } else if path
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| matches!(ext, "rs" | "md"))
        {
            files.push(path);
        }
    }
}
