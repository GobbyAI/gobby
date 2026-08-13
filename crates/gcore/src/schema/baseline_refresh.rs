//! Predecessor-baseline refresh acceptance for the #19645 → 1.1 hop.

/// Statement prefixes the predecessor refresh hop may apply.
pub(crate) const REFRESH_STATEMENT_PREFIXES: &[&str] = &[
    "CREATE TABLE IF NOT EXISTS deployment_runtime",
    "GRANT SELECT,INSERT,UPDATE ON TABLE deployment_runtime",
    "REVOKE ALL ON TABLE deployment_runtime FROM PUBLIC",
    "ALTER TABLE gobby_agent_auth.principal_bindings DROP CONSTRAINT",
    "ALTER TABLE gobby_agent_auth.principal_bindings ADD CONSTRAINT",
    "ALTER TABLE gobby_agent_auth.principal_bindings ADD COLUMN",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_interactive_principal_active",
    "CREATE TABLE IF NOT EXISTS gobby_agent_auth.interactive_credential_material",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "ALTER TABLE gobby_agent_auth.interactive_credential_material OWNER TO gobby_agent_issuer",
    "REVOKE ALL ON TABLE gobby_agent_auth.interactive_credential_material FROM PUBLIC",
    "ALTER FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "ALTER FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
];

pub(crate) fn baseline_refresh_statement(statement: &str) -> bool {
    let body = statement_body(statement);
    REFRESH_STATEMENT_PREFIXES
        .iter()
        .any(|prefix| body.starts_with(prefix))
}

fn statement_body(mut statement: &str) -> &str {
    loop {
        statement = statement.trim_start();
        if let Some(comment) = statement.strip_prefix("--") {
            statement = comment
                .find('\n')
                .map_or("", |newline| &comment[newline + 1..]);
        } else if let Some(comment) = statement.strip_prefix("/*") {
            statement = comment.find("*/").map_or("", |end| &comment[end + 2..]);
        } else {
            return statement;
        }
    }
}
