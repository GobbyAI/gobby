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
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE pipeline_definitions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE rule_definitions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE session_variable_defaults",
];

/// Statement prefixes this hop is allowed to drop from the predecessor fixture
/// (or from the current baseline, for `legacy_copy_ledger`, which this hop
/// both added and then removed).
#[allow(dead_code)]
pub(crate) const REMOVED_STATEMENT_PREFIXES: &[&str] = &[
    "CREATE TABLE workflow_definitions",
    "CREATE TABLE workflow_instances",
    "CREATE TABLE IF NOT EXISTS legacy_copy_ledger",
    "ALTER TABLE ONLY workflow_definitions\n    ADD CONSTRAINT idx_wf_defs_name_project",
    "ALTER TABLE ONLY workflow_definitions\n    ADD CONSTRAINT workflow_definitions_pkey",
    "ALTER TABLE ONLY workflow_definitions\n    ADD CONSTRAINT workflow_definitions_project_id_fkey",
    "ALTER TABLE ONLY workflow_instances\n    ADD CONSTRAINT workflow_instances_pkey",
    "ALTER TABLE ONLY workflow_instances\n    ADD CONSTRAINT workflow_instances_session_id_workflow_name_key",
    "ALTER TABLE ONLY workflow_instances\n    ADD CONSTRAINT workflow_instances_session_id_fkey",
    "CREATE INDEX idx_wf_defs_enabled",
    "CREATE INDEX idx_wf_defs_name",
    "CREATE INDEX idx_wf_defs_project",
    "CREATE INDEX idx_wf_defs_type",
    "CREATE INDEX idx_workflow_instances_enabled",
    "CREATE INDEX idx_workflow_instances_session",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE workflow_definitions",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE workflow_instances",
    "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE legacy_copy_ledger",
];

pub(crate) fn baseline_refresh_statement(statement: &str) -> bool {
    let body = statement_body(statement);
    REFRESH_STATEMENT_PREFIXES
        .iter()
        .any(|prefix| body.starts_with(prefix))
}

#[allow(dead_code)]
pub(crate) fn baseline_removed_statement(statement: &str) -> bool {
    let body = statement_body(statement);
    REMOVED_STATEMENT_PREFIXES
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
