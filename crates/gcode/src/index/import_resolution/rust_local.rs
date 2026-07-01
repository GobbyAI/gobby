use super::predicates::STANDARD_RUST_CRATES;
use std::collections::HashSet;

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

/// Project-relative files a Rust module path could be defined in, given the
/// crate `source_root` (e.g. `crates/gcode/src`) and the crate-relative `module`
/// (`::`-separated). Inverts [`rust_module_context_for_rel_path`] to cover both
/// `foo/bar.rs` and `foo/bar/mod.rs`; an empty module is the crate root
/// (`lib.rs`/`main.rs`). No file reads — the post-write pass narrows against the
/// indexed symbols.
pub(crate) fn rust_candidate_files(source_root: &str, module: &str) -> Vec<String> {
    let root = source_root.trim_end_matches('/');
    if module.is_empty() {
        return vec![format!("{root}/lib.rs"), format!("{root}/main.rs")];
    }
    let module_path = module.replace("::", "/");
    vec![
        format!("{root}/{module_path}.rs"),
        format!("{root}/{module_path}/mod.rs"),
    ]
}

pub(crate) fn rust_import_target(
    rel_path: &str,
    self_crate_name: Option<&str>,
    rust_external_crates: &HashSet<String>,
    path: &str,
) -> Option<RustLocalTarget> {
    let context = rust_module_context_for_rel_path(rel_path)?;
    let segments = rust_path_segments(path);
    let (name, module_segments) = segments.split_last()?;
    let module = rust_module_for_segments(
        &context,
        self_crate_name,
        rust_external_crates,
        module_segments,
    )?;
    Some(RustLocalTarget {
        source_root: context.source_root,
        module,
        name: (*name).to_string(),
    })
}

pub(crate) fn rust_qualified_call_target(
    rel_path: &str,
    self_crate_name: Option<&str>,
    rust_external_crates: &HashSet<String>,
    qualifier_path: &str,
    callee_name: &str,
) -> Option<RustLocalTarget> {
    let context = rust_module_context_for_rel_path(rel_path)?;
    let segments = rust_path_segments(qualifier_path);
    let module =
        rust_module_for_segments(&context, self_crate_name, rust_external_crates, &segments)?;
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
    rust_external_crates: &HashSet<String>,
    segments: &[&str],
) -> Option<String> {
    let (first, rest) = segments.split_first()?;
    match *first {
        "crate" => Some(rest.join("::")),
        "self" => Some(join_rust_module(&context.module, rest)),
        "super" => Some(rust_super_module(&context.module, rest)),
        root if Some(root) == self_crate_name => Some(rest.join("::")),
        root if STANDARD_RUST_CRATES.contains(&root) => None,
        root if rust_external_crates.contains(root) => None,
        _ => Some(join_rust_module(&context.module, segments)),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidate_files_cover_module_file_and_mod_rs() {
        assert_eq!(
            rust_candidate_files("crates/gcode/src", "index::foo"),
            vec![
                "crates/gcode/src/index/foo.rs".to_string(),
                "crates/gcode/src/index/foo/mod.rs".to_string(),
            ]
        );
    }

    #[test]
    fn candidate_files_for_crate_root_are_lib_and_main() {
        assert_eq!(
            rust_candidate_files("src", ""),
            vec!["src/lib.rs".to_string(), "src/main.rs".to_string()]
        );
    }

    #[test]
    fn import_target_resolves_crate_self_super_and_self_crate_roots() {
        let rel = "src/index/context.rs";
        let external_crates = HashSet::new();
        let crate_target =
            rust_import_target(rel, Some("app"), &external_crates, "crate::service::helper")
                .unwrap();
        assert_eq!(crate_target.module, "service");
        assert_eq!(crate_target.name, "helper");

        let super_target =
            rust_import_target(rel, Some("app"), &external_crates, "super::sibling::run").unwrap();
        assert_eq!(super_target.module, "index::sibling");

        let self_crate_target =
            rust_import_target(rel, Some("app"), &external_crates, "app::service::run").unwrap();
        assert_eq!(self_crate_target.module, "service");
    }

    #[test]
    fn import_target_leaves_external_crate_paths_unresolved() {
        let external_crates = HashSet::from(["serde_json".to_string()]);
        // External crate roots must not be invented as local candidate files.
        assert!(
            rust_import_target(
                "src/lib.rs",
                Some("app"),
                &external_crates,
                "serde_json::from_str",
            )
            .is_none()
        );
        assert!(rust_import_target("src/lib.rs", None, &HashSet::new(), "std::fs::read").is_none());
    }

    #[test]
    fn import_target_resolves_bare_non_external_root_relative_to_current_module() {
        let target =
            rust_import_target("src/app/main.rs", Some("app"), &HashSet::new(), "foo::bar")
                .unwrap();

        assert_eq!(target.source_root, "src");
        assert_eq!(target.module, "app::foo");
        assert_eq!(target.name, "bar");
    }

    #[test]
    fn qualified_call_resolves_bare_non_external_root_from_crate_root() {
        let target =
            rust_qualified_call_target("src/main.rs", Some("app"), &HashSet::new(), "foo", "bar")
                .unwrap();

        assert_eq!(target.source_root, "src");
        assert_eq!(target.module, "foo");
        assert_eq!(target.name, "bar");
    }
}
