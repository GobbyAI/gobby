//! Predecessor-baseline refresh acceptance for typed-domain and runtime hops.

/// Common-parent predecessor (`b2e08b…`) plus the union of later lineage hops.
///
/// The set-difference tripwire in `runner_tests` requires this list to name
/// exactly the statements added to `baseline.sql` since the common-parent fixture.
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
    "CREATE TABLE IF NOT EXISTS deployment_runtime",
    "GRANT SELECT,INSERT,UPDATE ON TABLE deployment_runtime",
    "REVOKE ALL ON TABLE deployment_runtime FROM PUBLIC",
    "ALTER TABLE gobby_agent_auth.principal_bindings DROP CONSTRAINT",
    "ALTER TABLE gobby_agent_auth.principal_bindings ADD CONSTRAINT",
    "ALTER TABLE gobby_agent_auth.principal_bindings ADD COLUMN",
    "DROP INDEX IF EXISTS gobby_agent_auth.uq_interactive_principal_active",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_interactive_principal_active",
    "CREATE TABLE IF NOT EXISTS gobby_agent_auth.interactive_credential_material",
    "DROP FUNCTION IF EXISTS gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "ALTER TABLE gobby_agent_auth.interactive_credential_material OWNER TO gobby_agent_issuer",
    "REVOKE ALL ON TABLE gobby_agent_auth.interactive_credential_material FROM PUBLIC",
    "ALTER FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "ALTER FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "ALTER FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "ALTER FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "ALTER FUNCTION gobby_agent_auth.rotate_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.rotate_interactive_principal(",
];

pub(crate) const TYPED_DOMAIN_REFRESH_PREFIXES: &[&str] = &[
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

pub(crate) const RUNTIME_BOUNDARY_REFRESH_PREFIXES: &[&str] = &[
    "CREATE TABLE IF NOT EXISTS deployment_runtime",
    "GRANT SELECT,INSERT,UPDATE ON TABLE deployment_runtime",
    "REVOKE ALL ON TABLE deployment_runtime FROM PUBLIC",
    "ALTER TABLE gobby_agent_auth.principal_bindings DROP CONSTRAINT",
    "ALTER TABLE gobby_agent_auth.principal_bindings ADD CONSTRAINT",
    "ALTER TABLE gobby_agent_auth.principal_bindings ADD COLUMN",
    "DROP INDEX IF EXISTS gobby_agent_auth.uq_interactive_principal_active",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_interactive_principal_active",
    "CREATE TABLE IF NOT EXISTS gobby_agent_auth.interactive_credential_material",
    "DROP FUNCTION IF EXISTS gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.rotate_interactive_principal(",
    "CREATE OR REPLACE FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "ALTER TABLE gobby_agent_auth.interactive_credential_material OWNER TO gobby_agent_issuer",
    "REVOKE ALL ON TABLE gobby_agent_auth.interactive_credential_material FROM PUBLIC",
    "ALTER FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "ALTER FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "ALTER FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "ALTER FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "ALTER FUNCTION gobby_agent_auth.rotate_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "REVOKE ALL ON FUNCTION gobby_agent_auth.rotate_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue_or_reuse_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.replace_interactive_credential_material(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.load_interactive_credential_material(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.lookup_interactive_principal(",
    "GRANT EXECUTE ON FUNCTION gobby_agent_auth.rotate_interactive_principal(",
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RefreshMode {
    TypedDomainAndRuntime,
    RuntimeOnly,
    TypedDomainOnly,
}

pub(crate) fn baseline_refresh_statement(statement: &str) -> bool {
    statement_matches_prefixes(statement, REFRESH_STATEMENT_PREFIXES)
}

pub(crate) fn baseline_refresh_statement_for_mode(statement: &str, mode: RefreshMode) -> bool {
    match mode {
        RefreshMode::TypedDomainAndRuntime => baseline_refresh_statement(statement),
        RefreshMode::RuntimeOnly => {
            statement_matches_prefixes(statement, RUNTIME_BOUNDARY_REFRESH_PREFIXES)
        }
        RefreshMode::TypedDomainOnly => {
            statement_matches_prefixes(statement, TYPED_DOMAIN_REFRESH_PREFIXES)
        }
    }
}

#[allow(dead_code)]
pub(crate) fn baseline_removed_statement(statement: &str) -> bool {
    statement_matches_prefixes(statement, REMOVED_STATEMENT_PREFIXES)
}

fn statement_matches_prefixes(statement: &str, prefixes: &[&str]) -> bool {
    let body = statement_body(statement);
    prefixes.iter().any(|prefix| body.starts_with(prefix))
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
