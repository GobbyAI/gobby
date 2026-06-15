use super::*;
use crate::provisioning::StandaloneConfig;

#[test]
fn indexing_config_defaults_to_respecting_gitignore() {
    let _env = EnvGuard::new();
    let mut source = TestSource::default();

    let indexing = resolve_indexing_config(&mut source).expect("indexing config");

    assert!(indexing.respect_gitignore);
}

#[test]
fn indexing_config_resolves_standalone_yaml_values() {
    let _env = EnvGuard::new();
    for (raw, expected) in [("true", true), ("false", false)] {
        let standalone =
            StandaloneConfig::from_yaml_str(&format!("indexing:\n  respect_gitignore: {raw}\n"))
                .expect("standalone config");
        let mut source =
            LayeredConfigSource::<TestSource, StandaloneConfig>::new(None, Some(standalone));

        let indexing = resolve_indexing_config(&mut source).expect("indexing config");

        assert_eq!(indexing.respect_gitignore, expected);
    }
}

#[test]
fn indexing_config_resolves_config_store_values_before_yaml() {
    let _env = EnvGuard::new();
    for (raw, yaml, expected) in [("false", "true", false), ("true", "false", true)] {
        let store = TestSource::with_raw_values([(INDEXING_RESPECT_GITIGNORE_KEY, raw)]);
        let standalone =
            StandaloneConfig::from_yaml_str(&format!("indexing:\n  respect_gitignore: {yaml}\n"))
                .expect("standalone config");
        let mut source = LayeredConfigSource::new(Some(store), Some(standalone));

        let indexing = resolve_indexing_config(&mut source).expect("indexing config");

        assert_eq!(indexing.respect_gitignore, expected);
    }
}

#[test]
#[serial_test::serial(config_log_capture)]
fn indexing_config_invalid_boolean_warns_and_uses_default() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([(INDEXING_RESPECT_GITIGNORE_KEY, "sometimes")]);

    let (indexing, warnings) =
        capture_warn_logs(|| resolve_indexing_config(&mut source).expect("indexing config"));

    assert!(indexing.respect_gitignore);
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains(INDEXING_RESPECT_GITIGNORE_KEY))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("invalid boolean"));
    assert!(matching[0].contains("using default true"));
    assert!(!matching[0].contains("sometimes"));
}

#[test]
fn indexing_config_env_overrides_config_sources() {
    let env = EnvGuard::new();
    env.set("GOBBY_INDEXING_RESPECT_GITIGNORE", "false");
    let standalone = StandaloneConfig::from_yaml_str("indexing:\n  respect_gitignore: true\n")
        .expect("standalone config");
    let store = TestSource::with_raw_values([(INDEXING_RESPECT_GITIGNORE_KEY, "true")]);
    let mut source = LayeredConfigSource::new(Some(store), Some(standalone));

    let indexing = resolve_indexing_config(&mut source).expect("indexing config");

    assert!(!indexing.respect_gitignore);
}

#[test]
#[serial_test::serial(config_log_capture)]
fn indexing_config_invalid_environment_boolean_warns_and_uses_default() {
    let env = EnvGuard::new();
    env.set("GOBBY_INDEXING_RESPECT_GITIGNORE", "sometimes");
    let mut source = TestSource::with_values([(INDEXING_RESPECT_GITIGNORE_KEY, "false")]);

    let (indexing, warnings) =
        capture_warn_logs(|| resolve_indexing_config(&mut source).expect("indexing config"));

    assert!(indexing.respect_gitignore);
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains("GOBBY_INDEXING_RESPECT_GITIGNORE"))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("invalid boolean"));
    assert!(matching[0].contains("using default true"));
    assert!(!matching[0].contains("sometimes"));
}
