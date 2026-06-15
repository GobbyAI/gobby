use std::collections::HashMap;
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::context::LocalImportBinding;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RustLocalTarget {
    pub(crate) source_root: String,
    pub(crate) module: String,
    pub(crate) name: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RustModuleContext {
    source_root: String,
    module: String,
}

pub(crate) fn build_rust_local_symbol_index(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<LocalImportBinding>> {
    let entries = candidate_files
        .par_iter()
        .flat_map(|path| {
            let rel_path = relative_path_string(root_path, path);
            let Some(module_context) = rust_module_context_for_rel_path(&rel_path) else {
                return Vec::new();
            };
            let Ok(source) = std::fs::read_to_string(path) else {
                return Vec::new();
            };

            rust_top_level_definitions(&source, &rel_path)
                .into_iter()
                .map(|binding| {
                    (
                        rust_local_symbol_key(
                            &module_context.source_root,
                            &module_context.module,
                            &binding.name,
                        ),
                        binding,
                    )
                })
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();

    let mut index: HashMap<String, Vec<LocalImportBinding>> = HashMap::new();
    for (key, binding) in entries {
        index.entry(key).or_default().push(binding);
    }
    index
}

pub(crate) fn rust_local_symbol_key(source_root: &str, module: &str, name: &str) -> String {
    format!("{source_root}:{module}:{name}")
}

pub(crate) fn rust_import_target(
    rel_path: &str,
    self_crate_name: Option<&str>,
    path: &str,
) -> Option<RustLocalTarget> {
    let context = rust_module_context_for_rel_path(rel_path)?;
    let segments = rust_path_segments(path);
    let (name, module_segments) = segments.split_last()?;
    let module = rust_module_for_segments(&context, self_crate_name, module_segments)?;
    Some(RustLocalTarget {
        source_root: context.source_root,
        module,
        name: (*name).to_string(),
    })
}

pub(crate) fn rust_qualified_call_target(
    rel_path: &str,
    self_crate_name: Option<&str>,
    qualifier_path: &str,
    callee_name: &str,
) -> Option<RustLocalTarget> {
    let context = rust_module_context_for_rel_path(rel_path)?;
    let segments = rust_path_segments(qualifier_path);
    let module = rust_module_for_segments(&context, self_crate_name, &segments)?;
    Some(RustLocalTarget {
        source_root: context.source_root,
        module,
        name: callee_name.to_string(),
    })
}

fn rust_module_context_for_rel_path(rel_path: &str) -> Option<RustModuleContext> {
    let parts: Vec<&str> = rel_path
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    let src_index = parts.iter().rposition(|part| *part == "src")?;
    let rest = parts.get(src_index + 1..)?;
    let file_name = rest.last().copied()?;
    let file_stem = file_name.strip_suffix(".rs")?;
    let source_root = parts[..=src_index].join("/");
    let mut module_parts = rest[..rest.len() - 1].to_vec();
    if file_stem != "lib" && file_stem != "main" && file_stem != "mod" {
        module_parts.push(file_stem);
    }
    Some(RustModuleContext {
        source_root,
        module: module_parts.join("::"),
    })
}

fn rust_module_for_segments(
    context: &RustModuleContext,
    self_crate_name: Option<&str>,
    segments: &[&str],
) -> Option<String> {
    let (first, rest) = segments.split_first()?;
    match *first {
        "crate" => Some(rest.join("::")),
        "self" => Some(join_rust_module(&context.module, rest)),
        "super" => Some(rust_super_module(&context.module, rest)),
        root if Some(root) == self_crate_name => Some(rest.join("::")),
        _ => Some(segments.join("::")),
    }
}

fn rust_super_module(current_module: &str, rest: &[&str]) -> String {
    let mut base: Vec<&str> = current_module
        .split("::")
        .filter(|part| !part.is_empty())
        .collect();
    if !base.is_empty() {
        base.pop();
    }
    base.extend(rest.iter().copied());
    base.join("::")
}

fn join_rust_module(base: &str, rest: &[&str]) -> String {
    let mut parts: Vec<&str> = base.split("::").filter(|part| !part.is_empty()).collect();
    parts.extend(rest.iter().copied());
    parts.join("::")
}

fn rust_path_segments(path: &str) -> Vec<&str> {
    path.split("::")
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .collect()
}

fn rust_top_level_definitions(source: &str, rel_path: &str) -> Vec<LocalImportBinding> {
    let mut definitions = Vec::new();
    let mut line_start = 0usize;
    for line in source.split_inclusive('\n') {
        let line_without_newline = line.trim_end_matches(['\r', '\n']);
        if let Some((kind, name, name_offset)) = rust_top_level_definition(line_without_newline) {
            definitions.push(LocalImportBinding {
                file_path: rel_path.to_string(),
                name,
                kind: kind.to_string(),
                byte_start: line_start + name_offset,
            });
        }
        line_start += line.len();
    }
    definitions
}

fn rust_top_level_definition(line: &str) -> Option<(&'static str, String, usize)> {
    if line.starts_with(char::is_whitespace) {
        return None;
    }
    let code = line.split_once("//").map(|(code, _)| code).unwrap_or(line);
    let (code, _offset) = strip_rust_decl_prefixes(code, 0);
    for (keyword, kind) in [
        ("fn ", "function"),
        ("struct ", "class"),
        ("enum ", "type"),
        ("trait ", "type"),
        ("type ", "type"),
    ] {
        if let Some(rest) = code.strip_prefix(keyword) {
            let (name, _) = rust_identifier(rest)?;
            return Some((kind, name, 0));
        }
    }
    None
}

fn strip_rust_decl_prefixes(mut code: &str, mut offset: usize) -> (&str, usize) {
    loop {
        let trimmed = code.trim_start();
        offset += code.len() - trimmed.len();
        code = trimmed;
        if let Some(rest) = strip_rust_visibility(code) {
            offset += code.len() - rest.len();
            code = rest;
            continue;
        }
        let Some(rest) = code
            .strip_prefix("async ")
            .or_else(|| code.strip_prefix("const "))
            .or_else(|| code.strip_prefix("unsafe "))
        else {
            return (code, offset);
        };
        offset += code.len() - rest.len();
        code = rest;
    }
}

fn strip_rust_visibility(code: &str) -> Option<&str> {
    if let Some(rest) = code.strip_prefix("pub ") {
        return Some(rest);
    }
    let rest = code.strip_prefix("pub(")?;
    let close = rest.find(')')?;
    rest.get(close + 1..)?.strip_prefix(' ')
}

fn rust_identifier(text: &str) -> Option<(String, usize)> {
    let leading = text.len() - text.trim_start().len();
    let text = text.trim_start();
    let mut chars = text.char_indices();
    let (_, first) = chars.next()?;
    if !(first == '_' || first.is_ascii_alphabetic()) {
        return None;
    }
    let mut end = first.len_utf8();
    for (idx, ch) in chars {
        if ch == '_' || ch.is_ascii_alphanumeric() {
            end = idx + ch.len_utf8();
        } else {
            break;
        }
    }
    let name = text[..end].to_string();
    Some((name, leading))
}

fn relative_path_string(root_path: &Path, path: &Path) -> String {
    path.strip_prefix(root_path)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}
