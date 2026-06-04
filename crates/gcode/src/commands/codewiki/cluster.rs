use super::*;

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
            common_module_for_files(grouped_files)
        } else {
            module_for_file(&grouped_files[0])
        };
        for file in grouped_files {
            modules.insert(file.clone(), module.clone());
        }
    }
    modules
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
