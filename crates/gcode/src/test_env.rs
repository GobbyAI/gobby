#![cfg(any(test, feature = "test-support"))]

use gobby_test_support::hub_guard::{DbUrlError, database_url_with_env};

#[cfg(test)]
const GOBBY_HOME_ENV: &str = "GOBBY_HOME";
const GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV: &str = "GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE";
// The fixture applies the schema and seeds users/machines rows, so it owns its
// database outright; pytest's gobby_test is a different `*_test` database.
const GCODE_POSTGRES_TEST_DATABASE: &str = "gobby_gcode_test";

pub fn postgres_test_database_url(purpose: &str) -> String {
    let database_url = resolve_postgres_test_database_url(purpose);
    ensure_code_index_schema(&database_url);
    database_url
}

/// Resolve the test database URL: `DATABASE_URL` through the shared guard, then gcode's
/// `gobby_gcode_test` namespace on that hub, refusing any other database unless
/// `GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE` overrides the guard. `GOBBY_POSTGRES_TEST_*`
/// and the operator's bootstrap.yaml are never read.
fn resolve_postgres_test_database_url(purpose: &str) -> String {
    let Some(database_url) = postgres_test_database_url_from_sources() else {
        panic!(
            "{purpose} requires a PostgreSQL test database URL; set DATABASE_URL to \
             the isolated test hub (gobby_test), whose `{GCODE_POSTGRES_TEST_DATABASE}` \
             namespace gcode uses"
        )
    };
    if let Err(reason) = destructive_postgres_test_allowed(&database_url) {
        panic!(
            "{purpose} refused the resolved PostgreSQL database URL: {reason}. \
             gcode DB tests only run against `{GCODE_POSTGRES_TEST_DATABASE}`; \
             {GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV}=1 bypasses this guard"
        );
    }
    let config = database_url
        .parse::<postgres::Config>()
        .expect("validated test DSN");
    if config.get_dbname().is_some_and(is_gcode_test_database) {
        let identity = gobby_core::schema::schema_identity();
        database_url_for_name(
            &database_url,
            &schema_test_database_name(identity.latest_asset.version, &identity.root_hash),
        )
    } else {
        database_url
    }
}

// A 128-bit hash prefix keeps cohort names below PostgreSQL's 63-byte limit.
fn schema_test_database_name(version: i32, root_hash: &str) -> String {
    format!(
        "{GCODE_POSTGRES_TEST_DATABASE}_v{version}_{}",
        &root_hash[..32]
    )
}

fn is_gcode_test_database(name: &str) -> bool {
    name == GCODE_POSTGRES_TEST_DATABASE
        || name.starts_with(&format!("{GCODE_POSTGRES_TEST_DATABASE}_"))
}

fn database_url_for_name(database_url: &str, name: &str) -> String {
    if let Ok(mut url) = reqwest::Url::parse(database_url) {
        url.set_path(&format!("/{name}"));
        // PostgreSQL URI queries treat '+' literally, unlike form encoding.
        if let Some(query) = url.query().map(|query| query.replace('+', "%2B")) {
            url.set_query(Some(&query));
        }
        let options: Vec<_> = url
            .query_pairs()
            .filter(|(key, _)| key != "dbname")
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();
        if url.query().is_some() {
            url.query_pairs_mut().clear().extend_pairs(options);
            let query = url
                .query()
                .expect("preserved DSN query")
                .replace('+', "%20");
            url.set_query(Some(&query));
        }
        url.to_string()
    } else {
        // libpq keyword strings use the last value for a repeated option.
        format!("{database_url} dbname='{}'", name.replace('\'', "\\'"))
    }
}

/// The one environment read of the resolver: `DATABASE_URL` through the shared guard entry,
/// with the database name replaced by gcode's own `gobby_gcode_test` namespace on that hub.
fn database_url_from(env: &dyn Fn(&str) -> Option<String>) -> Result<String, DbUrlError> {
    database_url_with_env(env)
        .map(|base| database_url_for_name(&base, GCODE_POSTGRES_TEST_DATABASE))
}

fn postgres_test_database_url_from_sources() -> Option<String> {
    match database_url_from(&non_empty_env) {
        Ok(url) => Some(url),
        Err(DbUrlError::Unset) => None,
        Err(error) => panic!("{error}"),
    }
}

fn non_empty_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// Refuse destructive operations against any database but
/// `gobby_gcode_test`, unless `GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE`
/// explicitly overrides the guard.
pub fn destructive_postgres_test_allowed(database_url: &str) -> Result<(), String> {
    if destructive_postgres_test_override_enabled() {
        return Ok(());
    }
    let config = database_url
        .parse::<postgres::Config>()
        .map_err(|error| format!("database URL could not be parsed: {error}"))?;
    match config.get_dbname() {
        Some(name) if is_gcode_test_database(name) => Ok(()),
        Some(name) => Err(format!(
            "database name `{name}` is not the dedicated `{GCODE_POSTGRES_TEST_DATABASE}`"
        )),
        None => Err("database URL does not include a database name".to_string()),
    }
}

pub fn destructive_postgres_test_override_enabled() -> bool {
    std::env::var(GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV)
        .ok()
        .is_some_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
}

/// Provision the code-index schema once per process so DB-backed tests pass
/// from any starting database state via hub schema apply. Any database the
/// guard refuses is left untouched.
fn ensure_code_index_schema(database_url: &str) {
    static PROVISIONED: std::sync::OnceLock<
        std::sync::Mutex<std::collections::HashMap<String, Result<(), String>>>,
    > = std::sync::OnceLock::new();
    let provisioned =
        PROVISIONED.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()));
    let mut provisioned = provisioned
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let result = provisioned
        .entry(database_url.to_string())
        .or_insert_with(|| provision_code_index_schema(database_url));
    if let Err(message) = result {
        panic!("provisioning the code-index test schema failed: {message}");
    }
}

fn provision_code_index_schema(database_url: &str) -> Result<(), String> {
    if let Err(reason) = destructive_postgres_test_allowed(database_url) {
        eprintln!("skipping code-index test schema provisioning: {reason}");
        return Ok(());
    }
    let _provisioning_lock = ensure_test_database_exists(database_url)?;
    let mut client = gobby_core::postgres::connect_readwrite(database_url)
        .map_err(|error| format!("connect to the test database: {error:#}"))?;
    client
        .batch_execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        .map_err(|error| format!("install test pg_search extension: {error:#}"))?;
    {
        let mut runner = gobby_core::schema::SchemaRunner::new(&mut client, "public")
            .map_err(|error| format!("schema runner: {error:#}"))?;
        runner
            .apply()
            .map(|_| ())
            .map_err(|error| format!("gdaemon schema apply: {error:#}"))?;
    }
    let machine_id = gobby_core::machine::read_local_machine_id()
        .map_err(|error| format!("read local machine id: {error:#}"))?;
    seed_test_machine(&mut client, &machine_id)
}

/// Keep this connection alive to serialize database, extension and schema setup.
fn ensure_test_database_exists(database_url: &str) -> Result<postgres::Client, String> {
    let config = database_url
        .parse::<postgres::Config>()
        .map_err(|error| error.to_string())?;
    let name = config.get_dbname().ok_or("test database name is missing")?;
    let admin_url = database_url_for_name(database_url, "postgres");
    let mut admin = gobby_core::postgres::connect_readwrite(&admin_url)
        .map_err(|error| format!("connect to test hub for provisioning: {error:#}"))?;
    admin
        .execute("SELECT pg_advisory_lock(hashtext($1))", &[&name])
        .map_err(|error| format!("lock test database creation: {error:#}"))?;
    let exists = admin
        .query_opt("SELECT 1 FROM pg_database WHERE datname = $1", &[&name])
        .map_err(|error| format!("inspect test database: {error:#}"))?
        .is_some();
    if !exists {
        admin
            .batch_execute(&format!(
                "CREATE DATABASE \"{}\"",
                name.replace('"', "\"\"")
            ))
            .map_err(|error| format!("create schema-versioned test database: {error:#}"))?;
    }
    // The connection owns the advisory lock, including on error paths.
    Ok(admin)
}

/// Register `machine_id` in `machines` under a synthetic test owner.
///
/// Code-index selector tables reference `machines`, and a fresh test database
/// has no row for this machine or for the synthetic machines that
/// multi-machine tests invent. Idempotent, so tests can call it freely.
pub fn seed_test_machine(client: &mut postgres::Client, machine_id: &str) -> Result<(), String> {
    let machine_id = uuid::Uuid::parse_str(machine_id)
        .map_err(|error| format!("parse test machine id {machine_id:?}: {error}"))?;
    let owner_id: uuid::Uuid = client
        .query_one(
            "INSERT INTO users (id, email, name, password_hash)
             VALUES (gen_random_uuid(), $1, 'gcode tests', 'unused')
             ON CONFLICT (lower(email)) DO UPDATE SET updated_at = now()
             RETURNING id",
            &[&"gcode-tests@example.invalid"],
        )
        .map_err(|error| format!("seed test owner user: {error}"))?
        .get(0);
    client
        .execute(
            "INSERT INTO machines (id, hostname, owner_user_id)
             VALUES ($1, 'gcode-tests', $2)
             ON CONFLICT (id) DO NOTHING",
            &[&machine_id, &owner_id],
        )
        .map_err(|error| format!("seed local machine row: {error}"))?;
    Ok(())
}

/// Register `root` as this machine's checkout for `project_id`, creating the
/// registry project row when needed. Primary index writes are fenced on this
/// row, so every test that indexes a root in Primary mode registers it first.
/// Idempotent: a re-run with a fresh temp root rebinds the same project id.
pub fn seed_test_checkout(
    client: &mut postgres::Client,
    project_id: &str,
    root: &std::path::Path,
) -> Result<(), String> {
    let root = root
        .canonicalize()
        .map_err(|error| format!("canonicalize test checkout {}: {error}", root.display()))?;
    let machine_id = gobby_core::machine::read_local_machine_id()
        .map_err(|error| format!("read local machine id: {error:#}"))?;
    let machine_id = uuid::Uuid::parse_str(&machine_id)
        .map_err(|error| format!("parse local machine id {machine_id:?}: {error}"))?;
    let project_uuid = uuid::Uuid::parse_str(project_id)
        .map_err(|error| format!("parse test project id {project_id:?}: {error}"))?;
    client
        .execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)
             ON CONFLICT (id) DO NOTHING",
            &[&project_uuid, &format!("gcode-test-{project_id}")],
        )
        .map_err(|error| format!("seed registry project row: {error}"))?;
    client
        .execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path)
             VALUES ($1, $2, $3)
             ON CONFLICT (machine_id, project_id) DO UPDATE SET
                root_path = EXCLUDED.root_path,
                updated_at = now()",
            &[&machine_id, &project_uuid, &root.to_string_lossy()],
        )
        .map_err(|error| format!("seed local checkout row: {error}"))?;
    Ok(())
}

/// Deployment token the test principal is issued under. The managed path never reads it
/// back; the issuance function only requires it to be non-empty.
const TEST_PRINCIPAL_DEPLOYMENT_TOKEN: &str = "gcode-test-deployment";

/// A managed principal issued for one test project through the production issuance
/// function. Its binding is keyed on the login role the DSN authenticates as, so the
/// managed grant carries [`TestPrincipal::database_url`]. Dropping it removes the
/// binding and the role.
pub struct TestPrincipal {
    admin_database_url: String,
    role_name: String,
    database_url: String,
}

impl TestPrincipal {
    /// DSN that authenticates as the issued role on the admin database.
    pub fn database_url(&self) -> &str {
        &self.database_url
    }
}

impl Drop for TestPrincipal {
    fn drop(&mut self) {
        let mut admin = match gobby_core::postgres::connect_readwrite(&self.admin_database_url) {
            Ok(admin) => admin,
            Err(err) => {
                eprintln!(
                    "TestPrincipal: connect failed for role {}: {err:#}",
                    self.role_name
                );
                return;
            }
        };
        if let Err(err) = admin.execute(
            "DELETE FROM gobby_agent_auth.principal_bindings WHERE role_name::TEXT = $1",
            &[&self.role_name],
        ) {
            eprintln!(
                "TestPrincipal: binding cleanup failed for role {}: {}",
                self.role_name,
                describe_db_error(&err)
            );
        }
        let drop_role = format!("DROP ROLE IF EXISTS {}", quote_ident(&self.role_name));
        if let Err(err) = admin.batch_execute(&drop_role) {
            eprintln!(
                "TestPrincipal: role cleanup failed for role {}: {}",
                self.role_name,
                describe_db_error(&err)
            );
        }
    }
}

/// Issues the managed principal for `project_id` through the production issuance
/// function and returns the DSN the managed grant carries for it. The fixture replaces
/// any prior binding for the project rather than stacking one; the hub's RLS helpers
/// fail closed on zero or duplicate live bindings.
///
/// The admin session cannot be the principal. The binding is keyed on `session_user`,
/// and the binding trigger claims the bound role with COMMENT ON ROLE, which PostgreSQL
/// refuses for a superuser role (the hub's admin role is one). Production never binds a
/// superuser: every issuer creates a NOSUPERUSER login role, and the issuer holds the
/// ADMIN option that the claim needs on roles it created.
pub fn seed_test_principal_binding(
    client: &mut postgres::Client,
    admin_database_url: &str,
    project_id: &str,
) -> Result<TestPrincipal, String> {
    let machine_id = gobby_core::machine::read_local_machine_id()
        .map_err(|error| format!("read local machine id: {error:#}"))?;
    let machine_id = uuid::Uuid::parse_str(&machine_id)
        .map_err(|error| format!("parse local machine id {machine_id:?}: {error}"))?;
    let project_uuid = uuid::Uuid::parse_str(project_id)
        .map_err(|error| format!("parse test project id {project_id:?}: {error}"))?;
    client
        .execute(
            "DELETE FROM gobby_agent_auth.principal_bindings WHERE project_id = $1",
            &[&project_uuid],
        )
        .map_err(|error| format!("clear prior principal binding: {error}"))?;
    let password = uuid::Uuid::new_v4().simple().to_string();
    let role_name: String = client
        .query_one(
            "SELECT role_name::TEXT
             FROM gobby_agent_auth.issue_or_reuse_interactive_principal(
                 $1, $2, $3, NULL, now() + interval '1 hour', $4, NULL)",
            &[
                &TEST_PRINCIPAL_DEPLOYMENT_TOKEN,
                &machine_id,
                &project_uuid,
                &password,
            ],
        )
        .map_err(|error| format!("issue managed principal: {}", describe_db_error(&error)))?
        .get(0);
    Ok(TestPrincipal {
        admin_database_url: admin_database_url.to_string(),
        database_url: database_url_as_role(admin_database_url, &role_name, &password),
        role_name,
    })
}

/// The same database and host as `admin_database_url`, authenticated as `role_name`.
pub fn database_url_as_role(admin_database_url: &str, role_name: &str, password: &str) -> String {
    let mut url = reqwest::Url::parse(admin_database_url).expect("test database URL is a URL");
    url.set_username(role_name)
        .expect("set the issued role as the DSN user");
    url.set_password(Some(password))
        .expect("set the issued role password");
    url.to_string()
}

fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// The server's message and detail, which `postgres::Error`'s own display omits.
fn describe_db_error(error: &postgres::Error) -> String {
    match error.as_db_error() {
        Some(db) => match db.detail() {
            Some(detail) => format!("{}: {detail}", db.message()),
            None => db.message().to_string(),
        },
        None => error.to_string(),
    }
}

/// Collection the production code-symbol lifecycle writes for `project_id`.
pub fn code_symbol_collection(project_id: &str) -> String {
    crate::vector::code_symbols::collection_name(
        crate::config::CODE_SYMBOL_COLLECTION_PREFIX,
        project_id,
    )
    .expect("code symbol collection name")
}

#[cfg(test)]
mod tests {
    use super::*;

    const POSTGRES_TEST_ENV_KEYS: &[&str] = &[
        "GCODE_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_POSTGRES_TEST_DATABASE_URL",
        "DATABASE_URL",
        "GOBBY_POSTGRES_TEST_DB",
        "GOBBY_POSTGRES_TEST_USER",
        "GOBBY_POSTGRES_TEST_PASSWORD",
        "GOBBY_POSTGRES_TEST_HOST",
        "GOBBY_POSTGRES_TEST_PORT",
        "GOBBY_AGENT_RUN_ID",
        "GOBBY_MANAGED_EXECUTION_ID",
        gobby_core::grant::MANAGED_BOOTSTRAP_ENV,
        "GOBBY_SESSION_ID",
        "GOBBY_PARENT_SESSION_ID",
        gobby_core::local_token::AGENT_API_TOKEN_ENV,
        "GOBBY_DAEMON_URL",
        "GOBBY_PORT",
        "GOBBY_DAEMON_PORT",
        GOBBY_HOME_ENV,
        GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
    ];

    fn with_postgres_test_env<R>(
        overrides: &[(&str, Option<&str>)],
        closure: impl FnOnce() -> R,
    ) -> R {
        let vars = POSTGRES_TEST_ENV_KEYS
            .iter()
            .map(|key| {
                let value = overrides
                    .iter()
                    .find_map(|(name, value)| (*name == *key).then_some(*value))
                    .unwrap_or(None);
                (*key, value)
            })
            .collect::<Vec<_>>();
        temp_env::with_vars(vars, closure)
    }

    #[test]
    fn database_url_comes_from_the_shared_entry_into_the_gcode_namespace() {
        const TEST_HUB: &str = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test";
        const LIVE_HUB: &str = "postgresql://gobby:gobby@localhost:60891/gobby";
        let only_database_url = |name: &str| (name == "DATABASE_URL").then(|| TEST_HUB.to_string());
        assert_eq!(
            database_url_from(&only_database_url),
            Ok("postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test".to_string())
        );
        let live_hub = |name: &str| (name == "DATABASE_URL").then(|| LIVE_HUB.to_string());
        assert!(matches!(
            database_url_from(&live_hub),
            Err(gobby_test_support::hub_guard::DbUrlError::Refused(_))
        ));
    }

    const TEST_HUB: &str = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test";
    const GCODE_NAMESPACE_HUB: &str =
        "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test";

    // The managed grant's DSN must authenticate as the issued login role while keeping
    // the admin database: the seeded binding is keyed on session_user.
    #[test]
    fn managed_dsn_authenticates_as_the_issued_role_on_the_admin_database() {
        let dsn = database_url_as_role(
            GCODE_NAMESPACE_HUB,
            "gobby_ix_probe_role",
            "fixture-password",
        );
        let config: postgres::Config = dsn.parse().expect("managed DSN parses");
        assert_eq!(config.get_user(), Some("gobby_ix_probe_role"));
        assert_eq!(config.get_password(), Some("fixture-password".as_bytes()));
        assert_eq!(config.get_dbname(), Some("gobby_gcode_test"));
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_reads_the_shared_database_url_into_the_gcode_namespace() {
        with_postgres_test_env(&[("DATABASE_URL", Some(TEST_HUB))], || {
            assert_eq!(
                postgres_test_database_url_from_sources().as_deref(),
                Some(GCODE_NAMESPACE_HUB)
            );
        });
    }

    // The GOBBY_POSTGRES_TEST_* family is the pytest stack's and points at gobby_test;
    // the gcode fixture must never pick it up.
    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_ignores_pytest_database_sources() {
        with_postgres_test_env(
            &[
                (
                    "GOBBY_POSTGRES_TEST_DATABASE_URL",
                    Some("postgresql://localhost/gobby_test"),
                ),
                ("GOBBY_POSTGRES_TEST_DB", Some("gobby_test")),
                ("GOBBY_POSTGRES_TEST_USER", Some("gobby_test")),
            ],
            || {
                assert_eq!(postgres_test_database_url_from_sources().as_deref(), None);
            },
        );
    }

    // The gcode-only variable is retired: DATABASE_URL is the one source.
    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_ignores_the_retired_gcode_variable() {
        with_postgres_test_env(
            &[(
                "GCODE_POSTGRES_TEST_DATABASE_URL",
                Some(GCODE_NAMESPACE_HUB),
            )],
            || {
                assert_eq!(postgres_test_database_url_from_sources().as_deref(), None);
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_does_not_use_bootstrap_yaml() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap/gobby\n",
        )
        .unwrap();
        let home = dir.path().to_str().unwrap();

        with_postgres_test_env(&[(GOBBY_HOME_ENV, Some(home))], || {
            assert_eq!(postgres_test_database_url_from_sources().as_deref(), None);
        });
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn resolve_refuses_a_live_hub_database_url() {
        with_postgres_test_env(
            &[(
                "DATABASE_URL",
                Some("postgresql://gobby:gobby@localhost:60891/gobby"),
            )],
            || {
                let panic =
                    std::panic::catch_unwind(|| resolve_postgres_test_database_url("guard tests"))
                        .expect_err("the live hub is refused");
                let message = panic
                    .downcast_ref::<String>()
                    .expect("panic payload is a formatted String");
                assert!(message.contains("R3"), "{message}");
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn resolve_selects_compiled_schema_database() {
        with_postgres_test_env(&[("DATABASE_URL", Some(TEST_HUB))], || {
            assert_eq!(resolve_postgres_test_database_url("guard tests"), {
                let identity = gobby_core::schema::schema_identity();
                format!(
                    "postgresql://gobby_test:gobby_test@127.0.0.1:60892/{}",
                    schema_test_database_name(identity.latest_asset.version, &identity.root_hash)
                )
            });
        });
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn destructive_postgres_guard_accepts_only_the_dedicated_database() {
        temp_env::with_var(
            GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
            Option::<&str>::None,
            || {
                for name in [
                    "gobby_gcode_test",
                    "gobby_gcode_test_v458",
                    "gobby_gcode_test_v459",
                ] {
                    assert!(
                        destructive_postgres_test_allowed(&format!(
                            "postgresql://localhost/{name}"
                        ))
                        .is_ok(),
                        "{name}"
                    );
                }
                for name in [
                    "gobby_test",
                    "gcode_test",
                    "gobby",
                    "gobby_gcode_testing",
                    "gobby_gcode_testevil",
                ] {
                    let error = destructive_postgres_test_allowed(&format!(
                        "postgresql://localhost/{name}"
                    ))
                    .expect_err("only gobby_gcode_test is accepted");
                    assert!(
                        error
                            .contains(&format!("`{name}` is not the dedicated `gobby_gcode_test`")),
                        "{error}"
                    );
                }
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn schema_database_selection_preserves_query_options() {
        let identity = gobby_core::schema::schema_identity();
        let version = identity.latest_asset.version;
        let name = schema_test_database_name(version, &identity.root_hash);
        for source in [
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test?application_name=fixture+app&sslmode=disable".to_string(),
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test?sslmode=disable&application_name=fixture+app".to_string(),
        ] {
            with_postgres_test_env(&[("DATABASE_URL", Some(&source))], || {
                let first = resolve_postgres_test_database_url("schema cohorts");
                let second = resolve_postgres_test_database_url("schema cohorts");
                assert_eq!(first, second, "same compiled schema shares a target");
                let config = first.parse::<postgres::Config>().expect("selected DSN");
                assert_eq!(config.get_dbname(), Some(name.as_str()));
                assert_eq!(config.get_user(), Some("gobby_test"));
                assert_eq!(config.get_password(), Some(b"gobby_test".as_slice()));
                assert_eq!(config.get_ports(), &[60892]);
                assert_eq!(config.get_application_name(), Some("fixture+app"));
                assert_eq!(config.get_ssl_mode(), postgres::config::SslMode::Disable);
            });
        }
    }

    #[test]
    fn schema_database_cohorts_separate_divergent_roots_at_the_same_version() {
        let identity = gobby_core::schema::schema_identity();
        let first = schema_test_database_name(identity.latest_asset.version, &identity.root_hash);
        let mut other_root = identity.root_hash.clone();
        other_root.replace_range(
            ..1,
            if other_root.starts_with('0') {
                "1"
            } else {
                "0"
            },
        );
        let second = schema_test_database_name(identity.latest_asset.version, &other_root);
        assert_ne!(
            first, second,
            "divergent migration roots must not share a database"
        );
        assert_eq!(
            first,
            schema_test_database_name(identity.latest_asset.version, &identity.root_hash)
        );
        assert!(
            first.len() <= 63,
            "PostgreSQL must not truncate the cohort name"
        );
        assert!(is_gcode_test_database(&first));
        assert!(is_gcode_test_database(&second));
    }

    #[test]
    #[cfg(gcode_postgres_tests)]
    #[serial_test::serial(serial_db)]
    fn serial_db_newer_schema_database_does_not_poison_compiled_schema_cohort() -> anyhow::Result<()>
    {
        let identity = gobby_core::schema::schema_identity();
        let version = identity.latest_asset.version;
        let source = gobby_test_support::hub_guard::database_url_for_tests().expect("DB test DSN");
        // Only this unique disposable probe database carries the simulated
        // future receipt. Never alter or drop another runner's shared cohort.
        let future_name = format!(
            "gobby_gcode_test_v{}_probe_{}",
            version + 1,
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        );
        let future_url = database_url_for_name(&source, &future_name);
        struct ProbeDatabase {
            admin: postgres::Client,
            name: String,
        }
        impl Drop for ProbeDatabase {
            fn drop(&mut self) {
                if let Err(error) = self
                    .admin
                    .batch_execute(&format!("DROP DATABASE \"{}\"", self.name))
                {
                    if std::thread::panicking() {
                        eprintln!("probe database cleanup failed: {error}");
                    } else {
                        panic!("probe database cleanup failed: {error}");
                    }
                }
            }
        }
        let _probe = ProbeDatabase {
            admin: ensure_test_database_exists(&future_url).map_err(anyhow::Error::msg)?,
            name: future_name,
        };
        (|| -> anyhow::Result<()> {
            let mut future = gobby_core::postgres::connect_readwrite(&future_url)?;
            future.batch_execute("CREATE EXTENSION IF NOT EXISTS pg_search; CREATE TABLE schema_migrations (version integer PRIMARY KEY)")?;
            future.execute(
                "INSERT INTO schema_migrations(version) VALUES ($1)",
                &[&(version + 1)],
            )?;
            let error = gobby_core::schema::SchemaRunner::new(&mut future, "public")?
                .apply()
                .expect_err("future schema refuses this older runner");
            assert!(
                error.to_string().contains("newer than this runner"),
                "{error}"
            );
            temp_env::with_var("DATABASE_URL", Some(source.as_str()), || {
                let selected = postgres_test_database_url("schema cohort regression");
                assert_eq!(
                    selected
                        .parse::<postgres::Config>()
                        .expect("cohort DSN")
                        .get_dbname(),
                    Some(schema_test_database_name(version, &identity.root_hash).as_str())
                );
                assert_eq!(postgres_test_database_url("same process"), selected);
                let mut current = gobby_core::postgres::connect_readwrite(&selected)
                    .expect("compiled cohort connection");
                gobby_core::schema::SchemaRunner::new(&mut current, "public")
                    .expect("compiled runner")
                    .apply()
                    .expect("compiled schema remains applicable");
            });
            Ok(())
        })()
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn destructive_postgres_guard_accepts_explicit_override_values() {
        for value in ["1", "true", "TRUE"] {
            temp_env::with_var(
                GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
                Some(value),
                || {
                    assert!(
                        destructive_postgres_test_allowed("postgresql://localhost/gcode").is_ok()
                    );
                },
            );
        }
        temp_env::with_var(GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV, Some("0"), || {
            assert!(destructive_postgres_test_allowed("postgresql://localhost/gcode").is_err());
        });
    }
}

// All library tests share log's single global logger. Capture per thread so
// concurrently running tests cannot clear or assert on each other's records.
#[cfg(test)]
mod logs {
    use std::cell::RefCell;

    thread_local! {
        static RECORDS: RefCell<Vec<(log::Level, String)>> = const { RefCell::new(Vec::new()) };
    }

    struct CaptureLogger;

    impl log::Log for CaptureLogger {
        fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
            metadata.level() <= log::Level::Debug
        }

        fn log(&self, record: &log::Record<'_>) {
            if self.enabled(record.metadata()) {
                let _ = RECORDS.try_with(|records| {
                    records
                        .borrow_mut()
                        .push((record.level(), record.args().to_string()));
                });
            }
        }

        fn flush(&self) {}
    }

    pub(super) fn clear() {
        static INSTALL: std::sync::Once = std::sync::Once::new();
        INSTALL.call_once(|| {
            static LOGGER: CaptureLogger = CaptureLogger;
            log::set_logger(&LOGGER).expect("install shared library-test logger");
            log::set_max_level(log::LevelFilter::Debug);
        });
        RECORDS.with(|records| records.borrow_mut().clear());
    }

    pub(super) fn captured(level: log::Level) -> Vec<String> {
        RECORDS.with(|records| {
            records
                .borrow()
                .iter()
                .filter(|(record_level, _)| *record_level <= level)
                .map(|(_, message)| message.clone())
                .collect()
        })
    }
}

#[cfg(test)]
pub(crate) fn clear_captured_logs() {
    logs::clear();
}

#[cfg(test)]
pub(crate) fn captured_logs(level: log::Level) -> Vec<String> {
    logs::captured(level)
}
