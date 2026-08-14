use gobby_core::config::AiRouting;

use crate::explainer::{ExplainerGenerator, ExplainerPrompt};
use crate::support::scope::resolve_selection_context;
use crate::support::services;
use crate::support::time::collect_timestamp;
use crate::{CommandOutcome, ScopeSelection, UpkeepOptions, WikiError, session, upkeep};

use super::generation_routes::{
    ai_notice_label, resolve_ai_selection, resolve_explainer_transport,
    resolve_tool_loop_generator, routing_label,
};

const COMMAND: &str = "gwiki upkeep";
const DAEMON_AGENTIC_CALLER: &str = "gwiki.upkeep";

pub(crate) fn execute(
    selection: ScopeSelection,
    options: UpkeepOptions,
    ai: AiRouting,
) -> Result<CommandOutcome, WikiError> {
    let context = resolve_selection_context(&selection)?;
    let research_scope = session::ResearchScope::from(&context.scope);
    let vault_root = research_scope.root().to_path_buf();
    let timestamp = collect_timestamp()?;
    let ai_selection = resolve_ai_selection(ai);

    let mut notes: Vec<String> = Vec::new();
    // A configured-but-unreachable hub only degrades the near-duplicate layer;
    // the drain itself is vault-file based and keeps running.
    let runtime_services = match services::probe_runtime_services(COMMAND) {
        Ok(services) => Some(services),
        Err(error) => {
            notes.push(format!("runtime service probe failed: {error}"));
            None
        }
    };
    let mut semantic_backend = runtime_services
        .as_ref()
        .and_then(services::RuntimeServices::semantic_backend);
    let probe = semantic_backend
        .as_mut()
        .map(|backend| upkeep::SemanticProbe {
            backend,
            search_scope: context.search_scope.clone(),
        });

    let mut lib_options = upkeep::Options {
        max_pages: options.max_pages,
        min_mentions: options.min_mentions,
        max_sources_per_page: options.max_sources_per_page,
        dry_run: options.dry_run,
        daemon_synthesis_available: matches!(ai_selection.route, AiRouting::Daemon),
        hard_fail_on_generation_failure: false,
        archive_after_days: upkeep::DEFAULT_ARCHIVE_AFTER_DAYS,
        time_budget_seconds: options.time_budget_seconds,
    };

    // Dry runs never generate, so report the selected transport as inactive.
    let (mut report, ai_payload) = if options.dry_run {
        let report = upkeep::run(
            research_scope,
            context.output_scope.clone(),
            &lib_options,
            probe,
            None,
            &timestamp,
        )?;
        (
            report,
            serde_json::json!({
                "requested_mode": routing_label(ai),
                "lane": "none",
                "route": "off",
                "selection_reason": "dry_run",
                "notice": Option::<&str>::None,
            }),
        )
    } else if let Some(mut tool_loop) = resolve_tool_loop_generator(
        ai_selection.route,
        DAEMON_AGENTIC_CALLER,
        &selection,
        vault_root,
        context.scope.project_root().map(|root| root.to_path_buf()),
        context.output_scope.clone(),
        COMMAND,
    ) {
        // Tool loop resolved: generation failures fail the cluster
        // instead of writing a skeleton page.
        lib_options.hard_fail_on_generation_failure = true;
        let info = tool_loop.info;
        let report = upkeep::run(
            research_scope,
            context.output_scope.clone(),
            &lib_options,
            probe,
            Some(tool_loop.generator.as_mut()),
            &timestamp,
        )?;
        (
            report,
            serde_json::json!({
                "requested_mode": routing_label(ai),
                "lane": "tool_loop",
                "route": info.route_label,
                "selection_reason": ai_selection.selection_reason,
                "notice": info.notice.map(ai_notice_label),
            }),
        )
    } else {
        let transport = resolve_explainer_transport(ai_selection.route, COMMAND);
        let route_label = transport.route_label();
        let notice = transport.notice_kind();
        let mut generate = |prompt: &ExplainerPrompt| transport.generate(prompt);
        let generator: Option<ExplainerGenerator<'_>> = if transport.is_active() {
            Some(&mut generate)
        } else {
            None
        };
        let report = upkeep::run(
            research_scope,
            context.output_scope.clone(),
            &lib_options,
            probe,
            generator,
            &timestamp,
        )?;
        (
            report,
            serde_json::json!({
                "requested_mode": routing_label(ai),
                "lane": "one_shot",
                "route": route_label,
                "selection_reason": ai_selection.selection_reason,
                "notice": notice.map(ai_notice_label),
            }),
        )
    };
    report.notes.splice(0..0, notes);

    let mut payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize upkeep report",
        path: None,
        source: error,
    })?;
    if let Some(object) = payload.as_object_mut() {
        object.insert("ai".to_string(), ai_payload);
    }
    let text = upkeep::render_text(&report);
    Ok(super::scoped_outcome(
        "upkeep",
        &context.output_scope,
        payload,
        text,
    ))
}

#[cfg(test)]
mod tests {
    use super::DAEMON_AGENTIC_CALLER;

    #[test]
    fn daemon_agentic_caller_is_stable() {
        assert_eq!(DAEMON_AGENTIC_CALLER, "gwiki.upkeep");
    }
}
