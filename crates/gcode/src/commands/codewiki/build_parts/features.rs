use std::path::Path;

use serde::Deserialize;

use super::super::types::{FeatureBinarySection, FeatureCatalogDoc, FeatureEntry};

/// Maximum number of contract flag names listed per command, to keep each row
/// tight. Commands with more flags simply have the tail dropped — the catalog
/// is a navigational index, not the per-command flag reference.
const MAX_KEY_FLAGS: usize = 8;

/// The pinned CLI contract JSON, parsed only for the fields the catalog needs.
/// The contract carries no handler pointer, so the command-to-handler mapping
/// is resolved separately by a curated dispatch resolver keyed on `name`.
#[derive(Debug, Deserialize)]
struct Contract {
    #[serde(default)]
    commands: Vec<ContractCommand>,
}

#[derive(Debug, Deserialize)]
struct ContractCommand {
    name: String,
    #[serde(default)]
    summary: String,
    #[serde(default)]
    flags: Vec<ContractFlag>,
}

#[derive(Debug, Deserialize)]
struct ContractFlag {
    #[serde(default)]
    name: String,
}

/// A binary the catalog enumerates from a pinned contract JSON: its display
/// name, the workspace crate that owns it, and the contract file under that
/// crate's `contract/` directory.
struct BinaryContract {
    /// Display name used for the binary's `##` section heading.
    binary: &'static str,
    /// Workspace crate directory under `crates/` holding the contract.
    crate_dir: &'static str,
    /// Contract file name under `crates/<crate_dir>/contract/`.
    contract_file: &'static str,
}

const BINARIES: &[BinaryContract] = &[
    BinaryContract {
        binary: "gcode",
        crate_dir: "gcode",
        contract_file: "gcode.contract.json",
    },
    BinaryContract {
        binary: "gwiki",
        crate_dir: "gwiki",
        contract_file: "gwiki.contract.json",
    },
];

/// Resolve a contract command name to its (handler_file, entry_symbol) by
/// reading the binary's dispatch wiring. The contract command set is the
/// authority; this resolver must cover every command, and the coverage test
/// asserts the catalog's command set equals each contract's exactly, so a
/// command added to a contract without a resolver entry is caught.
///
/// `handler_file` is a repo-relative path verified to exist on disk by the
/// feature catalog test; an unmapped command yields an empty `handler_file`
/// (rendered without a Docs wikilink) so the gap stays visible rather than
/// panicking.
fn resolve_handler(binary: &str, command: &str) -> (&'static str, &'static str) {
    match binary {
        "gcode" => resolve_gcode_handler(command),
        "gwiki" => resolve_gwiki_handler(command),
        _ => ("", ""),
    }
}

/// gcode command -> (handler_file, entry_symbol), traced from
/// `crates/gcode/src/dispatch.rs` (`Command::*` arms) and the early-dispatch
/// block. Graph/vector subcommands resolve to their implementation file.
fn resolve_gcode_handler(command: &str) -> (&'static str, &'static str) {
    match command {
        "contract" => ("crates/gcode/src/contract.rs", "contract::contract"),
        "init" => ("crates/gcode/src/commands/init.rs", "commands::init::run"),
        "setup" => ("crates/gcode/src/commands/setup.rs", "commands::setup::run"),
        "index" => ("crates/gcode/src/commands/index.rs", "commands::index::run"),
        "status" => (
            "crates/gcode/src/commands/status/current.rs",
            "commands::status::run",
        ),
        "invalidate" => (
            "crates/gcode/src/commands/status/invalidate.rs",
            "commands::status::invalidate",
        ),
        "search" => (
            "crates/gcode/src/commands/search.rs",
            "commands::search::search",
        ),
        "search-symbol" => (
            "crates/gcode/src/commands/search.rs",
            "commands::search::search_symbol",
        ),
        "search-text" => (
            "crates/gcode/src/commands/search.rs",
            "commands::search::search_text",
        ),
        "search-content" => (
            "crates/gcode/src/commands/search.rs",
            "commands::search::search_content",
        ),
        "grep" => ("crates/gcode/src/commands/grep.rs", "commands::grep::run"),
        "outline" => (
            "crates/gcode/src/commands/symbols.rs",
            "commands::symbols::outline",
        ),
        "symbol" => (
            "crates/gcode/src/commands/symbols.rs",
            "commands::symbols::symbol",
        ),
        "symbol-at" => (
            "crates/gcode/src/commands/symbol_at.rs",
            "commands::symbol_at::run",
        ),
        "symbols" => (
            "crates/gcode/src/commands/symbols.rs",
            "commands::symbols::symbols",
        ),
        "kinds" => (
            "crates/gcode/src/commands/symbols.rs",
            "commands::symbols::kinds",
        ),
        "tree" => (
            "crates/gcode/src/commands/symbols.rs",
            "commands::symbols::tree",
        ),
        "callers" => (
            "crates/gcode/src/commands/graph/reads.rs",
            "commands::graph::callers",
        ),
        "usages" => (
            "crates/gcode/src/commands/graph/reads.rs",
            "commands::graph::usages",
        ),
        "imports" => (
            "crates/gcode/src/commands/graph/reads.rs",
            "commands::graph::imports",
        ),
        "path" => (
            "crates/gcode/src/commands/graph/reads.rs",
            "commands::graph::path",
        ),
        "blast-radius" => (
            "crates/gcode/src/commands/graph/reads.rs",
            "commands::graph::blast_radius",
        ),
        "codewiki" => (
            "crates/gcode/src/commands/codewiki/run.rs",
            "commands::codewiki::run",
        ),
        "graph sync-file" => (
            "crates/gcode/src/commands/graph/lifecycle.rs",
            "commands::graph::sync_file",
        ),
        "graph overview" => (
            "crates/gcode/src/commands/graph/payload.rs",
            "commands::graph::overview",
        ),
        "graph file" => (
            "crates/gcode/src/commands/graph/payload.rs",
            "commands::graph::file",
        ),
        "graph neighbors" => (
            "crates/gcode/src/commands/graph/payload.rs",
            "commands::graph::neighbors",
        ),
        "graph blast-radius" => (
            "crates/gcode/src/commands/graph/payload.rs",
            "commands::graph::graph_blast_radius",
        ),
        "graph clear" => (
            "crates/gcode/src/commands/graph/lifecycle.rs",
            "commands::graph::clear",
        ),
        "graph rebuild" => (
            "crates/gcode/src/commands/graph/lifecycle.rs",
            "commands::graph::rebuild",
        ),
        "graph cleanup-orphans" => (
            "crates/gcode/src/commands/graph/lifecycle.rs",
            "commands::graph::cleanup_orphans",
        ),
        "graph report" => (
            "crates/gcode/src/commands/graph/payload.rs",
            "commands::graph::report",
        ),
        "vector sync-file" => (
            "crates/gcode/src/commands/vector.rs",
            "commands::vector::sync_file",
        ),
        "vector clear" => (
            "crates/gcode/src/commands/vector.rs",
            "commands::vector::clear",
        ),
        "vector rebuild" => (
            "crates/gcode/src/commands/vector.rs",
            "commands::vector::rebuild",
        ),
        "vector cleanup-orphans" => (
            "crates/gcode/src/commands/vector.rs",
            "commands::vector::cleanup_orphans",
        ),
        "embeddings doctor" => (
            "crates/gcode/src/commands/embeddings_doctor.rs",
            "commands::embeddings_doctor::run",
        ),
        "repo-outline" => (
            "crates/gcode/src/commands/status/repo_outline.rs",
            "commands::status::repo_outline",
        ),
        "projects" => (
            "crates/gcode/src/commands/status/projects.rs",
            "commands::status::projects",
        ),
        "prune" => (
            "crates/gcode/src/commands/status/prune.rs",
            "commands::status::prune",
        ),
        _ => ("", ""),
    }
}

/// gwiki command -> (handler_file, entry_symbol), traced from
/// `crates/gwiki/src/commands/mod.rs` (`commands::run` dispatch) which maps each
/// `Command::*` to a `commands::<module>::execute*` handler.
fn resolve_gwiki_handler(command: &str) -> (&'static str, &'static str) {
    match command {
        "contract" => ("crates/gwiki/src/contract.rs", "contract::contract"),
        "index" => (
            "crates/gwiki/src/commands/index.rs",
            "commands::index::execute",
        ),
        "search" => (
            "crates/gwiki/src/commands/search.rs",
            "commands::search::execute",
        ),
        "ask" => ("crates/gwiki/src/commands/ask.rs", "commands::ask::execute"),
        "read" => (
            "crates/gwiki/src/commands/read.rs",
            "commands::read::execute",
        ),
        "refresh" => (
            "crates/gwiki/src/commands/refresh/mod.rs",
            "commands::refresh::execute",
        ),
        "ingest-file" => (
            "crates/gwiki/src/commands/index.rs",
            "commands::index::execute_ingest_file",
        ),
        "ingest-url" => (
            "crates/gwiki/src/commands/index.rs",
            "commands::index::execute_ingest_url",
        ),
        "sync-sessions" => (
            "crates/gwiki/src/commands/session_sync.rs",
            "commands::session_sync::execute",
        ),
        "collect" => (
            "crates/gwiki/src/commands/collect.rs",
            "commands::collect::execute",
        ),
        "compile" => (
            "crates/gwiki/src/commands/compile.rs",
            "commands::compile::execute",
        ),
        "audit" => (
            "crates/gwiki/src/commands/audit.rs",
            "commands::audit::execute",
        ),
        "graph" => (
            "crates/gwiki/src/commands/graph.rs",
            "commands::graph::execute",
        ),
        "graph-context" => (
            "crates/gwiki/src/commands/graph_context.rs",
            "commands::graph_context::execute",
        ),
        "health" => (
            "crates/gwiki/src/commands/health.rs",
            "commands::health::execute",
        ),
        "librarian" => (
            "crates/gwiki/src/commands/librarian.rs",
            "commands::librarian::execute",
        ),
        "review-report" => (
            "crates/gwiki/src/commands/review_report.rs",
            "commands::review_report::execute",
        ),
        "citation-quality" => (
            "crates/gwiki/src/commands/citation_quality.rs",
            "commands::citation_quality::execute",
        ),
        "sources" => (
            "crates/gwiki/src/commands/sources.rs",
            "commands::sources::execute",
        ),
        "backlinks" => (
            "crates/gwiki/src/commands/backlinks.rs",
            "commands::backlinks::execute",
        ),
        "status" => (
            "crates/gwiki/src/commands/status.rs",
            "commands::status::execute",
        ),
        "trust" => (
            "crates/gwiki/src/commands/trust.rs",
            "commands::trust::execute",
        ),
        "remove-source" => (
            "crates/gwiki/src/commands/sources.rs",
            "commands::sources::execute_remove",
        ),
        _ => ("", ""),
    }
}

/// Parse a binary's pinned contract JSON and project it into a catalog section.
/// Returns `None` (omitting the binary) when the contract file is missing or
/// unparseable — never panics, never degrades the page.
fn section_for_binary(
    repo_root: &Path,
    descriptor: &BinaryContract,
) -> Option<FeatureBinarySection> {
    let contract_path = repo_root
        .join("crates")
        .join(descriptor.crate_dir)
        .join("contract")
        .join(descriptor.contract_file);
    let raw = std::fs::read_to_string(&contract_path).ok()?;
    let contract: Contract = serde_json::from_str(&raw).ok()?;

    let entries = contract
        .commands
        .into_iter()
        .map(|command| {
            let (handler_file, entry_symbol) = resolve_handler(descriptor.binary, &command.name);
            let key_flags = command
                .flags
                .into_iter()
                .map(|flag| flag.name)
                .filter(|name| !name.is_empty())
                .take(MAX_KEY_FLAGS)
                .collect::<Vec<_>>();
            FeatureEntry {
                command: command.name,
                summary: command.summary,
                key_flags,
                entry_symbol: entry_symbol.to_string(),
                handler_file: handler_file.to_string(),
            }
        })
        .collect::<Vec<_>>();

    Some(FeatureBinarySection {
        binary: descriptor.binary.to_string(),
        entries,
    })
}

/// Build the deterministic feature catalog (#888) from the pinned CLI contract
/// JSONs plus the curated dispatch resolver. One section per binary whose
/// contract is found, in `BINARIES` order; each command becomes one row.
///
/// `input_files` is the indexed file set the catalog is built alongside; it is
/// not consulted directly (the contract JSONs are the command-set authority),
/// but the parameter keeps the call site symmetric with the other builders.
///
/// Returns `None` only when NEITHER contract is found; otherwise `Some`, even
/// if one binary's contract is missing (that binary's section is just omitted).
/// The page is non-degrading: `degraded_sources` is always empty.
pub(crate) fn build_feature_catalog_doc(
    repo_root: &Path,
    input_files: &[String],
) -> Option<FeatureCatalogDoc> {
    let _ = input_files;
    let sections = BINARIES
        .iter()
        .filter_map(|descriptor| section_for_binary(repo_root, descriptor))
        .collect::<Vec<_>>();

    if sections.is_empty() {
        return None;
    }

    Some(FeatureCatalogDoc {
        sections,
        degraded_sources: Vec::new(),
    })
}
