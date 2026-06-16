use std::fmt::Write as _;

use crate::models::Symbol;

pub const SYMBOL_SYSTEM: &str = "You write concise API reference notes. Return one sentence describing the symbol's purpose. Do not include markdown fences.";
pub const FILE_SYSTEM: &str = "You write concise file-level code documentation. Return a short purpose summary grounded only in the supplied symbol summaries and source excerpt: what the file does and how its pieces work together. Do not include markdown fences.";
pub const CONTENT_FILE_SYSTEM: &str = "You write concise documentation for non-code repository files. Return a short purpose summary describing what the file contains and what it is for, grounded only in the supplied leading content. Do not include markdown fences.";
pub const MODULE_SYSTEM: &str = "You write module documentation briefs. Using only the supplied file summaries, child module summaries, and source excerpts, write two to three short paragraphs covering the module's responsibilities, its key flows, and how its files and submodules collaborate. Plain paragraphs only - no headings, no lists, no markdown fences. Cite supporting file:line spans that appear in the supplied input.";
pub const REPO_SYSTEM: &str = "You write repository overview briefs. Using only the supplied module summaries, root-file summaries, and source excerpts, write two to three short paragraphs covering what the system is, how the major pieces fit together, and where a reader should start. Plain paragraphs only - no headings, no lists, no markdown fences. Cite supporting file:line spans that appear in the supplied input.";
pub const ARCHITECTURE_SYSTEM: &str = "You write concise architecture documentation. Using only the supplied summaries and source excerpts, return one to two sentences naming the subsystem's responsibility and how it collaborates with the rest of the system. Do not include markdown fences.";
pub const ARCHITECTURE_NARRATIVE_SYSTEM: &str = "You write architecture overviews. Using only the supplied subsystem responsibilities and dependency edges, write two to three short paragraphs describing the system in layers: which subsystems sit at the foundation, which build on them, and how the layers interact. Plain paragraphs only - no headings, no lists, no markdown fences.";
pub const CURATED_NAVIGATION_SYSTEM: &str = "You design a curated navigation layer for grounded code documentation. Return strict JSON only. Name user-facing concept modules, organize them into a hierarchy, and create short narrative tour pages. Use only supplied module and file identifiers, and link into reference pages instead of duplicating source detail.";

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

pub fn file_prompt(file: &str, symbols: &[SymbolSummary], sources: &[SourceExcerpt]) -> String {
    let mut prompt =
        format!("Summarize this file once from its AST symbols.\n\nFile: {file}\n\nSymbols:\n");
    if symbols.is_empty() {
        prompt.push_str("- No indexed symbols.\n");
    } else {
        for symbol in symbols {
            let _ = writeln!(
                prompt,
                "- {} [{}] component {} ({}) lines {}-{}: {}",
                symbol.name,
                symbol.kind,
                symbol.component_label,
                symbol.component_id,
                symbol.line_start,
                symbol.line_end,
                symbol.purpose
            );
        }
    }
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

/// Prompt for files without indexed AST symbols (markdown, config, data):
/// the model derives a purpose from the file's leading content instead of
/// an empty symbol list.
pub fn content_file_prompt(file: &str, source: &SourceExcerpt) -> String {
    let mut prompt =
        format!("Summarize this repository file once from its leading content.\n\nFile: {file}\n");
    append_source_excerpt_section(&mut prompt, std::slice::from_ref(source));
    prompt
}

pub fn module_prompt(
    module: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
) -> String {
    build_entity_prompt(
        "Write a documentation brief for this module from its file summaries, child module summaries, and source excerpts.",
        "Module",
        module,
        files,
        modules,
        components,
        sources,
    )
}

pub fn repo_prompt(
    modules: &[ChildSummary],
    files: &[ChildSummary],
    sources: &[SourceExcerpt],
) -> String {
    let mut prompt =
        "Write a repository overview brief from module summaries, root-file summaries, and source excerpts.\n\nModules:\n"
            .to_string();
    if modules.is_empty() {
        prompt.push_str("- No modules.\n");
    } else {
        for module in modules {
            let _ = writeln!(
                prompt,
                "- {}: {}",
                module.name,
                summary_excerpt(&module.summary)
            );
        }
    }
    prompt.push_str("\nRoot files:\n");
    if files.is_empty() {
        prompt.push_str("- No root files.\n");
    } else {
        for file in files {
            let _ = writeln!(
                prompt,
                "- {}: {}",
                file.name,
                summary_excerpt(&file.summary)
            );
        }
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

#[allow(clippy::too_many_arguments)]
fn build_entity_prompt(
    header: &str,
    entity_label: &str,
    entity: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
) -> String {
    let mut prompt = format!("{header}\n\n{entity_label}: {entity}\n\nFiles:\n");
    append_child_summary_sections(&mut prompt, files, modules, components);
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
        for file in files {
            let _ = writeln!(
                prompt,
                "- {}: {}",
                file.name,
                summary_excerpt(&file.summary)
            );
        }
    }
    prompt.push_str("\nChild modules:\n");
    if modules.is_empty() {
        prompt.push_str("- No child modules.\n");
    } else {
        for module in modules {
            let _ = writeln!(
                prompt,
                "- {}: {}",
                module.name,
                summary_excerpt(&module.summary)
            );
        }
    }
    prompt.push_str("\nStable component IDs:\n");
    if components.is_empty() {
        prompt.push_str("- No indexed components.\n");
    } else {
        for component in components {
            let _ = writeln!(prompt, "- {component}");
        }
    }
}

fn append_source_excerpt_section(prompt: &mut String, sources: &[SourceExcerpt]) {
    prompt.push_str("\nSource excerpts:\n");
    if sources.is_empty() {
        prompt.push_str("- No source excerpts.\n");
        return;
    }
    for source in sources.iter().take(MAX_PROMPT_SOURCE_EXCERPTS) {
        let _ = writeln!(
            prompt,
            "--- {} (lines {}-{})",
            source.path, source.line_start, source.line_end
        );
        prompt.push_str(&bounded_excerpt(&source.excerpt));
        prompt.push('\n');
    }
}

/// Hard cap on one child summary embedded in an aggregate prompt. Aggregate
/// prompts roll up every child; unbounded summaries (citation walls, echoed
/// prompts recorded as summaries) once compounded up the module tree past
/// provider input limits (#698).
const CHILD_SUMMARY_EXCERPT_MAX_CHARS: usize = 2_000;

/// Hard cap on one retrieved source excerpt embedded in a prompt, and on how
/// many excerpts a single prompt may carry. Together with
/// [`CHILD_SUMMARY_EXCERPT_MAX_CHARS`] these keep aggregate prompts bounded
/// even though they now carry real source content.
pub(crate) const SOURCE_EXCERPT_MAX_CHARS: usize = 2_400;
pub(crate) const MAX_PROMPT_SOURCE_EXCERPTS: usize = 4;

/// First paragraph of a child summary, flattened to one line and hard-capped
/// at [`CHILD_SUMMARY_EXCERPT_MAX_CHARS`], so each prompt list entry stays one
/// bounded line regardless of what an earlier run recorded as the summary.
fn summary_excerpt(summary: &str) -> String {
    let paragraph = summary
        .trim()
        .split("\n\n")
        .next()
        .unwrap_or_default()
        .trim();
    let flattened = paragraph.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut excerpt = flattened;
    let cap = excerpt
        .char_indices()
        .nth(CHILD_SUMMARY_EXCERPT_MAX_CHARS)
        .map(|(index, _)| index);
    if let Some(cap) = cap {
        excerpt.truncate(cap);
        excerpt.push('…');
    }
    excerpt
}

/// Source excerpt text hard-capped at [`SOURCE_EXCERPT_MAX_CHARS`]; newlines
/// are preserved because excerpts carry real source content.
fn bounded_excerpt(excerpt: &str) -> String {
    let trimmed = excerpt.trim_end();
    let cap = trimmed
        .char_indices()
        .nth(SOURCE_EXCERPT_MAX_CHARS)
        .map(|(index, _)| index);
    match cap {
        Some(cap) => {
            let mut bounded = trimmed[..cap].to_string();
            bounded.push('…');
            bounded
        }
        None => trimmed.to_string(),
    }
}

#[derive(Debug, Clone)]
pub struct SymbolSummary {
    pub name: String,
    pub kind: String,
    pub component_id: String,
    pub component_label: String,
    pub line_start: usize,
    pub line_end: usize,
    pub purpose: String,
}

#[derive(Debug, Clone)]
pub struct ChildSummary {
    pub name: String,
    pub summary: String,
}

/// A retrieved span of real source content fed into an aggregate prompt
/// alongside child summaries.
#[derive(Debug, Clone)]
pub struct SourceExcerpt {
    pub path: String,
    pub line_start: usize,
    pub line_end: usize,
    pub excerpt: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn oversized_child(name: &str) -> ChildSummary {
        let citation_wall = (0..2_000)
            .map(|line| format!("[src/lib.rs:{line}]"))
            .collect::<Vec<_>>()
            .join("\n");
        ChildSummary {
            name: name.to_string(),
            summary: format!("One real sentence.\n{citation_wall}"),
        }
    }

    #[test]
    fn aggregate_prompts_bound_each_child_summary() {
        let children = (0..3)
            .map(|index| oversized_child(&format!("src/module_{index}")))
            .collect::<Vec<_>>();

        for prompt in [
            module_prompt("src", &children, &children, &[], &[]),
            architecture_prompt("src", &children, &children, &[], &[]),
            repo_prompt(&children, &children, &[]),
        ] {
            for line in prompt.lines().filter(|line| line.starts_with("- src/")) {
                assert!(
                    line.chars().count()
                        <= CHILD_SUMMARY_EXCERPT_MAX_CHARS + "- src/module_0: …".chars().count(),
                    "child summary line stays bounded: {} chars",
                    line.chars().count()
                );
                assert!(line.ends_with('…'), "oversized excerpt is marked truncated");
            }
        }
    }

    #[test]
    fn short_summaries_pass_through_untruncated() {
        let child = ChildSummary {
            name: "src/lib.rs".to_string(),
            summary: "Concise healthy summary.".to_string(),
        };
        let prompt = module_prompt("src", &[child], &[], &[], &[]);
        assert!(prompt.contains("- src/lib.rs: Concise healthy summary.\n"));
    }

    #[test]
    fn excerpt_flattens_multiline_summaries_to_one_line() {
        let child = ChildSummary {
            name: "src/lib.rs".to_string(),
            summary: "First line.\nSecond line of the same paragraph.".to_string(),
        };
        let prompt = module_prompt("src", &[child], &[], &[], &[]);
        assert!(prompt.contains("- src/lib.rs: First line. Second line of the same paragraph.\n"));
    }

    fn excerpt(path: &str, content: &str) -> SourceExcerpt {
        SourceExcerpt {
            path: path.to_string(),
            line_start: 1,
            line_end: 40,
            excerpt: content.to_string(),
        }
    }

    #[test]
    fn aggregate_prompts_embed_bounded_source_excerpts() {
        let oversized = "x".repeat(SOURCE_EXCERPT_MAX_CHARS * 3);
        let sources = (0..MAX_PROMPT_SOURCE_EXCERPTS + 3)
            .map(|index| excerpt(&format!("src/file_{index}.rs"), &oversized))
            .collect::<Vec<_>>();

        let prompt = module_prompt("src", &[], &[], &[], &sources);

        let headers = prompt
            .lines()
            .filter(|line| line.starts_with("--- src/file_"))
            .count();
        assert_eq!(
            headers, MAX_PROMPT_SOURCE_EXCERPTS,
            "excerpt count is capped"
        );
        assert!(prompt.contains("--- src/file_0.rs (lines 1-40)"));
        for chunk in prompt.split("--- ").skip(1) {
            assert!(
                chunk.chars().count() <= SOURCE_EXCERPT_MAX_CHARS + 120,
                "each excerpt block stays bounded: {} chars",
                chunk.chars().count()
            );
        }
    }

    #[test]
    fn prompts_without_excerpts_state_their_absence() {
        let prompt = repo_prompt(&[], &[], &[]);
        assert!(prompt.contains("Source excerpts:\n- No source excerpts.\n"));
    }

    #[test]
    fn content_file_prompt_carries_leading_content() {
        let prompt = content_file_prompt(
            "README.md",
            &excerpt("README.md", "# Project\n\nWhat this repo does."),
        );
        assert!(prompt.contains("File: README.md"));
        assert!(prompt.contains("--- README.md (lines 1-40)"));
        assert!(prompt.contains("What this repo does."));
    }

    #[test]
    fn architecture_narrative_prompt_lists_subsystems_and_edges() {
        let subsystems = vec![
            ChildSummary {
                name: "crates/gcore".to_string(),
                summary: "Shared foundation library.".to_string(),
            },
            ChildSummary {
                name: "crates/gcode".to_string(),
                summary: "Code search CLI.".to_string(),
            },
        ];
        let edges = vec![("crates/gcode".to_string(), "crates/gcore".to_string())];

        let prompt = architecture_narrative_prompt(&subsystems, &edges);

        assert!(prompt.contains("- crates/gcore: Shared foundation library."));
        assert!(prompt.contains("- crates/gcode -> crates/gcore"));

        let empty = architecture_narrative_prompt(&subsystems, &[]);
        assert!(empty.contains("- No cross-subsystem dependency edges.\n"));
    }
}
