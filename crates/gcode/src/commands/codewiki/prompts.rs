use std::fmt::Write as _;

use crate::models::Symbol;

pub const SYMBOL_SYSTEM: &str = "You write concise API reference notes. Return one sentence describing the symbol's purpose. Do not include markdown fences.";
pub const FILE_SYSTEM: &str = "You write concise file-level code documentation. Return a short purpose summary that reuses the supplied symbol summaries. Do not include markdown fences.";
pub const MODULE_SYSTEM: &str = "You write concise module overviews for code documentation. Return a short overview from the supplied child summaries. Do not include markdown fences.";
pub const REPO_SYSTEM: &str = "You write concise repository overviews for code documentation. Return a short overview from the supplied module summaries. Do not include markdown fences.";

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
        let _ = write!(prompt, "\nSignature: {signature}");
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        let _ = write!(prompt, "\nExisting docs: {docstring}");
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
    let mut prompt = format!(
        "Summarize this module once from lower-level summaries.\n\nModule: {module}\n\nFiles:\n"
    );
    if files.is_empty() {
        prompt.push_str("- No direct files.\n");
    } else {
        for file in files {
            let _ = writeln!(prompt, "- {}: {}", file.name, file.summary);
        }
    }
    prompt.push_str("\nChild modules:\n");
    if modules.is_empty() {
        prompt.push_str("- No child modules.\n");
    } else {
        for module in modules {
            let _ = writeln!(prompt, "- {}: {}", module.name, module.summary);
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
    prompt
}

pub fn repo_prompt(modules: &[ChildSummary], files: &[ChildSummary]) -> String {
    let mut prompt =
        "Summarize this repository once from module and root-file summaries.\n\nModules:\n"
            .to_string();
    if modules.is_empty() {
        prompt.push_str("- No modules.\n");
    } else {
        for module in modules {
            let _ = writeln!(prompt, "- {}: {}", module.name, module.summary);
        }
    }
    prompt.push_str("\nRoot files:\n");
    if files.is_empty() {
        prompt.push_str("- No root files.\n");
    } else {
        for file in files {
            let _ = writeln!(prompt, "- {}: {}", file.name, file.summary);
        }
    }
    prompt
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
