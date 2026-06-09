use super::super::{CodewikiIndexSnapshot, CodewikiSymbolSnapshot};
use serde::Serialize;
use std::collections::BTreeSet;

pub(crate) fn build_codewiki_changes_doc(
    previous: Option<&CodewikiIndexSnapshot>,
    current: &CodewikiIndexSnapshot,
) -> String {
    let baseline = previous.is_none();
    let degraded = !current.degraded_sources.is_empty();
    let mut doc = changes_frontmatter(baseline, degraded, &current.degraded_sources);
    doc.push_str("# Index Changes\n\n");
    doc.push_str("## Current Snapshot\n\n");
    doc.push_str(&format!("- Files: {}\n", current.files.len()));
    doc.push_str(&format!("- Symbols: {}\n", current.symbols.len()));
    match &current.graph_neighborhoods {
        Some(neighborhoods) => {
            doc.push_str(&format!("- Graph neighborhoods: {}\n", neighborhoods.len()));
        }
        None => doc.push_str("- Graph neighborhoods: unavailable\n"),
    }
    doc.push('\n');

    let Some(previous) = previous else {
        doc.push_str("No previous index snapshot was available.\n");
        doc.push_str("This page is the baseline for future index changes.\n");
        return doc;
    };

    let added_files = current
        .files
        .keys()
        .filter(|file| !previous.files.contains_key(*file))
        .cloned()
        .collect::<Vec<_>>();
    let removed_files = previous
        .files
        .keys()
        .filter(|file| !current.files.contains_key(*file))
        .cloned()
        .collect::<Vec<_>>();
    let changed_files = current
        .files
        .iter()
        .filter(|(file, current_file)| {
            previous.files.get(*file).is_some_and(|previous_file| {
                previous_file.content_hash != current_file.content_hash
            })
        })
        .map(|(file, _)| file.clone())
        .collect::<Vec<_>>();
    let new_symbols = current
        .symbols
        .iter()
        .filter(|(id, _)| !previous.symbols.contains_key(*id))
        .map(|(_, symbol)| symbol_label(symbol))
        .collect::<Vec<_>>();
    let removed_symbols = previous
        .symbols
        .iter()
        .filter(|(id, _)| !current.symbols.contains_key(*id))
        .map(|(_, symbol)| symbol_label(symbol))
        .collect::<Vec<_>>();

    write_bullet_section(&mut doc, "Added Files", added_files, "`", "`");
    write_bullet_section(&mut doc, "Removed Files", removed_files, "`", "`");
    write_bullet_section(&mut doc, "Changed Files", changed_files, "`", "`");
    write_bullet_section(&mut doc, "New Symbols", new_symbols, "", "");
    write_bullet_section(&mut doc, "Removed Symbols", removed_symbols, "", "");

    if let (Some(previous_graph), Some(current_graph)) =
        (&previous.graph_neighborhoods, &current.graph_neighborhoods)
    {
        let graph_ids = previous_graph
            .keys()
            .chain(current_graph.keys())
            .cloned()
            .collect::<BTreeSet<_>>();
        let changed_graph = graph_ids
            .into_iter()
            .filter(|id| previous_graph.get(id) != current_graph.get(id))
            .map(|id| {
                current
                    .symbols
                    .get(&id)
                    .or_else(|| previous.symbols.get(&id))
                    .map(symbol_label)
                    .unwrap_or_else(|| format!("`{id}`"))
            })
            .collect::<Vec<_>>();
        write_bullet_section(
            &mut doc,
            "Changed Graph Neighborhoods",
            changed_graph,
            "",
            "",
        );
    }

    doc
}

#[derive(Serialize)]
struct ChangesFrontmatter<'a> {
    title: &'a str,
    kind: &'a str,
    generated_by: &'a str,
    trust: &'a str,
    freshness: &'a str,
    baseline: bool,
    degraded: bool,
    degraded_sources: Vec<&'a str>,
}

fn changes_frontmatter(baseline: bool, degraded: bool, degraded_sources: &[String]) -> String {
    let data = ChangesFrontmatter {
        title: "Index Changes",
        kind: "code_changes",
        generated_by: "gcode-codewiki",
        trust: "generated",
        freshness: "indexed",
        baseline,
        degraded,
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
    };
    let yaml = serde_yaml::to_string(&data)
        .expect("codewiki changes frontmatter only contains YAML-serializable data");
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);
    let mut out = String::from("---\n");
    out.push_str(yaml);
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push_str("---\n\n");
    out
}

fn write_bullet_section(
    doc: &mut String,
    title: &str,
    items: Vec<String>,
    prefix: &str,
    suffix: &str,
) {
    doc.push_str(&format!("## {title}\n\n"));
    if items.is_empty() {
        doc.push_str("- None\n\n");
        return;
    }
    for item in items {
        doc.push_str(&format!("- {prefix}{item}{suffix}\n"));
    }
    doc.push('\n');
}

fn symbol_label(symbol: &CodewikiSymbolSnapshot) -> String {
    format!(
        "`{}` {} in `{}`",
        symbol.qualified_name, symbol.kind, symbol.file_path
    )
}
