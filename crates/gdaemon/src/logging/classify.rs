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

/// The surface of `target`, by whole-segment prefix, first match wins. A target outside
/// the `gobby` namespace has none and is not recorded.
pub fn surface_for_target(_target: &str) -> Option<Surface> {
    Some(Surface::Daemon)
}

/// The CLI name of a parser target, when it is a plain file name (`[a-z0-9_-]+`).
pub fn parser_cli(_target: &str) -> Option<&str> {
    None
}

/// The file a primary surface writes.
pub fn primary_file_name(_surface: Surface) -> &'static str {
    "daemon.log"
}
