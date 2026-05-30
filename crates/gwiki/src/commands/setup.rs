use gobby_core::setup::{SetupContext, StandaloneSetup};
use serde_json::json;

use crate::error::setup_error_to_wiki_error;
use crate::support::env::database_url_from_env;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::support::text::postgres_object_kind;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, setup as wiki_setup};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let setup = wiki_setup::default_setup();
    let objects = setup
        .postgres_objects()
        .map_err(setup_error_to_wiki_error)?;
    let object_payloads = objects
        .iter()
        .map(|object| {
            json!({
                "name": object.name,
                "kind": postgres_object_kind(object.kind),
                "store": "postgres",
            })
        })
        .collect::<Vec<_>>();

    let (status, created, skipped, failed) = if let Some(database_url) = database_url_from_env() {
        let mut client =
            gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
                WikiError::Config {
                    detail: format!("failed to connect to PostgreSQL for gwiki setup: {error}"),
                }
            })?;
        let mut ctx = SetupContext {
            pg: Some(&mut client),
            falkor_config: None,
            qdrant_config: None,
            non_interactive: true,
        };
        let report = setup.create(&mut ctx).map_err(setup_error_to_wiki_error)?;
        let status = if report.failed.is_empty() {
            "created"
        } else {
            "failed"
        };
        (status, report.created, report.skipped, report.failed)
    } else {
        ("ready", Vec::new(), Vec::new(), Vec::new())
    };

    Ok(render(
        output_scope,
        status,
        object_payloads,
        created,
        skipped,
        failed,
        wiki_setup::SETUP_OWNERSHIP_NOTE,
    ))
}

fn render(
    scope: ScopeIdentity,
    status: &str,
    objects: Vec<serde_json::Value>,
    created: Vec<String>,
    skipped: Vec<String>,
    failed: Vec<(String, String)>,
    ownership: &str,
) -> CommandOutcome {
    let object_count = objects.len();
    let payload = json!({
        "command": "setup",
        "scope": scope,
        "status": status,
        "objects": objects,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "ownership": ownership,
    });
    let text = format!(
        "Setup {status}
Scope: {scope}
Objects: {object_count}"
    );
    super::scoped_outcome("setup", &scope, payload, text)
}
