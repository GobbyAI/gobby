use super::*;

#[test]
fn indexing_config_defaults_to_respecting_gitignore() {
    let _env = EnvGuard::new();
    let mut source = TestSource::default();

    let indexing = resolve_indexing_config(&mut source).expect("indexing config");

    assert!(indexing.respect_gitignore);
    assert!(indexing.extra_excludes.is_empty());
}

#[test]
fn indexing_config_resolves_extra_excludes_from_grant_backed_source() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_raw_values([(
        INDEXING_EXTRA_EXCLUDES_KEY,
        r#"["generated","*.snapshot"]"#,
    )]);

    let indexing = resolve_indexing_config(&mut source).expect("indexing config");

    assert_eq!(indexing.extra_excludes, ["generated", "*.snapshot"]);
}

#[test]
fn indexing_config_rejects_invalid_extra_excludes() {
    let _env = EnvGuard::new();
    for raw in [r#""generated""#, r#"[""]"#, r#"["nested/generated"]"#] {
        let mut source = TestSource::with_raw_values([(INDEXING_EXTRA_EXCLUDES_KEY, raw)]);

        let error = resolve_indexing_config(&mut source).expect_err("invalid extra excludes");

        assert!(error.to_string().contains(INDEXING_EXTRA_EXCLUDES_KEY));
    }
}

#[test]
fn indexing_config_resolves_grant_backed_boolean_values() {
    let _env = EnvGuard::new();
    for (raw, expected) in [("true", true), ("false", false)] {
        let mut source = TestSource::with_raw_values([(INDEXING_RESPECT_GITIGNORE_KEY, raw)]);

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
fn indexing_config_env_overrides_grant_backed_source() {
    let env = EnvGuard::new();
    env.set("GOBBY_INDEXING_RESPECT_GITIGNORE", "false");
    let mut source = TestSource::with_raw_values([(INDEXING_RESPECT_GITIGNORE_KEY, "true")]);

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
