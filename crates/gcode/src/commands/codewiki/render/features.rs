use std::fmt::Write as _;

use super::super::*;

/// Render the deterministic feature catalog page (#888). The page is derived
/// purely from the pinned CLI contract JSONs plus a curated dispatch resolver —
/// no LLM, no graph, no network — so it carries no source-span provenance and
/// never marks itself degraded. One `##` section per binary, one table row per
/// contract subcommand, each linking to the handler file's codewiki page as the
/// explaining doc.
pub(crate) fn render_feature_catalog_doc(doc: &FeatureCatalogDoc) -> String {
    let mut out = frontmatter_with_degradation_without_ranges(
        "Feature Catalog",
        "code_features",
        &[],
        &doc.degraded_sources,
    );
    out.push_str("# Feature Catalog\n\n");
    out.push_str(
        "This page is derived deterministically from the pinned CLI contracts \
         (`crates/<binary>/contract/<binary>.contract.json`) plus the binaries' dispatch \
         wiring — no LLM. It enumerates every subcommand each binary exposes: what it does, its \
         key contract flags, the handler entry symbol, and a link to the source page that \
         explains it. The command set per binary is exactly the contract's command set, so the \
         catalog cannot drift from the real CLI surface.\n\n",
    );

    for section in &doc.sections {
        let _ = writeln!(out, "## {}\n", section.binary);
        write_markdown_table_header(
            &mut out,
            &["Command", "What it does", "Key flags", "Entry", "Docs"],
        );
        for entry in &section.entries {
            let command = inline_code(&entry.command);
            let summary = if entry.summary.is_empty() {
                "—".to_string()
            } else {
                entry.summary.clone()
            };
            let key_flags = if entry.key_flags.is_empty() {
                "—".to_string()
            } else {
                entry
                    .key_flags
                    .iter()
                    .map(|flag| inline_code(flag))
                    .collect::<Vec<_>>()
                    .join(" ")
            };
            let docs = if entry.handler_file.is_empty() {
                "—".to_string()
            } else {
                file_wikilink(&entry.handler_file)
            };
            write_markdown_table_row(
                &mut out,
                [
                    command,
                    summary,
                    key_flags,
                    inline_code(&entry.entry_symbol),
                    docs,
                ],
            );
        }
        out.push('\n');
    }

    render_ghook_section(&mut out);

    out
}

/// ghook has no contract JSON and no subcommands — it is a flag-driven hook
/// dispatcher — so it gets a prose subsection describing its modes plus a single
/// wikilink to its entry file's codewiki page, rather than command rows.
fn render_ghook_section(out: &mut String) {
    out.push_str("## ghook\n\n");
    out.push_str(
        "`ghook` has no subcommand contract: it is a flag-driven hook dispatcher with a single \
         entry point, so it is listed here for completeness without command rows. Its modes are:\n\n",
    );
    out.push_str(
        "- `ghook --gobby-owned --cli=<c> --type=<t> [--detach]` — build a hook envelope from \
         stdin, enqueue it to `~/.gobby/hooks/inbox/`, then best-effort POST to the daemon \
         (enqueue-first). `--detach` runs the dispatch in the background.\n",
    );
    out.push_str(
        "- `ghook --diagnose` — print JSON diagnostics with no network call and no envelope \
         write.\n",
    );
    out.push_str(
        "- `ghook --version` — print the version and write `~/.gobby/bin/.ghook-runtime.json`.\n\n",
    );
    let _ = writeln!(
        out,
        "**Docs:** {}\n",
        file_wikilink("crates/ghook/src/main.rs")
    );
}
