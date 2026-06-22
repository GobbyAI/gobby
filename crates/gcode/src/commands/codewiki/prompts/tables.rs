use super::super::{RelationshipFacts, write_markdown_table_header, write_markdown_table_row};
use super::excerpts::summary_excerpt;
use super::systems::ENUMERABLE_FACTS_GUIDANCE;
use super::types::{ChildSummary, PageEvidenceRow, SymbolSummary};

pub(super) fn append_child_summary_table(
    prompt: &mut String,
    headers: &[&str],
    children: &[ChildSummary],
) {
    write_markdown_table_header(prompt, headers);
    for child in children {
        write_markdown_table_row(
            prompt,
            [child.name.clone(), summary_excerpt(&child.summary)],
        );
    }
}

pub(super) fn append_component_table(prompt: &mut String, components: &[String]) {
    write_markdown_table_header(prompt, &["Component"]);
    for component in components {
        write_markdown_table_row(prompt, [component.clone()]);
    }
}

pub(super) fn append_table_guidance(prompt: &mut String) {
    prompt.push_str("Table guidance:\n");
    prompt.push_str(ENUMERABLE_FACTS_GUIDANCE);
    prompt.push_str("\n\n");
}

pub(super) fn append_evidence_table(
    prompt: &mut String,
    headers: &[&str; 4],
    rows: &[PageEvidenceRow],
) {
    write_markdown_table_header(prompt, headers);
    for row in rows {
        write_markdown_table_row(
            prompt,
            [
                row.name.clone(),
                row.kind.clone(),
                row.citation.clone(),
                summary_excerpt(&row.summary),
            ],
        );
    }
}

pub(super) fn append_symbol_summary_table(prompt: &mut String, symbols: &[SymbolSummary]) {
    if symbols.is_empty() {
        prompt.push_str("- No indexed symbols.\n");
        return;
    }

    write_markdown_table_header(
        prompt,
        &[
            "Symbol",
            "Kind",
            "Component",
            "Component ID",
            "Lines",
            "Purpose",
        ],
    );
    for symbol in symbols {
        write_markdown_table_row(
            prompt,
            [
                symbol.name.clone(),
                symbol.kind.clone(),
                symbol.component_label.clone(),
                symbol.component_id.clone(),
                format!("{}-{}", symbol.line_start, symbol.line_end),
                symbol.purpose.clone(),
            ],
        );
    }
}

/// Append the cross-file relationship evidence for a file page prompt or its
/// verification prompt. Each row carries a `[file:line]` anchor the narrative
/// must reproduce, so `## How it fits` can name concrete collaborators without
/// inventing locations and the verifier treats those relationships as
/// authoritative. Omitted entirely when the graph yielded no cross-file
/// relationships (or was unavailable), so the section never appears empty.
pub(super) fn append_relationship_section(prompt: &mut String, relationships: &RelationshipFacts) {
    if relationships.is_empty() {
        return;
    }
    prompt.push_str("\nCross-file relationships (cite these file:line anchors):\n");
    if !relationships.inbound_calls.is_empty() {
        prompt.push_str("Called by (external symbols that call into this file):\n");
        write_markdown_table_header(prompt, &["Caller", "Kind", "Calls", "Evidence"]);
        for relation in &relationships.inbound_calls {
            write_markdown_table_row(
                prompt,
                [
                    relation.other_name.clone(),
                    relation.other_kind.clone(),
                    relation.local_name.clone().unwrap_or_default(),
                    relation.citation(),
                ],
            );
        }
    }
    if !relationships.outbound_calls.is_empty() {
        prompt.push_str("Calls out to (external symbols this file calls):\n");
        write_markdown_table_header(prompt, &["Caller", "Callee", "Kind", "Evidence"]);
        for relation in &relationships.outbound_calls {
            write_markdown_table_row(
                prompt,
                [
                    relation.local_name.clone().unwrap_or_default(),
                    relation.other_name.clone(),
                    relation.other_kind.clone(),
                    relation.citation(),
                ],
            );
        }
    }
    if !relationships.imports.is_empty() {
        prompt.push_str("Imports (files this file depends on):\n");
        write_markdown_table_header(prompt, &["Imported file", "Evidence"]);
        for relation in &relationships.imports {
            write_markdown_table_row(prompt, [relation.other_name.clone(), relation.citation()]);
        }
    }
}
