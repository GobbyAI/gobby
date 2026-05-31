use super::contracts::{OVERWRITE_GUIDANCE, code_index_index_names, code_index_table_names};
use super::identifiers::quote_identifier;
use super::postgres::postgres_overwrite_reset_sql;
use super::*;
use ::postgres::{Client, NoTls};
use gobby_core::setup::{SetupContext, StandaloneSetup, StoreKind};

#[test]
fn standalone_setup_declares_public_daemon_code_index_subset() {
    let setup = GcodeStandaloneSetup::new("public");
    assert_eq!(setup.namespace(), "gcode");
    assert_eq!(setup.schema(), "public");

    let object_names: Vec<String> = setup
        .owned_objects()
        .expect("owned objects")
        .into_iter()
        .map(|object| object.name)
        .collect();

    assert!(
        object_names
            .iter()
            .any(|name| name.contains("indexed_files"))
    );
    assert!(object_names.iter().any(|name| name.contains("symbols")));
    assert!(
        object_names
            .iter()
            .any(|name| name.contains("content_chunks"))
    );
    assert!(object_names.iter().any(|name| name.contains("idx_cif")));
    assert!(object_names.iter().any(|name| name.contains("bm25")));

    let forbidden = [
        "config_store",
        "schema_migrations",
        "secrets",
        ".gobby/project.json",
        "project_json",
        "code_graph_sync_state",
        "code_vector_sync_state",
    ];
    for name in object_names {
        for forbidden_name in forbidden {
            assert!(
                !name.contains(forbidden_name),
                "standalone setup declared forbidden object {name}"
            );
        }
    }
}

#[test]
fn standalone_setup_uses_gobby_core_contract() {
    fn assert_standalone_setup<T: StandaloneSetup>() {}
    assert_standalone_setup::<GcodeStandaloneSetup>();

    let setup = GcodeStandaloneSetup::new("public");
    let objects = setup.owned_objects().expect("owned objects");
    assert!(
        objects
            .iter()
            .all(|object| object.store == StoreKind::Postgres)
    );
    assert!(
        objects
            .iter()
            .any(|object| object.name == "code_symbols table")
    );
    assert!(
        objects
            .iter()
            .any(|object| object.name == "code_symbols_search_bm25 index")
    );
    assert!(
        objects
            .iter()
            .any(|object| object.name == "pg_search extension")
    );
}

#[test]
fn standalone_setup_create_propagates_owned_object_errors() {
    let setup = GcodeStandaloneSetup::new("a".repeat(64));
    let mut ctx = SetupContext {
        pg: None,
        falkor_config: None,
        qdrant_config: None,
        non_interactive: true,
    };

    let error = setup
        .create(&mut ctx)
        .expect_err("invalid owned object declaration propagates");

    assert!(error.to_string().contains("schema identifier"));
    assert!(error.to_string().contains("at most 63 bytes"));
}

#[test]
fn standalone_setup_rejects_non_public_schema() {
    let request = StandaloneSetupRequest::new(
        true,
        Some("postgresql://localhost/gcode".to_string()),
        Some("gcode_ci".to_string()),
    );
    let err = validate_standalone_request(&request).expect_err("non-public schema fails");
    assert!(err.to_string().contains("public"));
}

#[test]
fn overwrite_reset_sql_is_allowlisted() {
    let sql = postgres_overwrite_reset_sql("public").expect("reset SQL");

    for table in code_index_table_names() {
        assert!(
            sql.contains(&format!("DROP TABLE IF EXISTS \"public\".\"{table}\";")),
            "{sql}"
        );
    }
    for index in code_index_index_names() {
        assert!(
            sql.contains(&format!("DROP INDEX IF EXISTS \"public\".\"{index}\";")),
            "{sql}"
        );
    }

    for forbidden in [
        "config_store",
        "schema_migrations",
        "secrets",
        "tasks",
        "sessions",
        "memory",
        ".gobby/project.json",
    ] {
        assert!(!sql.contains(forbidden), "{sql}");
    }
    assert!(!sql.contains("CASCADE"), "{sql}");
    assert!(!sql.contains("DROP DATABASE"), "{sql}");
    assert!(!sql.contains("DROP SCHEMA"), "{sql}");
}

#[test]
fn overwrite_guidance_names_flag() {
    let request = StandaloneSetupRequest::new(true, None, None);
    assert!(!request.overwrite_code_index);
    assert!(OVERWRITE_GUIDANCE.contains("--overwrite-code-index"));
}

#[test]
fn standalone_setup_request_redacts_password_in_json() {
    let mut request = StandaloneSetupRequest::new(true, None, None);
    request.falkordb_password = Some("secret".to_string());
    request.database_url = Some("postgresql://user:secret@localhost/gcode".to_string());

    let encoded = serde_json::to_string(&request).expect("serialize request");

    assert!(!encoded.contains("falkordb_password"));
    assert!(!encoded.contains("database_url"));
    assert!(!encoded.contains("secret"));
}

#[test]
fn standalone_setup_request_redacts_database_url_in_json() {
    let request = StandaloneSetupRequest::new(
        true,
        Some("postgresql://user:secret@localhost/gcode".to_string()),
        None,
    );

    let encoded = serde_json::to_string(&request).expect("serialize request");

    assert!(!encoded.contains("database_url"));
    assert!(!encoded.contains("secret"));
}

#[test]
fn standalone_setup_request_debug_redacts_database_url() {
    let mut request = StandaloneSetupRequest::new(
        true,
        Some("postgresql://user:secret@localhost/gcode".to_string()),
        None,
    );
    request.falkordb_password = Some("secret2".to_string());

    let debug = format!("{request:?}");

    assert!(debug.contains("<redacted>"));
    assert!(!debug.contains("secret"));
    assert!(!debug.contains("secret2"));
}

#[test]
fn setup_test_database_url_adds_connect_timeout() {
    assert_eq!(
        database_url_with_connect_timeout("postgresql://localhost/gcode"),
        "postgresql://localhost/gcode?connect_timeout=5"
    );
    assert_eq!(
        database_url_with_connect_timeout("postgresql://localhost/gcode?sslmode=require"),
        "postgresql://localhost/gcode?sslmode=require&connect_timeout=5"
    );
    assert_eq!(
        database_url_with_connect_timeout("postgresql://localhost/gcode?connect_timeout=2"),
        "postgresql://localhost/gcode?connect_timeout=2"
    );
}

#[test]
fn quote_identifier_rejects_names_over_postgres_byte_limit() {
    let name = "a".repeat(64);
    let error = quote_identifier(&name, "schema").expect_err("identifier is too long");

    assert!(error.to_string().contains("at most 63 bytes"));
}

#[test]
#[serial_test::serial]
fn overwrite_recreates_incompatible_code_index_and_preserves_sentinel_table() {
    let Ok(database_url) = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL") else {
        eprintln!(
            "skipping PostgreSQL overwrite test: GCODE_POSTGRES_TEST_DATABASE_URL is not set"
        );
        return;
    };
    if let Err(reason) = destructive_postgres_test_allowed(&database_url) {
        eprintln!("skipping PostgreSQL overwrite test: {reason}");
        return;
    }
    let database_url = database_url_with_connect_timeout(&database_url);
    let mut client = Client::connect(&database_url, NoTls).expect("connect test PostgreSQL hub");
    cleanup_code_index_relations(&mut client);
    client
        .batch_execute(
            "CREATE TABLE public.code_symbols (id TEXT PRIMARY KEY);
                 CREATE TABLE IF NOT EXISTS public.gobby_owned_sentinel (
                     key TEXT PRIMARY KEY,
                     value TEXT NOT NULL
                 );
                 INSERT INTO public.gobby_owned_sentinel (key, value)
                 VALUES ('gcode-overwrite-sentinel', 'keep-me')
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
        )
        .expect("seed incompatible code index and sentinel");

    let request = StandaloneSetupRequest::new(true, Some(database_url.clone()), None);
    let err = run_standalone_setup(&request, &mut client)
        .expect_err("incompatible setup fails without overwrite");
    assert!(err.to_string().contains("--overwrite-code-index"));

    let mut overwrite = StandaloneSetupRequest::new(true, Some(database_url), None);
    overwrite.overwrite_code_index = true;
    run_standalone_setup(&overwrite, &mut client).expect("overwrite setup succeeds");

    let has_project_id: bool = client
        .query_one(
            "SELECT EXISTS(
                    SELECT 1
                    FROM pg_attribute
                    WHERE attrelid = 'public.code_symbols'::regclass
                      AND attname = 'project_id'
                      AND attnum > 0
                      AND NOT attisdropped
                )",
            &[],
        )
        .expect("check recreated code_symbols")
        .get(0);
    assert!(has_project_id);

    let sentinel: String = client
        .query_one(
            "SELECT value FROM public.gobby_owned_sentinel WHERE key = 'gcode-overwrite-sentinel'",
            &[],
        )
        .expect("read sentinel")
        .get(0);
    assert_eq!(sentinel, "keep-me");

    cleanup_code_index_relations(&mut client);
    client
        .batch_execute(
            "DELETE FROM public.gobby_owned_sentinel WHERE key = 'gcode-overwrite-sentinel';
                 DROP TABLE IF EXISTS public.gobby_owned_sentinel;",
        )
        .expect("cleanup sentinel");
}

fn destructive_postgres_test_allowed(database_url: &str) -> Result<(), String> {
    if destructive_postgres_test_override_enabled() {
        return Ok(());
    }
    let config = database_url
        .parse::<::postgres::Config>()
        .map_err(|error| format!("database URL could not be parsed: {error}"))?;
    match config.get_dbname() {
        Some(name) if name.ends_with("_test") => Ok(()),
        Some(name) => Err(format!("database name `{name}` does not end with `_test`")),
        None => Err("database URL does not include a database name".to_string()),
    }
}

fn destructive_postgres_test_override_enabled() -> bool {
    std::env::var("GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE")
        .ok()
        .is_some_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
}

fn database_url_with_connect_timeout(database_url: &str) -> String {
    if database_url.contains("connect_timeout=") {
        return database_url.to_string();
    }
    let separator = if database_url.contains('?') { '&' } else { '?' };
    format!("{database_url}{separator}connect_timeout=5")
}

fn cleanup_code_index_relations(client: &mut Client) {
    let sql = postgres_overwrite_reset_sql("public").expect("reset SQL");
    client
        .batch_execute(&sql)
        .expect("cleanup code index objects");
}

#[test]
#[serial_test::serial]
fn destructive_postgres_guard_requires_test_database_name() {
    temp_env::with_var(
        "GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE",
        Option::<&str>::None,
        || {
            assert!(destructive_postgres_test_allowed("postgresql://localhost/gcode_test").is_ok());
            let error = destructive_postgres_test_allowed("postgresql://localhost/gcode")
                .expect_err("non-test database is rejected");
            assert!(error.contains("does not end with `_test`"));
        },
    );
}

#[test]
#[serial_test::serial]
fn destructive_postgres_guard_accepts_explicit_override_values() {
    for value in ["1", "true", "TRUE"] {
        temp_env::with_var("GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE", Some(value), || {
            assert!(destructive_postgres_test_allowed("postgresql://localhost/gcode").is_ok());
        });
    }
    temp_env::with_var("GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE", Some("0"), || {
        assert!(destructive_postgres_test_allowed("postgresql://localhost/gcode").is_err());
    });
}
