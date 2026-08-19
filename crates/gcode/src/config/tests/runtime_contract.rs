use std::collections::BTreeMap;
use std::sync::mpsc;

use super::super::layers::ServiceSource;
use super::super::runtime_contract::{capture_hub_snapshot, capture_hub_snapshot_with_hook};
use super::super::services::ServiceConfigSource;
use gobby_core::config::{
    DaemonServedConfig, decode_dynamic_segment, encode_dynamic_segment, invalid_dynamic_segments,
    is_registered_runtime_key, runtime_contract_codec_vectors,
};

#[test]
#[serial_test::serial]
fn gobby_mode_uses_registry_authority() {
    super::with_service_env(&[], || {
        let served = DaemonServedConfig::new(
            7,
            BTreeMap::from([(
                "databases.falkordb.host".to_string(),
                "daemon-host".to_string(),
            )]),
        );
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
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn hub_fallback_reads_atomic_snapshot() {
    let database_url = crate::test_env::postgres_test_database_url("runtime contract snapshot");
    let schema = format!("runtime_contract_test_{}", std::process::id());
    let mut setup = gobby_core::postgres::connect_readwrite(&database_url).expect("setup database");
    setup
        .batch_execute(&format!(
            "CREATE SCHEMA IF NOT EXISTS {schema}; SET search_path TO {schema};\
             CREATE TABLE config_state (id boolean PRIMARY KEY, revision bigint NOT NULL);\
             CREATE TABLE config_store (key text PRIMARY KEY, value text NOT NULL);"
        ))
        .expect("create isolated snapshot schema");
    setup
        .execute("INSERT INTO config_state VALUES (true, 40)", &[])
        .expect("insert revision");
    for (key, value) in [
        ("ai.embeddings.routing", "\"daemon\""),
        ("ai.embeddings.api_base", "\"http://embedding.test/v1\""),
        ("ai.embeddings.model", "\"old-model\""),
        ("ai.embeddings.api_key", "\"old-key\""),
    ] {
        setup
            .execute(
                "INSERT INTO config_store (key, value) VALUES ($1, $2)",
                &[&key, &value],
            )
            .expect("insert config value");
    }
    {
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
                 UPDATE config_store SET value = '\"new-key\"' \
                 WHERE key = 'ai.embeddings.api_key';\
                 UPDATE config_state SET revision = 41 WHERE id = true; COMMIT;"
            ))
            .expect("rotate config values");
        resume_tx.send(()).expect("resume reader");

        let old = reader.join().expect("reader thread");
        assert_eq!(old.revision(), 40);
        assert_eq!(old.value("ai.embeddings.model"), Some("old-model"));
        assert_eq!(old.value("ai.embeddings.api_key"), Some("old-key"));
        let current = capture_hub_snapshot(&mut writer).expect("capture new snapshot");
        assert_eq!(current.revision(), 41);
        assert_eq!(current.value("ai.embeddings.model"), Some("new-model"));
        assert_eq!(current.value("ai.embeddings.api_key"), Some("new-key"));

        let stale_daemon = DaemonServedConfig::new(
            40,
            BTreeMap::from([("ai.embeddings.model".to_string(), "old-model".to_string())]),
        );
        let current = capture_hub_snapshot(&mut writer).expect("recapture new snapshot");
        let error = match ServiceSource::daemon_with_snapshot(stale_daemon, current) {
            Ok(_) => panic!("stale daemon and current secret snapshot must not combine"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("daemon=40, hub=41"));
    }

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
