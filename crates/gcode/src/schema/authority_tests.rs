use super::*;
use gobby_core::grant::GrantError;
use postgres::NoTls;

fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// Drops the login role created for one test. Runs on a fresh owner connection
/// because the restricted client must already be closed by the time it runs.
struct LoginRoleCleanup {
    database_url: String,
    role: String,
}

impl Drop for LoginRoleCleanup {
    fn drop(&mut self) {
        let mut owner = match gobby_core::postgres::connect_readwrite(&self.database_url) {
            Ok(owner) => owner,
            Err(err) => {
                eprintln!(
                    "LoginRoleCleanup: connect failed for role {}: {err}",
                    self.role
                );
                return;
            }
        };
        let role = quote_ident(&self.role);
        if let Err(err) = owner.batch_execute(&format!("DROP OWNED BY {role}; DROP ROLE {role};")) {
            eprintln!(
                "LoginRoleCleanup: dropping role {} failed: {err}",
                self.role
            );
        }
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn credential_without_table_privileges_reports_refresh_not_migration_debt() {
    let database_url = crate::test_env::postgres_test_database_url("schema authority tests");
    let mut owner = gobby_core::postgres::connect_readwrite(&database_url).expect("connect owner");

    // A login role with CONNECT and schema USAGE but no table grants: its
    // credential is valid, but information_schema hides every code-index
    // column from it.
    let role = format!("gcode_authority_{}", uuid::Uuid::new_v4().simple());
    let password = uuid::Uuid::new_v4().simple().to_string();
    let schema = current_schema_name(&mut owner).expect("resolve current schema");
    let database: String = owner
        .query_one("SELECT current_database()", &[])
        .expect("resolve current database")
        .get(0);
    owner
        .batch_execute(&format!(
            "CREATE ROLE {role} LOGIN PASSWORD '{password}';
             GRANT CONNECT ON DATABASE {database} TO {role};
             GRANT USAGE ON SCHEMA {schema} TO {role};",
            role = quote_ident(&role),
            database = quote_ident(&database),
            schema = quote_ident(&schema),
        ))
        .expect("create login role without table privileges");
    let _cleanup = LoginRoleCleanup {
        database_url: database_url.clone(),
        role: role.clone(),
    };

    let mut config: postgres::Config = database_url.parse().expect("validated test DSN");
    config.user(&role).password(&password);
    let mut restricted = config
        .connect(NoTls)
        .expect("connect as role without table privileges");

    let err = validate_runtime_schema(&mut restricted)
        .expect_err("a role without table privileges must not pass schema validation");
    assert!(
        !format!("{err:#}").contains("missing required code-index columns"),
        "missing table privileges were reported as migration debt: {err:#}"
    );
    assert!(
        matches!(
            err.downcast_ref::<GrantError>(),
            Some(GrantError::Expired | GrantError::Revoked)
        ),
        "expected a credential refresh condition (GrantError::Expired or Revoked), got: {err:#}"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn fully_authorized_missing_column_still_reports_migration_debt() {
    let database_url = crate::test_env::postgres_test_database_url("schema authority tests");
    let mut owner = gobby_core::postgres::connect_readwrite(&database_url).expect("connect owner");

    // Rollback-only: the owner holds every privilege, so the only defect the
    // validator may report here is the genuinely absent column.
    owner
        .batch_execute("BEGIN; ALTER TABLE code_indexed_projects DROP COLUMN created_at;")
        .expect("drop column inside a rollback-only transaction");
    let result = validate_runtime_schema(&mut owner);
    owner
        .batch_execute("ROLLBACK")
        .expect("roll back the column drop");

    let err = result.expect_err("a genuinely missing column must fail schema validation");
    let message = format!("{err:#}");
    assert!(
        message.contains("missing required code-index columns: code_indexed_projects.created_at"),
        "missing column was not reported as migration debt: {message}"
    );
    assert!(
        message.contains(MIGRATION_HINT),
        "migration debt must carry the migration hint: {message}"
    );
    assert!(
        err.downcast_ref::<GrantError>().is_none(),
        "a fully authorized role must not get a credential refresh condition: {message}"
    );
}

/// Runs a restoring statement on drop, so a panicking test leaves the shared schema
/// as it found it.
struct RestoreOnDrop {
    database_url: String,
    sql: String,
}

impl Drop for RestoreOnDrop {
    fn drop(&mut self) {
        match gobby_core::postgres::connect_readwrite(&self.database_url) {
            Ok(mut owner) => {
                if let Err(err) = owner.batch_execute(&self.sql) {
                    eprintln!("RestoreOnDrop: `{}` failed: {err}", self.sql);
                }
            }
            Err(err) => eprintln!("RestoreOnDrop: connect failed for `{}`: {err}", self.sql),
        }
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn nologin_open_connection_with_missing_column_reports_refresh_not_migration_debt() {
    let database_url = crate::test_env::postgres_test_database_url("schema authority tests");
    let mut owner = gobby_core::postgres::connect_readwrite(&database_url).expect("connect owner");

    // A login role with CONNECT, schema USAGE, and SELECT on every inspected table.
    // Its connection stays open after the owner revokes login, so the open session
    // is the only place the revoked authority can be observed.
    let role = format!("gcode_authority_{}", uuid::Uuid::new_v4().simple());
    let password = uuid::Uuid::new_v4().simple().to_string();
    let schema = current_schema_name(&mut owner).expect("resolve current schema");
    let database: String = owner
        .query_one("SELECT current_database()", &[])
        .expect("resolve current database")
        .get(0);
    let table_grants: String = TABLE_CONTRACTS
        .iter()
        .map(|contract| {
            format!(
                "GRANT SELECT ON {schema}.{table} TO {role};\n",
                schema = quote_ident(&schema),
                table = quote_ident(contract.name),
                role = quote_ident(&role),
            )
        })
        .collect();
    owner
        .batch_execute(&format!(
            "CREATE ROLE {role} LOGIN PASSWORD '{password}';
             GRANT CONNECT ON DATABASE {database} TO {role};
             GRANT USAGE ON SCHEMA {schema} TO {role};
             {table_grants}",
            role = quote_ident(&role),
            database = quote_ident(&database),
            schema = quote_ident(&schema),
        ))
        .expect("create fully granted login role");
    let _cleanup = LoginRoleCleanup {
        database_url: database_url.clone(),
        role: role.clone(),
    };

    let mut config: postgres::Config = database_url.parse().expect("validated test DSN");
    config.user(&role).password(&password);
    let mut restricted = config
        .connect(NoTls)
        .expect("connect as fully granted login role");
    validate_runtime_schema(&mut restricted)
        .expect("a fully granted login role passes validation before its login is revoked");

    // Rename rather than drop: the restore puts back the exact column definition.
    let table = format!(
        "{}.{}",
        quote_ident(&schema),
        quote_ident("code_indexed_projects")
    );
    let _restore = RestoreOnDrop {
        database_url: database_url.clone(),
        sql: format!(
            "ALTER TABLE {table} RENAME COLUMN {probe} TO {column}",
            probe = quote_ident("created_at_authority_probe"),
            column = quote_ident("created_at"),
        ),
    };
    owner
        .batch_execute(&format!(
            "ALTER TABLE {table} RENAME COLUMN {column} TO {probe};
             ALTER ROLE {role} NOLOGIN;",
            column = quote_ident("created_at"),
            probe = quote_ident("created_at_authority_probe"),
            role = quote_ident(&role),
        ))
        .expect("hide created_at and revoke login on the open role");

    let err = validate_runtime_schema(&mut restricted)
        .expect_err("a NOLOGIN role on an open connection must not pass schema validation");
    let message = format!("{err:#}");
    assert!(
        !message.contains("missing required code-index columns"),
        "a revoked login was reported as migration debt: {message}"
    );
    assert!(
        matches!(err.downcast_ref::<GrantError>(), Some(GrantError::Expired)),
        "expected a credential refresh condition (GrantError::Expired), got: {message}"
    );
}
