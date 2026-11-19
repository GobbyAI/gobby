use serde_json::{Map, Value, json};

use super::*;
use crate::logging::settings::{allow_audit_backup_count, from_values};

fn values(document: Value) -> Map<String, Value> {
    match document {
        Value::Object(map) => map,
        other => panic!("active values are an object, not {other}"),
    }
}

#[test]
fn settings_from_active_values_map_keys_and_reject_bad_values() {
    let previous = settings_in(Path::new("/previous/logs"));

    let (next, warnings) = from_values(
        &values(json!({
            "logging.level": "warning",
            "logging.format": "json",
            "logging.dir": "/configured/logs",
            "logging.max_size_mb": 20,
            "logging.backup_count": 3,
            "logging.llm_max_size_mb": 60,
            "logging.llm_backup_count": 4,
            "logging.runtime_max_size_mb": 99,
        })),
        &previous,
    );
    assert_eq!(
        next,
        LogSettings {
            level: Level::Warning,
            format: Format::Json,
            dir: PathBuf::from("/configured/logs"),
            max_size_mb: 20,
            backup_count: 3,
            llm_max_size_mb: 60,
            llm_backup_count: 4,
            ..previous.clone()
        }
    );
    assert_eq!(warnings, Vec::<String>::new());

    let (kept, warnings) = from_values(
        &values(json!({
            "logging.level": "trace",
            "logging.format": "yaml",
            "logging.max_size_mb": 0,
        })),
        &next,
    );
    assert_eq!(kept, next);
    assert_eq!(warnings.len(), 3, "{warnings:?}");
    for key in ["logging.level", "logging.format", "logging.max_size_mb"] {
        assert!(
            warnings.iter().any(|warning| warning.contains(key)),
            "no warning names {key}: {warnings:?}"
        );
    }
}

#[test]
fn allow_audit_defaults_match_python() {
    let defaults = LogSettings::defaults(PathBuf::from("/home/logs"));
    assert_eq!(defaults.allow_audit_max_size_mb, 256);
    assert_eq!(defaults.allow_audit_backup_count, 9);
    // max(1, ceil(14 * 368000 * 512 / (256 * 1048576)) - 1)
    assert_eq!(allow_audit_backup_count(14, 256), 9);
    assert_eq!(allow_audit_backup_count(14, 1_000_000), 1);

    let (unchanged, _) = from_values(
        &values(json!({
            "logging.allow_audit_max_size_mb": 512,
            "logging.allow_audit_retention_days": 30,
        })),
        &defaults,
    );
    assert_eq!(unchanged, defaults);
}

#[test]
fn compiled_defaults_match_python() {
    assert_eq!(
        LogSettings::defaults(PathBuf::from("/home/logs")),
        LogSettings {
            level: Level::Info,
            format: Format::Text,
            dir: PathBuf::from("/home/logs"),
            max_size_mb: 10,
            backup_count: 5,
            llm_max_size_mb: 50,
            llm_backup_count: 5,
            allow_audit_max_size_mb: 256,
            allow_audit_backup_count: 9,
        }
    );
}

#[test]
fn env_dir_overrides_config_and_default_resolves_under_gobby_home() {
    let home = Path::new("/gobby/home");
    let with_env = |name: &str| (name == "GOBBY_LOGGING_DIR").then(|| "/env/logs".to_owned());
    let without_env = |_: &str| None;

    assert_eq!(
        resolve_dir(&with_env, Some(Path::new("/configured/logs")), home),
        PathBuf::from("/env/logs")
    );
    assert_eq!(
        resolve_dir(&without_env, Some(Path::new("/configured/logs")), home),
        PathBuf::from("/configured/logs")
    );
    assert_eq!(
        resolve_dir(&without_env, Some(Path::new("~/.gobby/logs")), home),
        home.join("logs")
    );
    assert_eq!(resolve_dir(&without_env, None, home), home.join("logs"));
}
