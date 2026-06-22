use std::fmt::Write as _;

use crate::models::Symbol;

use super::super::{ProseRegister, RelationshipFacts};
use super::excerpts::{
    CONCEPT_PAGE_SOURCE_EXCERPTS, NARRATIVE_PAGE_SOURCE_EXCERPTS, VERIFY_SOURCE_EXCERPTS,
    append_source_excerpt_section, append_source_excerpt_section_n, bounded_excerpt,
    summary_excerpt,
};
use super::systems::{
    AGENT_REGISTER_GUIDANCE, MAINTAINER_REGISTER_GUIDANCE, NEWCOMER_REGISTER_GUIDANCE,
};
use super::tables::{
    append_child_summary_table, append_component_table, append_evidence_table,
    append_relationship_section, append_symbol_summary_table, append_table_guidance,
};
use super::types::{ChildSummary, PageEvidenceRow, SourceExcerpt, SymbolSummary};

/// Layer an audience register onto a base system prompt. `None` returns the base
/// prompt unchanged (a borrowed `Cow`) so default runs are byte-identical; a
/// register appends its voice guidance while leaving the base section and
/// grounding contract intact.
pub(crate) fn with_register(
    system: &str,
    register: Option<ProseRegister>,
) -> std::borrow::Cow<'_, str> {
    match register {
        None => std::borrow::Cow::Borrowed(system),
        Some(register) => {
            let guidance = match register {
                ProseRegister::Newcomer => NEWCOMER_REGISTER_GUIDANCE,
                ProseRegister::Maintainer => MAINTAINER_REGISTER_GUIDANCE,
                ProseRegister::Agent => AGENT_REGISTER_GUIDANCE,
            };
            std::borrow::Cow::Owned(format!("{system}\n\n{guidance}"))
        }
    }
}

pub fn symbol_prompt(symbol: &Symbol) -> String {
    let mut prompt = format!(
        "File: {}\nSymbol: {} [{}]\nLines: {}-{}",
        symbol.file_path, symbol.qualified_name, symbol.kind, symbol.line_start, symbol.line_end
    );
    if let Some(signature) = symbol
        .signature
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        prompt.push_str("\nSignature: ");
        prompt.push_str(signature);
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        prompt.push_str("\nExisting docs: ");
        prompt.push_str(docstring);
    }
    prompt
}

pub fn file_prompt(
    file: &str,
    symbols: &[SymbolSummary],
    sources: &[SourceExcerpt],
    relationships: &RelationshipFacts,
) -> String {
    let mut prompt = format!(
        "Write a narrative explainer page for this source file from its AST symbols, source excerpt, and cross-file relationships.\n\nFile: {file}\n\nSymbols:\n"
    );
    append_symbol_summary_table(&mut prompt, symbols);
    append_relationship_section(&mut prompt, relationships);
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

/// Prompt for files without indexed AST symbols (markdown, config, data):
/// the model derives a narrative page from the file's leading content instead
/// of an empty symbol list.
pub fn content_file_prompt(file: &str, source: &SourceExcerpt) -> String {
    let mut prompt = format!(
        "Write a narrative explainer page for this repository file from its leading content.\n\nFile: {file}\n"
    );
    append_source_excerpt_section(&mut prompt, std::slice::from_ref(source));
    prompt
}

pub fn module_prompt(
    module: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
    relationships: &RelationshipFacts,
) -> String {
    build_entity_prompt(
        "Write a documentation brief for this module from its file summaries, child module summaries, source excerpts, and cross-file relationships.",
        "Module",
        module,
        files,
        modules,
        components,
        sources,
        relationships,
    )
}

pub fn repo_prompt(
    modules: &[ChildSummary],
    files: &[ChildSummary],
    sources: &[SourceExcerpt],
) -> String {
    let mut prompt =
        "Write a repository overview brief from module summaries, root-file summaries, and source excerpts.\n\n"
            .to_string();
    append_table_guidance(&mut prompt);
    prompt.push_str("Modules:\n");
    if modules.is_empty() {
        prompt.push_str("- No modules.\n");
    } else {
        append_child_summary_table(&mut prompt, &["Module", "Summary"], modules);
    }
    prompt.push_str("\nRoot files:\n");
    if files.is_empty() {
        prompt.push_str("- No root files.\n");
    } else {
        append_child_summary_table(&mut prompt, &["File", "Summary"], files);
    }
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

pub fn architecture_prompt(
    subsystem: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
) -> String {
    build_entity_prompt(
        "Summarize this subsystem's responsibility for a repository architecture overview.",
        "Subsystem",
        subsystem,
        files,
        modules,
        components,
        sources,
        &RelationshipFacts::default(),
    )
}

/// Prompt for the architecture page's layered narrative: subsystem
/// responsibilities plus the cross-subsystem dependency edges.
pub fn architecture_narrative_prompt(
    subsystems: &[ChildSummary],
    edges: &[(String, String)],
) -> String {
    let mut prompt =
        "Write a layered architecture narrative for this repository from its subsystem responsibilities and dependency edges.\n\nSubsystems:\n"
            .to_string();
    if subsystems.is_empty() {
        prompt.push_str("- No subsystems.\n");
    } else {
        for subsystem in subsystems {
            let _ = writeln!(
                prompt,
                "- {}: {}",
                subsystem.name,
                summary_excerpt(&subsystem.summary)
            );
        }
    }
    prompt.push_str("\nDependency edges (source depends on target):\n");
    if edges.is_empty() {
        prompt.push_str("- No cross-subsystem dependency edges.\n");
    } else {
        for (source, target) in edges {
            let _ = writeln!(prompt, "- {source} -> {target}");
        }
    }
    prompt
}

/// Build the per-page content prompt for a curated concept page (reference
/// explainer voice; pair with [`super::CONCEPT_PAGE_SYSTEM`]).
pub fn concept_page_prompt(
    title: &str,
    summary: &str,
    members: &[PageEvidenceRow],
    symbols: &[PageEvidenceRow],
    sources: &[SourceExcerpt],
) -> String {
    build_curated_page_prompt(
        "Write a reference explainer page for this concept.",
        title,
        summary,
        members,
        symbols,
        sources,
        CONCEPT_PAGE_SOURCE_EXCERPTS,
    )
}

/// Build the per-page content prompt for a curated narrative chapter (guided
/// tutorial voice; pair with [`super::NARRATIVE_PAGE_SYSTEM`]).
pub fn narrative_page_prompt(
    title: &str,
    summary: &str,
    members: &[PageEvidenceRow],
    symbols: &[PageEvidenceRow],
    sources: &[SourceExcerpt],
) -> String {
    build_curated_page_prompt(
        "Write one guided-tour chapter for this part of the codebase.",
        title,
        summary,
        members,
        symbols,
        sources,
        NARRATIVE_PAGE_SOURCE_EXCERPTS,
    )
}

/// Build the verification prompt for a generated page: the draft prose split
/// into numbered blocks, followed by the evidence the auditor may rely on (pair
/// with [`super::VERIFY_SYSTEM`]). Symbol-bearing file pages include the same
/// table used for generation; content files with no indexed symbols stay
/// source-only. The block IDs here are the 1-based positions the verifier
/// returns and the caller strips, so numbering must stay in lockstep with the
/// caller's block split.
pub fn verify_prompt(
    blocks: &[String],
    symbols: &[SymbolSummary],
    sources: &[SourceExcerpt],
    relationships: &RelationshipFacts,
) -> String {
    let evidence = match (symbols.is_empty(), relationships.is_empty()) {
        (true, true) => "the source excerpts",
        (false, true) => "the Symbols table and source excerpts",
        (true, false) => "the cross-file relationships and source excerpts",
        (false, false) => "the Symbols table, cross-file relationships, and source excerpts",
    };
    let mut prompt =
        format!("Audit each numbered draft block against {evidence} below.\n\nDraft blocks:\n");
    for (index, block) in blocks.iter().enumerate() {
        let _ = writeln!(prompt, "[{}] {}", index + 1, bounded_excerpt(block.trim()));
    }
    if !symbols.is_empty() {
        prompt.push_str("\nSymbols:\n");
        append_symbol_summary_table(&mut prompt, symbols);
    }
    append_relationship_section(&mut prompt, relationships);
    append_source_excerpt_section_n(&mut prompt, sources, VERIFY_SOURCE_EXCERPTS);
    prompt
}

fn build_curated_page_prompt(
    header: &str,
    title: &str,
    summary: &str,
    members: &[PageEvidenceRow],
    symbols: &[PageEvidenceRow],
    sources: &[SourceExcerpt],
    excerpt_take: usize,
) -> String {
    let mut prompt = format!(
        "{header}\n\nPage: {title}\n\nWorking summary (expand into full sections; do not just restate it): {}\n\n",
        summary_excerpt(summary)
    );
    append_table_guidance(&mut prompt);
    prompt.push_str("Member modules and files (cite these file:line anchors):\n");
    if members.is_empty() {
        prompt.push_str("- No members.\n");
    } else {
        append_evidence_table(
            &mut prompt,
            &["Name", "Kind", "Evidence", "Summary"],
            members,
        );
    }
    prompt.push_str("\nKey symbols (cite these file:line anchors):\n");
    if symbols.is_empty() {
        prompt.push_str("- No indexed symbols.\n");
    } else {
        append_evidence_table(
            &mut prompt,
            &["Symbol", "Kind", "Evidence", "Purpose"],
            symbols,
        );
    }
    append_source_excerpt_section_n(&mut prompt, sources, excerpt_take);
    prompt
}

#[allow(clippy::too_many_arguments)]
fn build_entity_prompt(
    header: &str,
    entity_label: &str,
    entity: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
    relationships: &RelationshipFacts,
) -> String {
    let mut prompt = format!("{header}\n\n{entity_label}: {entity}\n\n");
    append_table_guidance(&mut prompt);
    prompt.push_str("Files:\n");
    append_child_summary_sections(&mut prompt, files, modules, components);
    append_relationship_section(&mut prompt, relationships);
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

fn append_child_summary_sections(
    prompt: &mut String,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
) {
    if files.is_empty() {
        prompt.push_str("- No direct files.\n");
    } else {
        append_child_summary_table(prompt, &["File", "Summary"], files);
    }
    prompt.push_str("\nChild modules:\n");
    if modules.is_empty() {
        prompt.push_str("- No child modules.\n");
    } else {
        append_child_summary_table(prompt, &["Module", "Summary"], modules);
    }
    prompt.push_str("\nStable component IDs:\n");
    if components.is_empty() {
        prompt.push_str("- No indexed components.\n");
    } else {
        append_component_table(prompt, components);
    }
}
