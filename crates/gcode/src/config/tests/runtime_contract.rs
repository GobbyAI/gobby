use std::collections::BTreeMap;
use std::sync::mpsc;

use gobby_core::config::{
    DaemonServedConfig, decode_dynamic_segment, encode_dynamic_segment, invalid_dynamic_segments,
    is_registered_runtime_key, runtime_contract_codec_vectors,
};
use gobby_core::provisioning::StandaloneConfig;

use super::super::layers::ServiceSource;
use super::super::runtime_contract::{
    HubConfigSnapshot, capture_hub_snapshot, capture_hub_snapshot_with_hook,
};
use super::super::services::{ServiceConfigSource, resolve_embedding_config_from_service_source};

const WRAPPED_DEK: &str = "gAAAAABJlgLSAAAAAAAAAAAAAAAAAAAAAC9b8RrAMGFhE3wxSkKBFwLhxkJrL8D_3Yz5NsOSpTGXwSHaKKYAAv7LiH3nK3KaCIs4vSSbctmyv763hbGrXx2-xFonOlEINxCtTMFY15-9";
const OLD_SECRET: &str = "gAAAAABqeTBkgwaijnvZhdQ-QMCz5vfCDguUWt0BOd3HbXTZRbxczN-0AT1Al2d0t7VfszRfkM_9P9_6_sMLB1v_xwMu6lnuCQ==";
const NEW_SECRET: &str = "gAAAAABqeTBkwMABPndJJlia01OccTFyT6-40tAj4Pei5ZdQGisMXrNmcYblLwX1HwaywiuU7GTGueemar2p7y6ONw2Dz53yyQ==";

#[test]
#[serial_test::serial]
fn gobby_mode_uses_registry_authority() {
    super::with_service_env(&[("GOBBY_FALKORDB_HOST", Some("env-host"))], || {
        let served = DaemonServedConfig::new(BTreeMap::from([(
            "databases.falkordb.host".to_string(),
            "daemon-host".to_string(),
        )]));
        let mut source = ServiceSource::daemon(served);

        assert_eq!(
            source
                .config_value("databases.falkordb.host")
                .expect("daemon value"),
            Some("daemon-host".to_string())
        );
        assert_eq!(
            source
                .config_value("databases.qdrant.url")
                .expect("missing daemon value"),
            None
        );
    });
}

#[test]
#[serial_test::serial(serial_db)]
fn hub_fallback_reads_atomic_snapshot() {
    let database_url = crate::test_env::postgres_test_database_url("runtime contract snapshot");
    let schema = format!("runtime_contract_test_{}", std::process::id());
    let mut setup = gobby_core::postgres::connect_readwrite(&database_url).expect("setup database");
    setup
        .batch_execute(&format!(
            "CREATE SCHEMA IF NOT EXISTS {schema}; SET search_path TO {schema};\
             CREATE TABLE config_state (id boolean PRIMARY KEY, revision bigint NOT NULL);\
             CREATE TABLE config_store (key text PRIMARY KEY, value text NOT NULL);\
             CREATE TABLE secret_key_material (\
                 id text PRIMARY KEY, wrapped_dek text NOT NULL, kek_posture text NOT NULL,\
                 kek_salt text, kek_kdf_n integer, kek_kdf_r integer, kek_kdf_p integer\
             );\
             CREATE TABLE secrets (name text PRIMARY KEY, encrypted_value text NOT NULL);"
        ))
        .expect("create isolated snapshot schema");
    setup
        .execute("INSERT INTO config_state VALUES (true, 40)", &[])
        .expect("insert revision");
    for (key, value) in [
        ("ai.embeddings.routing", "\"direct\""),
        ("ai.embeddings.api_base", "\"http://embedding.test/v1\""),
        ("ai.embeddings.model", "\"old-model\""),
        ("ai.embeddings.api_key", "\"$secret:embedding_api_key\""),
    ] {
        setup
            .execute(
                "INSERT INTO config_store (key, value) VALUES ($1, $2)",
                &[&key, &value],
            )
            .expect("insert config value");
    }
    setup
        .execute(
            "INSERT INTO secret_key_material \
             (id, wrapped_dek, kek_posture) VALUES ('default', $1, 'key_file')",
            &[&WRAPPED_DEK],
        )
        .expect("insert key material");
    setup
        .execute(
            "INSERT INTO secrets (name, encrypted_value) VALUES ('embedding_api_key', $1)",
            &[&OLD_SECRET],
        )
        .expect("insert old secret");

    let home = tempfile::tempdir().expect("temporary Gobby home");
    std::fs::write(
        home.path().join(".secret_kek"),
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=\n",
    )
    .expect("write fixture KEK");
    temp_env::with_var("GOBBY_HOME", Some(home.path()), || {
        let (captured_tx, captured_rx) = mpsc::channel();
        let (resume_tx, resume_rx) = mpsc::channel();
        let reader_url = database_url.clone();
        let reader_schema = schema.clone();
        let reader = std::thread::spawn(move || {
            let mut connection =
                gobby_core::postgres::connect_readwrite(&reader_url).expect("reader connection");
            connection
                .batch_execute(&format!("SET search_path TO {reader_schema}"))
                .expect("set reader search path");
            capture_hub_snapshot_with_hook(&mut connection, || {
                captured_tx.send(()).expect("announce capture");
                resume_rx.recv().expect("resume capture");
            })
            .expect("capture old snapshot")
        });

        captured_rx.recv().expect("reader captured config rows");
        let mut writer =
            gobby_core::postgres::connect_readwrite(&database_url).expect("writer connection");
        writer
            .batch_execute(&format!(
                "SET search_path TO {schema}; BEGIN;\
                 UPDATE config_store SET value = '\"new-model\"' \
                 WHERE key = 'ai.embeddings.model';\
                 UPDATE secrets SET encrypted_value = '{NEW_SECRET}' \
                 WHERE name = 'embedding_api_key';\
                 UPDATE config_state SET revision = 41 WHERE id = true; COMMIT;"
            ))
            .expect("rotate config and same-reference secret");
        resume_tx.send(()).expect("resume reader");

        let old = reader.join().expect("reader thread");
        assert_eq!(old.revision(), 40);
        assert_embedding_snapshot(old, "old-model", "old-secret");
        let current = capture_hub_snapshot(&mut writer).expect("capture new snapshot");
        assert_eq!(current.revision(), 41);
        assert_embedding_snapshot(current, "new-model", "new-secret");
    });

    setup
        .batch_execute(&format!(
            "SET search_path TO public; DROP SCHEMA {schema} CASCADE"
        ))
        .expect("drop isolated snapshot schema");
}

#[test]
fn dynamic_segment_codec_matches_python() {
    for vector in runtime_contract_codec_vectors() {
        assert_eq!(
            encode_dynamic_segment(&vector.decoded).expect("encodable vector"),
            vector.encoded
        );
        assert_eq!(
            decode_dynamic_segment(&vector.encoded).expect("canonical vector"),
            vector.decoded
        );
    }
    for invalid in invalid_dynamic_segments() {
        assert!(
            decode_dynamic_segment(invalid).is_err(),
            "accepted {invalid:?}"
        );
    }
    assert!(encode_dynamic_segment("").is_err());
    assert!(is_registered_runtime_key("launch_defaults.dot%2Esegment"));
    assert!(!is_registered_runtime_key("launch_defaults.dot.segment"));
}

#[test]
#[serial_test::serial]
fn standalone_mode_preserves_env_yaml_precedence() {
    let standalone = StandaloneConfig::from_yaml_str(
        "databases:\n  falkordb:\n    host: yaml-host\n    port: 16379\n",
    )
    .expect("standalone config");
    super::with_service_env(&[("GOBBY_FALKORDB_HOST", Some("env-host"))], || {
        let mut source = ServiceSource::standalone(Some(standalone.clone()));
        assert_eq!(
            source
                .config_value("databases.falkordb.host")
                .expect("environment value"),
            Some("env-host".to_string())
        );
    });
    super::with_service_env(&[], || {
        let mut source = ServiceSource::standalone(Some(standalone));
        assert_eq!(
            source
                .config_value("databases.falkordb.host")
                .expect("YAML value"),
            Some("yaml-host".to_string())
        );
    });
}

fn assert_embedding_snapshot(snapshot: HubConfigSnapshot, model: &str, secret: &str) {
    let mut source = ServiceSource::hub(snapshot);
    let config = resolve_embedding_config_from_service_source(None, &mut source)
        .expect("embedding config from snapshot");
    let config = config.expect("direct embedding config");
    assert_eq!(config.model, model);
    assert_eq!(config.api_key.as_deref(), Some(secret));
}
