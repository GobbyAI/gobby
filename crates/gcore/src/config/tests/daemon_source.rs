use super::*;
use std::collections::{BTreeMap, VecDeque};

#[derive(Debug)]
struct PrimarySource;

impl ConfigSource for PrimarySource {
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        Ok(Some(7))
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        Some(format!("primary:{key}"))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        Ok(format!("primary:{value}"))
    }
}

#[derive(Debug)]
struct SequencedPrimarySource {
    revisions: VecDeque<anyhow::Result<Option<i64>>>,
}

impl SequencedPrimarySource {
    fn new(revisions: impl IntoIterator<Item = anyhow::Result<Option<i64>>>) -> Self {
        Self {
            revisions: revisions.into_iter().collect(),
        }
    }
}

impl ConfigSource for SequencedPrimarySource {
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        self.revisions
            .pop_front()
            .unwrap_or_else(|| anyhow::bail!("revision sequence exhausted"))
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        Some(format!("primary:{key}"))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        Ok(format!("primary:{value}"))
    }
}

#[test]
fn daemon_served_config_filters_routing_keys_and_returns_stored_values() {
    let mut source = DaemonServedConfig::new(
        7,
        BTreeMap::from([
            ("ai.routing".to_string(), "daemon".to_string()),
            ("ai.text_generate.routing".to_string(), "direct".to_string()),
            (
                "ai.embeddings.api_base".to_string(),
                "http://daemon.example/v1".to_string(),
            ),
        ]),
    );

    assert_eq!(source.config_value("ai.routing"), None);
    assert_eq!(source.config_value("ai.text_generate.routing"), None);
    assert_eq!(
        source.config_value("ai.embeddings.api_base").as_deref(),
        Some("http://daemon.example/v1")
    );
}

#[test]
fn daemon_served_config_resolves_values_verbatim() {
    let mut source = DaemonServedConfig::new(7, BTreeMap::new());

    assert_eq!(
        source
            .resolve_value("${CLIENT_ENV_MUST_NOT_EXPAND}")
            .expect("daemon value is already validated"),
        "${CLIENT_ENV_MUST_NOT_EXPAND}"
    );
}

#[test]
fn daemon_or_primary_delegates_to_the_active_source() {
    let mut daemon = DaemonOrPrimary::<PrimarySource>::Daemon(DaemonServedConfig::new(
        7,
        BTreeMap::from([(
            "ai.embeddings.model".to_string(),
            "daemon-model".to_string(),
        )]),
    ));
    assert_eq!(
        daemon.config_value("ai.embeddings.model").as_deref(),
        Some("daemon-model")
    );
    assert_eq!(
        daemon
            .resolve_value("daemon-value")
            .expect("daemon resolution"),
        "daemon-value"
    );

    let mut primary = DaemonOrPrimary::Primary(PrimarySource);
    assert_eq!(
        primary.config_value("ai.embeddings.model").as_deref(),
        Some("primary:ai.embeddings.model")
    );
    assert_eq!(
        primary
            .resolve_value("primary-value")
            .expect("primary resolution"),
        "primary:primary-value"
    );
}

#[test]
fn daemon_with_secrets_falls_through_only_for_secret_reference_keys() {
    let mut source = DaemonOrPrimary::DaemonWithSecrets(
        DaemonServedConfig::new(
            7,
            BTreeMap::from([(
                "ai.embeddings.model".to_string(),
                "daemon-model".to_string(),
            )]),
        ),
        PrimarySource,
    );

    // Served values win; the primary is never consulted for them.
    assert_eq!(
        source.config_value("ai.embeddings.model").as_deref(),
        Some("daemon-model")
    );
    // Secret-reference keys are never served — they fall through.
    assert_eq!(
        source.config_value("ai.embeddings.api_key").as_deref(),
        Some("primary:ai.embeddings.api_key")
    );
    assert_eq!(
        source
            .config_value("databases.falkordb.password")
            .as_deref(),
        Some("primary:databases.falkordb.password")
    );
    // Pattern keys whose {field} segment carries reference secrecy fall
    // through; sibling non-secret fields do not.
    assert_eq!(
        source
            .config_value("ai.generation.endpoints.mine.api_key")
            .as_deref(),
        Some("primary:ai.generation.endpoints.mine.api_key")
    );
    assert_eq!(
        source.config_value("ai.generation.endpoints.mine.api_base"),
        None
    );
    // Unserved non-secret keys stay absent instead of leaking the primary.
    assert_eq!(source.config_value("indexing.respect_gitignore"), None);
    // Routing keys keep their served-config filtering.
    assert_eq!(source.config_value("ai.routing"), None);
}

#[test]
fn daemon_with_secrets_rejects_secret_markers() {
    let mut source = DaemonOrPrimary::DaemonWithSecrets(
        DaemonServedConfig::new(7, BTreeMap::new()),
        PrimarySource,
    );
    let marker = crate::config::secret_marker_prefix() + "falkordb_password";
    let error = source
        .resolve_value(&marker)
        .expect_err("secret marker must fail typed");
    assert!(error.to_string().contains("grant-issuance"));
    assert_eq!(
        source
            .resolve_value("${CLIENT_ENV_MUST_NOT_EXPAND}")
            .expect("daemon value is already validated"),
        "${CLIENT_ENV_MUST_NOT_EXPAND}"
    );
}

#[test]
fn daemon_with_secrets_fails_closed_for_unusable_primary_revisions() {
    let primaries = [
        SequencedPrimarySource::new([Ok(None)]),
        SequencedPrimarySource::new([Err(anyhow::anyhow!("revision unavailable"))]),
        SequencedPrimarySource::new([Ok(Some(8))]),
    ];

    for primary in primaries {
        let mut source = DaemonOrPrimary::DaemonWithSecrets(
            DaemonServedConfig::new(7, BTreeMap::new()),
            primary,
        );
        assert_eq!(source.config_value("ai.embeddings.api_key"), None);
    }
}

#[test]
fn daemon_with_secrets_rejects_revision_changes_around_lookup_and_resolution() {
    let mut lookup_source = DaemonOrPrimary::DaemonWithSecrets(
        DaemonServedConfig::new(7, BTreeMap::new()),
        SequencedPrimarySource::new([Ok(Some(7)), Ok(Some(8))]),
    );
    assert_eq!(lookup_source.config_value("ai.embeddings.api_key"), None);

    let mut resolution_source = DaemonOrPrimary::DaemonWithSecrets(
        DaemonServedConfig::new(7, BTreeMap::new()),
        SequencedPrimarySource::new([Ok(Some(7)), Ok(Some(8))]),
    );
    let marker = crate::config::secret_marker_prefix() + "embedding_api_key";
    let error = resolution_source
        .resolve_value(&marker)
        .expect_err("secret marker must fail typed");
    assert!(error.to_string().contains("grant-issuance"));
}
