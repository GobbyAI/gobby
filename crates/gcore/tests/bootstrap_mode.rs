use anyhow::Result;
use gobby_core::bootstrap::{RunMode, parse_hub_database_bootstrap};

#[test]
fn native_mode_matches_python_bootstrap() -> Result<()> {
    for (yaml, expected) in [
        ("{}", RunMode::Standalone),
        ("datastore_mode: local\nhub: true", RunMode::Hub),
        ("datastore_mode: remote\nhub: true", RunMode::Node),
        ("datastore_mode: remote", RunMode::Node),
    ] {
        assert_eq!(
            parse_hub_database_bootstrap(yaml)?.expect("mapping").mode,
            expected
        );
    }
    assert!(parse_hub_database_bootstrap("datastore_mode: typo").is_err());
    assert!(parse_hub_database_bootstrap("hub: string").is_err());
    Ok(())
}
