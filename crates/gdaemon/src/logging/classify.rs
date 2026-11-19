//! Which surface a record's target belongs to, as Python's logger namespaces decide it.

/// A routed surface. Each primary surface has one file; `Errors` aggregates WARNING and
/// above; `Parser` writes one file per CLI.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Surface {
    Daemon,
    Hooks,
    Llm,
    Mcp,
    Automation,
    AllowAudit,
    Parser,
    Errors,
}

/// The namespace whose records each CLI's parser log receives.
const PARSER_NAMESPACE: &str = "gobby.parser_error";

/// Namespace to surface, in precedence order (Python's `classify_log_surface`). The parser
/// namespace comes first because Python keeps parser records out of every primary file.
const TABLE: [(&str, Surface); 12] = [
    (PARSER_NAMESPACE, Surface::Parser),
    ("gobby.rule_allow_audit", Surface::AllowAudit),
    ("gobby.hooks", Surface::Hooks),
    ("gobby.ai.text_generation", Surface::Llm),
    ("gobby.mcp", Surface::Mcp),
    ("gobby.mcp_proxy", Surface::Mcp),
    ("gobby.servers.routes.mcp", Surface::Mcp),
    ("gobby.scheduler", Surface::Automation),
    ("gobby.system_automation", Surface::Automation),
    ("gobby.workflows.pipeline", Surface::Automation),
    ("gobby.workflows.pipeline_executor", Surface::Automation),
    ("gobby.workflows.pipeline_heartbeat", Surface::Automation),
];

/// Whether `target` is `namespace` or a dotted child of it.
fn in_namespace(target: &str, namespace: &str) -> bool {
    target
        .strip_prefix(namespace)
        .is_some_and(|rest| rest.is_empty() || rest.starts_with('.'))
}

/// The surface of `target`, by whole-segment prefix, first match wins. A target outside
/// the `gobby` namespace has none and is not recorded.
pub fn surface_for_target(target: &str) -> Option<Surface> {
    if !in_namespace(target, "gobby") {
        return None;
    }
    let surface = TABLE
        .iter()
        .find(|(namespace, _)| in_namespace(target, namespace))
        .map_or(Surface::Daemon, |(_, surface)| *surface);
    Some(surface)
}

/// The CLI name of a parser target, when it is a plain file name (`[a-z0-9_-]+`).
pub fn parser_cli(target: &str) -> Option<&str> {
    let cli = target.strip_prefix(PARSER_NAMESPACE)?.strip_prefix('.')?;
    let plain = !cli.is_empty()
        && cli
            .bytes()
            .all(|byte| matches!(byte, b'a'..=b'z' | b'0'..=b'9' | b'_' | b'-'));
    plain.then_some(cli)
}

/// The file a primary surface writes. `Errors` writes `errors.log`; a parser record's
/// file is named by its CLI, so `Parser` has no fixed file and maps to `daemon.log`, where
/// a parser record with an unusable CLI name goes.
pub fn primary_file_name(surface: Surface) -> &'static str {
    match surface {
        Surface::Daemon | Surface::Parser => "daemon.log",
        Surface::Hooks => "hooks.log",
        Surface::Llm => "llm.log",
        Surface::Mcp => "mcp.log",
        Surface::Automation => "automation.log",
        Surface::AllowAudit => "rule-allow-audit.jsonl",
        Surface::Errors => "errors.log",
    }
}

/// The `surface` label of `logging_records_total`, for the surfaces Python counts.
pub(crate) fn metric_label(surface: Surface) -> Option<&'static str> {
    match surface {
        Surface::Daemon => Some("daemon"),
        Surface::Hooks => Some("hooks"),
        Surface::Llm => Some("llm"),
        Surface::Mcp => Some("mcp"),
        Surface::Automation => Some("automation"),
        Surface::Parser => Some("parser"),
        Surface::AllowAudit | Surface::Errors => None,
    }
}
