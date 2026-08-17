use gobby_core::config::AiRouting;

use crate::support::scope::resolve_selection_context;
use crate::support::services;
use crate::{CommandOutcome, ScopeSelection, WikiError, librarian, vault};

pub(crate) fn execute(
    selection: ScopeSelection,
    ai: AiRouting,
) -> Result<CommandOutcome, WikiError> {
    let context = resolve_selection_context(&selection)?;
    // librarian persists its proposals report under the vault root, so claim
    // the vault first — an unclaimed write poisons the dir as a non-vault
    // collision.
    vault::initialize(&context.scope)?;
    let runtime_services = services::probe_runtime_services("gwiki librarian")?;
    let model_available = services::text_generation_available("gwiki librarian", ai);
    let options = librarian::Options::probed(&runtime_services, model_available);
    let mut semantic_backend = runtime_services.semantic_backend();
    let probe = semantic_backend
        .as_mut()
        .map(|backend| librarian::SemanticProbe {
            backend,
            search_scope: context.search_scope.clone(),
        });

    let report = librarian::run(
        context.scope.root(),
        context.output_scope.clone(),
        options,
        probe,
    )?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize librarian proposals report",
        path: None,
        source: error,
    })?;
    Ok(super::scoped_outcome(
        "librarian",
        &context.output_scope,
        payload,
        librarian::render_text(&report),
    ))
}
