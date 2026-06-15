use std::path::{Component, Path};

/// Project-relative files a JS/TS import `specifier` (resolved relative to
/// `rel_path`) could be defined in. Each resolved module key is expanded to the
/// supported source extensions and the directory `index.*` form. No file reads;
/// the post-write pass narrows these against the indexed symbols.
pub(crate) fn js_candidate_files(
    rel_path: &str,
    self_package: Option<&str>,
    specifier: &str,
) -> Vec<String> {
    const EXTENSIONS: [&str; 6] = ["js", "jsx", "cjs", "mjs", "ts", "tsx"];
    let mut files = Vec::new();
    for key in js_import_target_keys(rel_path, self_package, specifier) {
        if key.is_empty() {
            continue;
        }
        for ext in EXTENSIONS {
            files.push(format!("{key}.{ext}"));
            files.push(format!("{key}/index.{ext}"));
        }
    }
    dedupe(files)
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

fn dedupe(values: Vec<String>) -> Vec<String> {
    let mut deduped = Vec::new();
    for value in values {
        if !deduped.contains(&value) {
            deduped.push(value);
        }
    }
    deduped
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contains(files: &[String], expected: &str) -> bool {
        files.iter().any(|file| file == expected)
    }

    #[test]
    fn relative_specifier_resolves_to_sibling_file_and_index() {
        let files = js_candidate_files("src/a/b.ts", None, "./c");
        assert!(contains(&files, "src/a/c.ts"), "{files:?}");
        assert!(contains(&files, "src/a/c.js"), "{files:?}");
        assert!(contains(&files, "src/a/c/index.tsx"), "{files:?}");
    }

    #[test]
    fn parent_specifier_walks_up_one_directory() {
        let files = js_candidate_files("src/a/b.ts", None, "../shared");
        assert!(contains(&files, "src/shared.ts"), "{files:?}");
    }

    #[test]
    fn at_alias_maps_to_src_root() {
        let files = js_candidate_files("src/app.js", None, "@/utils");
        assert!(contains(&files, "src/utils.js"), "{files:?}");
    }

    #[test]
    fn self_package_specifier_resolves_with_and_without_src() {
        let files = js_candidate_files("src/app.js", Some("app"), "app/utils");
        assert!(contains(&files, "src/utils.js"), "{files:?}");
        assert!(contains(&files, "utils.js"), "{files:?}");
    }

    #[test]
    fn bare_external_specifier_yields_no_candidates() {
        assert!(js_candidate_files("src/app.js", Some("app"), "lodash").is_empty());
    }
}
