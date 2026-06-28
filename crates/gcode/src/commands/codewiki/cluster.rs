use super::*;

/// Directory roots acting as the repository's top decomposition units.
/// A top-level directory that holds no direct files is a container — like
/// `crates/` in a Cargo workspace — and expands one level so decomposition
/// starts at meaningful units (the individual crates) instead of the
/// container. Root-level files belong to no subsystem.
pub(crate) fn subsystem_roots(files: &[String]) -> BTreeSet<String> {
    let mut top_level = BTreeSet::new();
    let mut has_direct_files = BTreeSet::new();
    let mut children: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for file in files {
        let module = module_for_file(file);
        let mut parts = module.split('/').filter(|part| !part.is_empty());
        let Some(top) = parts.next() else {
            continue;
        };
        top_level.insert(top.to_string());
        match parts.next() {
            None => {
                has_direct_files.insert(top.to_string());
            }
            Some(child) => {
                children
                    .entry(top.to_string())
                    .or_default()
                    .insert(format!("{top}/{child}"));
            }
        }
    }

    let mut roots = BTreeSet::new();
    for top in top_level {
        let expandable = !has_direct_files.contains(&top)
            && children.get(&top).is_some_and(|kids| !kids.is_empty());
        if expandable {
            roots.extend(children.remove(&top).unwrap_or_default());
        } else {
            roots.insert(top);
        }
    }
    roots
}

/// The subsystem root that owns `file`, when one exists.
pub(crate) fn subsystem_root_for_file<'a>(
    file: &str,
    roots: &'a BTreeSet<String>,
) -> Option<&'a str> {
    let module = module_for_file(file);
    roots
        .iter()
        .map(String::as_str)
        .find(|root| module_is_within(&module, root))
}

fn module_is_within(module: &str, root: &str) -> bool {
    module
        .strip_prefix(root)
        .is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
}

pub(crate) fn cluster_file_modules(
    files: &[String],
    symbols_by_file: &BTreeMap<String, Vec<Symbol>>,
    graph_edges: &[CodewikiGraphEdge],
) -> HashMap<String, String> {
    let mut components_to_file = HashMap::new();
    for (file, symbols) in symbols_by_file {
        for symbol in symbols {
            components_to_file.insert(symbol.id.clone(), file.clone());
        }
    }

    let roots = subsystem_roots(files);
    let mut parents = files
        .iter()
        .map(|file| (file.clone(), file.clone()))
        .collect::<HashMap<_, _>>();
    let mut ranks = files
        .iter()
        .map(|file| (file.clone(), 0_usize))
        .collect::<HashMap<_, _>>();
    for edge in graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Call)
    {
        let Some(source_file) = components_to_file.get(&edge.source_component_id) else {
            continue;
        };
        let Some(target_file) = components_to_file.get(&edge.target_component_id) else {
            continue;
        };
        // Call edges never merge clusters across subsystem roots; otherwise
        // one cross-subsystem call collapses the decomposition to the common
        // container (`crates`) and every page above it loses its structure.
        if subsystem_root_for_file(source_file, &roots)
            != subsystem_root_for_file(target_file, &roots)
        {
            continue;
        }
        union_files(&mut parents, &mut ranks, source_file, target_file);
    }

    let mut grouped: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for file in files {
        let root = find_file_root(&mut parents, file);
        grouped.entry(root).or_default().push(file.clone());
    }

    let mut modules = HashMap::new();
    for grouped_files in grouped.values() {
        let module = if grouped_files.len() > 1 {
            cluster_module_name(grouped_files, symbols_by_file)
        } else {
            module_for_file(&grouped_files[0])
        };
        for file in grouped_files {
            modules.insert(file.clone(), module.clone());
        }
    }
    modules
}

/// Tokens that say nothing about a cluster's *purpose* — file scaffolding,
/// generic containers, and test plumbing — dropped when deriving a semantic
/// cluster name so the label reflects what the code does.
const CLUSTER_NAME_STOPWORDS: &[&str] = &[
    "mod", "lib", "main", "common", "shared", "util", "utils", "helper", "helpers", "type",
    "types", "core", "support", "render", "test", "tests", "mock", "mocks", "impl", "base", "data",
    "src", "crate", "crates",
];

/// Name a multi-file cluster.
///
/// When every member lives in the same directory the shared path is already a
/// meaningful label, so it stands. When the cluster spans directory boundaries
/// the shared path collapses to a generic container (`crates/gcode/src/commands`)
/// that loses the cluster's purpose, so a semantic leaf derived from the
/// members' recurring name tokens (e.g. `recovery_cleanup`) is nested under that
/// container. Nesting keeps the path hierarchy intact for ancestor/child
/// resolution while giving the concept layer a purpose-named title.
fn cluster_module_name(
    files: &[String],
    symbols_by_file: &BTreeMap<String, Vec<Symbol>>,
) -> String {
    let common = common_module_for_files(files);
    if !spans_sibling_subdirectories(files, &common) {
        return common;
    }
    match semantic_cluster_leaf(files, symbols_by_file) {
        Some(leaf) if common.is_empty() => leaf,
        Some(leaf) => format!("{common}/{leaf}"),
        None => common,
    }
}

/// Whether a cluster's members fan out across two or more *sibling*
/// subdirectories of their common module — the case where the shared path is a
/// generic fork point (`crates/app/src` for files under `commands/` and
/// `daemon/`) rather than a real module. Natural nesting under one branch
/// (`src/api` holding `api/handler.rs` and `api/inner/router.rs`) is a single
/// branch and keeps its meaningful path name.
fn spans_sibling_subdirectories(files: &[String], common: &str) -> bool {
    let common_depth = common.split('/').filter(|part| !part.is_empty()).count();
    let mut branches = BTreeSet::new();
    for file in files {
        let module = module_for_file(file);
        let branch = module
            .split('/')
            .filter(|part| !part.is_empty())
            .nth(common_depth);
        if let Some(branch) = branch {
            branches.insert(branch.to_string());
        }
        if branches.len() >= 2 {
            return true;
        }
    }
    false
}

/// Derive a purpose-named leaf for a directory-spanning cluster from the
/// recurring tokens in its member file names (and, secondarily, their symbol
/// names). Tokens are ranked by how many distinct members they appear in (a
/// shared theme), then lexicographically for determinism; the top two are
/// joined with `_`. Returns `None` when no meaningful token survives, so the
/// caller keeps the path-based name.
fn semantic_cluster_leaf(
    files: &[String],
    symbols_by_file: &BTreeMap<String, Vec<Symbol>>,
) -> Option<String> {
    let mut frequency: BTreeMap<String, usize> = BTreeMap::new();
    for file in files {
        let mut seen = BTreeSet::new();
        for token in cluster_name_tokens(file, symbols_by_file.get(file)) {
            if seen.insert(token.clone()) {
                *frequency.entry(token).or_default() += 1;
            }
        }
    }
    if frequency.is_empty() {
        return None;
    }
    let mut ranked: Vec<(String, usize)> = frequency.into_iter().collect();
    ranked.sort_by(|(left_token, left_count), (right_token, right_count)| {
        right_count
            .cmp(left_count)
            .then_with(|| left_token.cmp(right_token))
    });
    let leaf = if ranked[0].1 >= 2 {
        // One or more tokens recur across members — a genuine shared theme.
        // Those describe the cluster's purpose on their own.
        ranked
            .iter()
            .filter(|(_, count)| *count >= 2)
            .take(2)
            .map(|(token, _)| token.as_str())
            .collect::<Vec<_>>()
            .join("_")
    } else {
        // No shared token — combine the two most salient distinct tokens so a
        // cross-directory cluster of differently-named files still reads as a
        // purpose (e.g. `cleanup_recovery`).
        ranked
            .iter()
            .take(2)
            .map(|(token, _)| token.as_str())
            .collect::<Vec<_>>()
            .join("_")
    };
    (!leaf.is_empty()).then_some(leaf)
}

/// Lowercase purpose tokens from a file's base name (and, secondarily, its
/// symbol names): the extension is dropped, identifiers are split on
/// snake_case / kebab-case / camelCase boundaries, and scaffolding stopwords
/// and tokens shorter than three characters are removed. Symbol names are
/// capped so one large file cannot dominate the cluster name.
fn cluster_name_tokens(file: &str, symbols: Option<&Vec<Symbol>>) -> Vec<String> {
    let stem = Path::new(file)
        .file_stem()
        .map(|stem| stem.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut tokens = split_identifier_tokens(&stem);
    if let Some(symbols) = symbols {
        for symbol in symbols.iter().take(8) {
            tokens.extend(split_identifier_tokens(&symbol.name));
        }
    }
    tokens
        .into_iter()
        .filter(|token| token.len() >= 3 && !CLUSTER_NAME_STOPWORDS.contains(&token.as_str()))
        .collect()
}

/// Split an identifier into lowercase word tokens on snake_case, kebab-case,
/// and camelCase / PascalCase boundaries.
fn split_identifier_tokens(value: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut prev_alnum_lower = false;
    for ch in value.chars() {
        if matches!(ch, '_' | '-' | '/' | '.' | ' ' | ':') {
            if !current.is_empty() {
                tokens.push(std::mem::take(&mut current));
            }
            prev_alnum_lower = false;
            continue;
        }
        if ch.is_uppercase() && prev_alnum_lower && !current.is_empty() {
            tokens.push(std::mem::take(&mut current));
        }
        current.extend(ch.to_lowercase());
        prev_alnum_lower = ch.is_lowercase() || ch.is_numeric();
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
}

pub(crate) fn union_files(
    parents: &mut HashMap<String, String>,
    ranks: &mut HashMap<String, usize>,
    left: &str,
    right: &str,
) {
    let left_root = find_file_root(parents, left);
    let right_root = find_file_root(parents, right);
    if left_root == right_root {
        return;
    }

    let left_rank = *ranks.entry(left_root.clone()).or_default();
    let right_rank = *ranks.entry(right_root.clone()).or_default();
    let (parent, child, increment_rank) = match left_rank.cmp(&right_rank) {
        std::cmp::Ordering::Greater => (left_root, right_root, false),
        std::cmp::Ordering::Less => (right_root, left_root, false),
        std::cmp::Ordering::Equal if left_root <= right_root => (left_root, right_root, true),
        std::cmp::Ordering::Equal => (right_root, left_root, true),
    };
    parents.insert(child, parent.clone());
    if increment_rank {
        *ranks.entry(parent).or_default() += 1;
    }
}

/// Find the canonical root for a file in the module-union parent map.
///
/// The parent map is expected to represent a disjoint-set forest where roots
/// point to themselves. If a cycle slips in, choose the lexicographically
/// smallest file in the discovered cycle/path as the stable root. Every visited
/// node is then path-compressed to that root so future lookups keep the forest
/// invariant.
pub(crate) fn find_file_root(parents: &mut HashMap<String, String>, file: &str) -> String {
    let mut current = file.to_string();
    let mut path = Vec::new();
    let mut seen = HashSet::new();

    let root = loop {
        if !seen.insert(current.clone()) {
            path.push(current.clone());
            let root = path
                .iter()
                .min()
                .cloned()
                .unwrap_or_else(|| current.clone());
            debug_assert!(
                path.iter().any(|node| node == &current),
                "codewiki file union parent cycle detected while resolving `{file}`"
            );
            log::debug!(
                "codewiki file union parent cycle detected while resolving `{}`; compressing to `{}`",
                file,
                root
            );
            break root;
        }

        let parent = parents
            .get(&current)
            .cloned()
            .unwrap_or_else(|| current.clone());
        if parent == current {
            break parent;
        }

        path.push(current);
        current = parent;
    };

    for node in path {
        parents.insert(node, root.clone());
    }
    root
}

pub(crate) fn common_module_for_files(files: &[String]) -> String {
    if files.is_empty() {
        return String::new();
    }

    let mut common = module_for_file(&files[0])
        .split('/')
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect::<Vec<_>>();
    for file in &files[1..] {
        let parts = module_for_file(file)
            .split('/')
            .filter(|part| !part.is_empty())
            .map(str::to_string)
            .collect::<Vec<_>>();
        let keep = common
            .iter()
            .zip(parts.iter())
            .take_while(|(left, right)| left == right)
            .count();
        common.truncate(keep);
    }
    common.join("/")
}

pub(crate) fn symbols_by_file_component(symbols: &[Symbol]) -> BTreeMap<String, Vec<String>> {
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for symbol in symbols {
        if is_core_file(&symbol.file_path) {
            out.entry(symbol.file_path.clone())
                .or_default()
                .push(symbol.id.clone());
        }
    }
    out
}

pub(crate) fn first_component_for_file(
    symbols_by_file: &BTreeMap<String, Vec<String>>,
    file: &str,
) -> Option<String> {
    symbols_by_file
        .get(file)
        .and_then(|components| components.first())
        .cloned()
}

pub(crate) fn files_for_import_target<'a>(
    files: &'a [String],
    target_module: &str,
) -> Vec<&'a str> {
    let target = module_components(target_module);
    if target.is_empty() {
        return Vec::new();
    }
    files
        .iter()
        .map(String::as_str)
        .filter(|file| {
            contains_component_sequence(&path_components(file), &target)
                || contains_component_sequence(&module_components(&module_for_file(file)), &target)
        })
        .collect()
}

fn module_components(value: &str) -> Vec<String> {
    value
        .replace("::", "/")
        .replace(['.', '\\'], "/")
        .split('/')
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect()
}

fn path_components(file: &str) -> Vec<String> {
    Path::new(file)
        .components()
        .filter_map(|component| match component {
            std::path::Component::Normal(part) => {
                let part = part.to_string_lossy();
                let component = if Path::new(part.as_ref()).extension().is_some() {
                    Path::new(part.as_ref())
                        .file_stem()
                        .map(|stem| stem.to_string_lossy().into_owned())
                } else {
                    Some(part.into_owned())
                };
                component.filter(|part| !part.is_empty())
            }
            _ => None,
        })
        .collect()
}

fn contains_component_sequence(components: &[String], target: &[String]) -> bool {
    target.len() <= components.len()
        && components
            .windows(target.len())
            .any(|window| window.iter().zip(target).all(|(left, right)| left == right))
}

#[cfg(test)]
mod cluster_tests {
    use super::*;

    fn paths(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn subsystem_roots_expand_container_directories_one_level() {
        let files = paths(&[
            "crates/gcode/src/main.rs",
            "crates/gcore/src/lib.rs",
            "docs/guides/guide.md",
            "scripts/install.sh",
            "README.md",
        ]);

        let roots = subsystem_roots(&files);

        assert!(roots.contains("crates/gcode"), "{roots:?}");
        assert!(roots.contains("crates/gcore"), "{roots:?}");
        assert!(!roots.contains("crates"), "container expands: {roots:?}");
        assert!(roots.contains("docs/guides"), "{roots:?}");
        assert!(roots.contains("scripts"), "{roots:?}");
    }

    #[test]
    fn subsystem_roots_keep_top_level_directories_with_direct_files() {
        let files = paths(&["docs/readme.md", "docs/guides/guide.md"]);
        let roots = subsystem_roots(&files);
        assert_eq!(roots, BTreeSet::from(["docs".to_string()]), "{roots:?}");
    }

    #[test]
    fn subsystem_root_for_file_matches_whole_components_only() {
        let roots = BTreeSet::from(["crates/gcode".to_string()]);
        assert_eq!(
            subsystem_root_for_file("crates/gcode/src/main.rs", &roots),
            Some("crates/gcode")
        );
        assert_eq!(
            subsystem_root_for_file("crates/gcodex/src/main.rs", &roots),
            None
        );
        assert_eq!(subsystem_root_for_file("README.md", &roots), None);
    }

    #[test]
    fn call_edges_never_merge_clusters_across_subsystem_roots() {
        let files = paths(&["crates/gcode/src/main.rs", "crates/gcore/src/lib.rs"]);
        let main_symbol = Symbol::make_id(
            "project-1",
            "crates/gcode/src/main.rs",
            "main",
            "function",
            0,
        );
        let lib_symbol = Symbol::make_id(
            "project-1",
            "crates/gcore/src/lib.rs",
            "helper",
            "function",
            0,
        );
        let mut symbols_by_file: BTreeMap<String, Vec<Symbol>> = BTreeMap::new();
        for (file, name, id) in [
            ("crates/gcode/src/main.rs", "main", &main_symbol),
            ("crates/gcore/src/lib.rs", "helper", &lib_symbol),
        ] {
            let symbol = Symbol {
                id: id.clone(),
                project_id: "project-1".to_string(),
                file_path: file.to_string(),
                name: name.to_string(),
                qualified_name: name.to_string(),
                kind: "function".to_string(),
                language: "rust".to_string(),
                byte_start: 0,
                byte_end: 0,
                line_start: 1,
                line_end: 1,
                signature: None,
                docstring: None,
                parent_symbol_id: None,
                content_hash: String::new(),
                summary: None,
                created_at: String::new(),
                updated_at: String::new(),
            };
            symbols_by_file.insert(file.to_string(), vec![symbol]);
        }
        let edges = vec![CodewikiGraphEdge::call(
            main_symbol.clone(),
            lib_symbol.clone(),
        )];

        let modules = cluster_file_modules(&files, &symbols_by_file, &edges);

        assert_eq!(
            modules.get("crates/gcode/src/main.rs").map(String::as_str),
            Some("crates/gcode/src"),
            "cross-root call must not collapse modules to `crates`: {modules:?}"
        );
        assert_eq!(
            modules.get("crates/gcore/src/lib.rs").map(String::as_str),
            Some("crates/gcore/src"),
            "{modules:?}"
        );
    }

    #[test]
    fn same_directory_cluster_keeps_its_shared_path() {
        // Every member lives in one directory, so the shared path is already a
        // meaningful label and the semantic rename does not fire.
        let files = paths(&[
            "crates/app/src/search/bm25.rs",
            "crates/app/src/search/semantic.rs",
        ]);
        let symbols = BTreeMap::new();
        assert_eq!(
            cluster_module_name(&files, &symbols),
            "crates/app/src/search"
        );
    }

    #[test]
    fn cross_directory_cluster_is_named_by_shared_purpose() {
        // Members span two directories, so the shared path collapses to the
        // generic container; a token recurring across members names a leaf
        // nested under it rather than leaving the cluster as `.../src`.
        let files = paths(&[
            "crates/app/src/commands/recovery_start.rs",
            "crates/app/src/daemon/recovery_finish.rs",
        ]);
        let symbols = BTreeMap::new();
        assert_eq!(
            cluster_module_name(&files, &symbols),
            "crates/app/src/recovery"
        );
    }

    #[test]
    fn cross_directory_cluster_without_a_shared_token_combines_salient_tokens() {
        // No token recurs across the differently-named members, so the two most
        // salient purpose tokens combine into a readable leaf.
        let files = paths(&[
            "crates/app/src/commands/cleanup_runner.rs",
            "crates/app/src/jobs/recovery_worker.rs",
        ]);
        let symbols = BTreeMap::new();
        assert_eq!(
            cluster_module_name(&files, &symbols),
            "crates/app/src/cleanup_recovery"
        );
    }
}
