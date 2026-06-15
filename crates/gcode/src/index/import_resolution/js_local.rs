use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};

use rayon::prelude::*;

use super::context::LocalImportBinding;

#[derive(Debug, Clone, Default)]
pub(crate) struct JsLocalModule {
    pub(crate) exports: HashMap<String, LocalImportBinding>,
}

#[derive(Debug, Clone)]
struct JsDefinition {
    binding: LocalImportBinding,
    exported_name: Option<String>,
}

pub(crate) fn build_js_local_module_index(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<JsLocalModule>> {
    let entries = candidate_files
        .par_iter()
        .flat_map(|path| {
            let Some(keys) = js_module_keys(root_path, path) else {
                return Vec::new();
            };
            let Ok(source) = std::fs::read_to_string(path) else {
                return Vec::new();
            };
            let rel_path = relative_path_string(root_path, path);
            let is_typescript = path
                .extension()
                .and_then(|ext| ext.to_str())
                .is_some_and(|ext| matches!(ext, "ts" | "tsx"));
            let module = js_local_module(&source, &rel_path, is_typescript);
            if module.exports.is_empty() {
                return Vec::new();
            }
            keys.into_iter()
                .map(|key| (key, module.clone()))
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();

    let mut index: HashMap<String, Vec<JsLocalModule>> = HashMap::new();
    for (key, module) in entries {
        index.entry(key).or_default().push(module);
    }
    index
}

pub(crate) fn js_import_target_keys(
    rel_path: &str,
    self_package_name: Option<&str>,
    specifier: &str,
) -> Vec<String> {
    let specifier = specifier.trim();
    if specifier.is_empty() {
        return Vec::new();
    }

    if specifier.starts_with("./") || specifier.starts_with("../") {
        let base = Path::new(rel_path)
            .parent()
            .unwrap_or_else(|| Path::new(""));
        return module_key_variants(&normalize_module_path(&base.join(specifier)));
    }

    if specifier.starts_with('/') {
        return module_key_variants(&strip_js_extension(specifier.trim_start_matches('/')));
    }

    if let Some(rest) = specifier
        .strip_prefix("@/")
        .or_else(|| specifier.strip_prefix("~/"))
    {
        return module_key_variants(&format!("src/{}", strip_js_extension(rest)));
    }

    if let Some(package_name) = self_package_name {
        if specifier == package_name {
            return vec!["index".to_string(), "src/index".to_string()];
        }
        if let Some(rest) = specifier.strip_prefix(&format!("{package_name}/")) {
            let rest = strip_js_extension(rest);
            let mut keys = module_key_variants(&rest);
            keys.extend(module_key_variants(&format!("src/{rest}")));
            dedupe(keys)
        } else {
            Vec::new()
        }
    } else {
        Vec::new()
    }
}

fn js_module_keys(root_path: &Path, path: &Path) -> Option<Vec<String>> {
    let rel = path.strip_prefix(root_path).ok()?;
    let ext = rel
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default();
    if !matches!(ext, "js" | "jsx" | "cjs" | "mjs" | "ts" | "tsx") {
        return None;
    }

    let key = normalize_module_path(&rel.with_extension(""));
    let mut keys = module_key_variants(&key);
    if let Some(stripped) = key.strip_prefix("src/") {
        keys.extend(module_key_variants(stripped));
    }
    Some(dedupe(keys))
}

fn module_key_variants(key: &str) -> Vec<String> {
    let key = normalize_module_key(key);
    if key.is_empty() {
        return Vec::new();
    }

    let mut keys = vec![key.clone()];
    if let Some(stripped) = key.strip_suffix("/index")
        && !stripped.is_empty()
    {
        keys.push(stripped.to_string());
    }
    keys
}

fn normalize_module_path(path: &Path) -> String {
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                parts.pop();
            }
            Component::Normal(part) => parts.push(part.to_string_lossy().to_string()),
            Component::RootDir | Component::Prefix(_) => {}
        }
    }
    strip_js_extension(&parts.join("/"))
}

fn normalize_module_key(key: &str) -> String {
    key.replace('\\', "/")
        .trim_matches('/')
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .fold(Vec::<&str>::new(), |mut parts, part| {
            if part == ".." {
                parts.pop();
            } else {
                parts.push(part);
            }
            parts
        })
        .join("/")
}

fn strip_js_extension(module: &str) -> String {
    for ext in [".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx"] {
        if let Some(stripped) = module.strip_suffix(ext) {
            return stripped.to_string();
        }
    }
    module.to_string()
}

fn js_local_module(source: &str, rel_path: &str, is_typescript: bool) -> JsLocalModule {
    let mut top_level = HashMap::new();
    let mut exports = HashMap::new();
    let mut export_specifiers = Vec::new();
    let mut line_start = 0usize;

    for line in source.split_inclusive('\n') {
        let line_without_newline = line.trim_end_matches(['\r', '\n']);
        if let Some(definition) =
            js_top_level_definition(line_without_newline, rel_path, line_start, is_typescript)
        {
            top_level.insert(definition.binding.name.clone(), definition.binding.clone());
            if let Some(exported_name) = definition.exported_name {
                exports.insert(exported_name, definition.binding);
            }
        }
        export_specifiers.extend(js_export_specifiers(line_without_newline));
        line_start += line.len();
    }

    for (local_name, exported_name) in export_specifiers {
        if let Some(binding) = top_level.get(&local_name) {
            exports.insert(exported_name, binding.clone());
        }
    }

    JsLocalModule { exports }
}

fn js_top_level_definition(
    line: &str,
    rel_path: &str,
    line_start: usize,
    is_typescript: bool,
) -> Option<JsDefinition> {
    if line.starts_with(' ') || line.starts_with('\t') {
        return None;
    }

    let mut code = line.trim_start();
    let mut exported = false;
    if let Some(rest) = strip_keyword(code, "export") {
        exported = true;
        code = rest.trim_start();
    }

    let mut default_export = false;
    if exported && let Some(rest) = strip_keyword(code, "default") {
        default_export = true;
        code = rest.trim_start();
    }

    let (kind, name) = if let Some(name) = js_function_name(code) {
        ("function", name)
    } else if let Some(name) = js_class_name(code) {
        ("class", name)
    } else if (!exported || is_typescript)
        && let Some(name) = js_arrow_function_name(code)
    {
        ("function", name)
    } else {
        return None;
    };

    let exported_name = if default_export {
        Some("default".to_string())
    } else if exported {
        Some(name.clone())
    } else {
        None
    };

    Some(JsDefinition {
        binding: LocalImportBinding {
            file_path: rel_path.to_string(),
            name,
            kind: kind.to_string(),
            byte_start: line_start,
        },
        exported_name,
    })
}

fn strip_keyword<'a>(code: &'a str, keyword: &str) -> Option<&'a str> {
    let rest = code.strip_prefix(keyword)?;
    if rest
        .chars()
        .next()
        .is_some_and(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '$')
    {
        None
    } else {
        Some(rest)
    }
}

fn js_function_name(code: &str) -> Option<String> {
    let code = strip_keyword(code, "async")
        .map(str::trim_start)
        .unwrap_or(code);
    let rest = strip_keyword(code, "function")?.trim_start();
    js_identifier(rest)
}

fn js_class_name(code: &str) -> Option<String> {
    let rest = strip_keyword(code, "class")?.trim_start();
    js_identifier(rest)
}

fn js_arrow_function_name(code: &str) -> Option<String> {
    if !code.contains("=>") {
        return None;
    }
    let rest = strip_keyword(code, "const")
        .or_else(|| strip_keyword(code, "let"))
        .or_else(|| strip_keyword(code, "var"))?
        .trim_start();
    js_identifier(rest)
}

fn js_identifier(text: &str) -> Option<String> {
    let mut chars = text.chars();
    let first = chars.next()?;
    if !(first.is_ascii_alphabetic() || first == '_' || first == '$') {
        return None;
    }
    let mut name = String::from(first);
    for ch in chars {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '$' {
            name.push(ch);
        } else {
            break;
        }
    }
    Some(name)
}

fn js_export_specifiers(line: &str) -> Vec<(String, String)> {
    let line = line.trim();
    let Some(rest) = line.strip_prefix("export ") else {
        return Vec::new();
    };
    let Some(rest) = rest.strip_prefix('{') else {
        return Vec::new();
    };
    let Some((inner, suffix)) = rest.split_once('}') else {
        return Vec::new();
    };
    if suffix.contains(" from ") {
        return Vec::new();
    }

    inner
        .split(',')
        .filter_map(|item| {
            let item = item.trim().strip_prefix("type ").unwrap_or(item.trim());
            if item.is_empty() {
                return None;
            }
            let (local_name, exported_name) = item
                .split_once(" as ")
                .map(|(local, exported)| (local.trim(), exported.trim()))
                .unwrap_or((item, item));
            if local_name.is_empty() || exported_name.is_empty() {
                None
            } else {
                Some((local_name.to_string(), exported_name.to_string()))
            }
        })
        .collect()
}

fn dedupe(values: Vec<String>) -> Vec<String> {
    let mut deduped = Vec::new();
    for value in values {
        if !deduped.contains(&value) {
            deduped.push(value);
        }
    }
    deduped
}

fn relative_path_string(root_path: &Path, path: &Path) -> String {
    path.strip_prefix(root_path)
        .map(|rel| rel.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string())
}
