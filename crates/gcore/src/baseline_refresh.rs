//! Predecessor-baseline refresh acceptance for the domain-table hop.

/// Statement prefixes that the predecessor refresh hop may apply.
///
/// The set-difference tripwire in `runner_tests` requires this list to name
/// exactly the statements added to `baseline.sql` since the predecessor fixture.
pub(crate) const REFRESH_STATEMENT_PREFIXES: &[&str] = &[
    "CREATE TABLE IF NOT EXISTS agent_definitions",
    "CREATE INDEX IF NOT EXISTS idx_agent_defs_project",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_defs_live_name",
    "CREATE TABLE IF NOT EXISTS agent_step_workflows",
    "CREATE TABLE IF NOT EXISTS agent_step_instances",
    "CREATE INDEX IF NOT EXISTS idx_asi_step_workflow",
    "CREATE TABLE IF NOT EXISTS definition_revisions",
    "CREATE TABLE IF NOT EXISTS legacy_copy_ledger",
    "CREATE TABLE IF NOT EXISTS pipeline_definitions",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_defs_project",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_defs_live_name",
    "CREATE TABLE IF NOT EXISTS rule_definitions",
    "CREATE INDEX IF NOT EXISTS idx_rule_defs_project",
    "CREATE INDEX IF NOT EXISTS idx_rule_defs_event",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_defs_live_name",
    "CREATE TABLE IF NOT EXISTS session_variable_defaults",
    "CREATE INDEX IF NOT EXISTS idx_session_var_defs_project",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_session_var_defs_live_name",
    "ALTER TABLE ONLY agent_definitions",
    "ALTER TABLE ONLY agent_step_instances",
    "ALTER TABLE ONLY pipeline_definitions",
    "ALTER TABLE ONLY rule_definitions",
    "ALTER TABLE ONLY session_variable_defaults",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE agent_definitions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE agent_step_instances",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE agent_step_workflows",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE definition_revisions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE legacy_copy_ledger",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE pipeline_definitions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE rule_definitions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE session_variable_defaults",
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
