use std::fmt::Write as _;

use crate::models::Symbol;

pub const SYMBOL_SYSTEM: &str = "You write concise API reference notes. Return one sentence describing the symbol's purpose. Do not include markdown fences.";
pub const FILE_SYSTEM: &str = "You write concise file-level code documentation. Return a short purpose summary that reuses the supplied symbol summaries. Do not include markdown fences.";
pub const MODULE_SYSTEM: &str = "You write concise module overviews for code documentation. Return a short overview from the supplied child summaries. Do not include markdown fences.";
pub const REPO_SYSTEM: &str = "You write concise repository overviews for code documentation. Return a short overview from the supplied module summaries. Do not include markdown fences.";
pub const ARCHITECTURE_SYSTEM: &str = "You write concise architecture documentation. Return one sentence naming the subsystem's responsibility from the supplied lower-level summaries. Do not include markdown fences.";

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

pub fn file_prompt(file: &str, symbols: &[SymbolSummary]) -> String {
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
    prompt
}

pub fn module_prompt(
    module: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
) -> String {
    build_entity_prompt(
        "Summarize this module once from lower-level summaries.",
        "Module",
        module,
        files,
        modules,
        components,
    )
}

pub fn repo_prompt(modules: &[ChildSummary], files: &[ChildSummary]) -> String {
    let mut prompt =
        "Summarize this repository once from module and root-file summaries.\n\nModules:\n"
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
    prompt
}

pub fn architecture_prompt(
    subsystem: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
) -> String {
    build_entity_prompt(
        "Summarize this subsystem's responsibility for a repository architecture overview.",
        "Subsystem",
        subsystem,
        files,
        modules,
        components,
    )
}

fn build_entity_prompt(
    header: &str,
    entity_label: &str,
    entity: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
) -> String {
    let mut prompt = format!("{header}\n\n{entity_label}: {entity}\n\nFiles:\n");
    append_child_summary_sections(&mut prompt, files, modules, components);
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

/// Hard cap on one child summary embedded in an aggregate prompt. Aggregate
/// prompts roll up every child; unbounded summaries (citation walls, echoed
/// prompts recorded as summaries) once compounded up the module tree past
/// provider input limits (#698).
const CHILD_SUMMARY_EXCERPT_MAX_CHARS: usize = 600;

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
            module_prompt("src", &children, &children, &[]),
            architecture_prompt("src", &children, &children, &[]),
            repo_prompt(&children, &children),
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
        let prompt = module_prompt("src", &[child], &[], &[]);
        assert!(prompt.contains("- src/lib.rs: Concise healthy summary.\n"));
    }

    #[test]
    fn excerpt_flattens_multiline_summaries_to_one_line() {
        let child = ChildSummary {
            name: "src/lib.rs".to_string(),
            summary: "First line.\nSecond line of the same paragraph.".to_string(),
        };
        let prompt = module_prompt("src", &[child], &[], &[]);
        assert!(prompt.contains("- src/lib.rs: First line. Second line of the same paragraph.\n"));
    }
}
