use chrono::{NaiveDate, Utc};
use gobby_core::config::AiRouting;

use crate::explainer::{ExplainerGenerator, ExplainerPrompt};
use crate::support::scope::resolve_selection_context;
use crate::support::time::collect_timestamp;
use crate::{CommandOutcome, RecapOptions, ScopeSelection, WikiError, recap};

use super::generation_routes::{
    ai_notice_label, resolve_ai_selection, resolve_explainer_transport, routing_label,
};

const COMMAND: &str = "gwiki recap";

pub(crate) fn execute(
    selection: ScopeSelection,
    options: RecapOptions,
    ai: AiRouting,
) -> Result<CommandOutcome, WikiError> {
    let context = resolve_selection_context(&selection)?;
    let vault_root = context.scope.root().to_path_buf();
    let timestamp = collect_timestamp()?;
    let date = resolve_date(options.date.as_deref())?;
    let ai_selection = resolve_ai_selection(ai);

    // Recap is one bounded completion by design — never the tool loop —
    // so it survives local models that cannot drive an agent loop.
    let transport = resolve_explainer_transport(ai_selection.route, COMMAND);
    let route_label = transport.route_label();
    let notice = transport.notice_kind();
    let mut generate = |prompt: &ExplainerPrompt| transport.generate(prompt);
    let generator: Option<ExplainerGenerator<'_>> = if transport.is_active() {
        Some(&mut generate)
    } else {
        None
    };

    let report = recap::run(
        &vault_root,
        context.output_scope.clone(),
        date,
        generator,
        &timestamp,
    )?;

    let mut payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize recap report",
        path: None,
        source: error,
    })?;
    if let Some(object) = payload.as_object_mut() {
        object.insert(
            "ai".to_string(),
            serde_json::json!({
                "requested_mode": routing_label(ai_selection.requested),
                "lane": "one_shot",
                "route": route_label,
                "selection_reason": ai_selection.selection_reason,
                "notice": notice.map(ai_notice_label),
            }),
        );
    }
    let text = recap::render_text(&report);
    Ok(super::scoped_outcome(
        "recap",
        &context.output_scope,
        payload,
        text,
    ))
}

/// Parse `--date` or default to today (UTC).
fn resolve_date(requested: Option<&str>) -> Result<NaiveDate, WikiError> {
    match requested {
        None => Ok(Utc::now().date_naive()),
        Some(value) => {
            NaiveDate::parse_from_str(value, "%Y-%m-%d").map_err(|_| WikiError::Config {
                detail: format!("invalid --date `{value}`: expected YYYY-MM-DD"),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::resolve_date;

    #[test]
    fn resolve_date_parses_iso_dates_and_rejects_garbage() {
        let parsed = resolve_date(Some("2026-07-04")).expect("valid date");
        assert_eq!(parsed.format("%Y-%m-%d").to_string(), "2026-07-04");
        assert!(resolve_date(Some("07/04/2026")).is_err());
        assert!(resolve_date(Some("2026-13-01")).is_err());
        assert!(resolve_date(None).is_ok());
    }
}
