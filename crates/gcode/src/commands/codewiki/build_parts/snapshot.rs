use super::super::*;
use crate::index::hasher;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

pub(crate) fn build_codewiki_index_snapshot(
    project_root: &Path,
    input: &CodewikiInput,
) -> anyhow::Result<CodewikiIndexSnapshot> {
    let mut files = input
        .files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<BTreeSet<_>>();
    for symbol in &input.symbols {
        if is_core_file(&symbol.file_path) {
            files.insert(symbol.file_path.clone());
        }
    }

    let symbols_by_file = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .fold(BTreeMap::<String, usize>::new(), |mut counts, symbol| {
            *counts.entry(symbol.file_path.clone()).or_default() += 1;
            counts
        });
    let mut file_snapshots = BTreeMap::new();
    for file in files {
        // Indexed files can vanish from disk before the run starts (the index
        // lags external commits that delete sources); skip them instead of
        // aborting the whole run (#18109).
        let Some(content_hash) = hash_snapshot_file(project_root, &file)? else {
            eprintln!("warning: skipping codewiki source file missing from disk: {file}");
            continue;
        };
        file_snapshots.insert(
            file.clone(),
            CodewikiFileSnapshot {
                content_hash,
                symbol_count: symbols_by_file.get(&file).copied().unwrap_or_default(),
            },
        );
    }

    let symbols = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| {
            (
                symbol.id.clone(),
                CodewikiSymbolSnapshot {
                    file_path: symbol.file_path.clone(),
                    name: symbol.name.clone(),
                    qualified_name: symbol.qualified_name.clone(),
                    kind: symbol.kind.clone(),
                    line_start: symbol.line_start,
                },
            )
        })
        .collect::<BTreeMap<_, _>>();

    // Graph availability decides only whether neighborhood fingerprints can be
    // computed (used for incremental change detection); it is informational and
    // never degrades the changes page, so no graph marker is recorded here.
    let graph_neighborhoods = match input.graph_availability {
        CodewikiGraphAvailability::Available => Some(graph_neighborhood_fingerprints(
            &symbols,
            &input.graph_edges,
        )),
        CodewikiGraphAvailability::Truncated | CodewikiGraphAvailability::Unavailable => None,
    };

    Ok(CodewikiIndexSnapshot {
        files: file_snapshots,
        symbols,
        graph_neighborhoods,
        degraded_sources: Vec::new(),
    })
}

/// Hash one snapshot source from disk; `Ok(None)` means the file no longer
/// exists (deleted since indexing) and the caller should skip it (#18109).
pub(crate) fn hash_snapshot_file(
    project_root: &Path,
    file: &str,
) -> anyhow::Result<Option<String>> {
    let canonical_root = project_root
        .canonicalize()
        .map_err(|err| anyhow::anyhow!("failed to resolve codewiki project root: {err}"))?;
    let source_path = project_root.join(file);
    let canonical_source = match source_path.canonicalize() {
        Ok(path) => path,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => {
            return Err(anyhow::anyhow!(
                "failed to resolve codewiki source file {file}: {err}"
            ));
        }
    };
    if !canonical_source.starts_with(&canonical_root) {
        anyhow::bail!("codewiki source file {file} resolves outside project root");
    }
    match hasher::file_content_hash(&canonical_source) {
        Ok(hash) => Ok(Some(hash)),
        Err(err)
            if err
                .downcast_ref::<std::io::Error>()
                .is_some_and(|io_err| io_err.kind() == std::io::ErrorKind::NotFound) =>
        {
            Ok(None)
        }
        Err(err) => Err(anyhow::anyhow!(
            "failed to hash codewiki source file {file}: {err}"
        )),
    }
}

fn graph_neighborhood_fingerprints(
    symbols: &BTreeMap<String, CodewikiSymbolSnapshot>,
    graph_edges: &[CodewikiGraphEdge],
) -> BTreeMap<String, String> {
    let mut neighborhoods = symbols
        .keys()
        .map(|id| (id.clone(), Vec::<String>::new()))
        .collect::<BTreeMap<_, _>>();
    for edge in graph_edges {
        let edge_key = format!(
            "{}:{}->{}",
            match edge.kind {
                CodewikiGraphEdgeKind::Call => "call",
                CodewikiGraphEdgeKind::Import => "import",
            },
            edge.source_component_id,
            edge.target_component_id
        );
        if let Some(source) = neighborhoods.get_mut(&edge.source_component_id) {
            source.push(edge_key.clone());
        }
        if let Some(target) = neighborhoods.get_mut(&edge.target_component_id) {
            target.push(edge_key);
        }
    }
    neighborhoods
        .into_iter()
        .map(|(id, mut edges)| {
            edges.sort();
            let joined = edges.join("\n");
            (id, hasher::content_hash(joined.as_bytes()))
        })
        .collect()
}
