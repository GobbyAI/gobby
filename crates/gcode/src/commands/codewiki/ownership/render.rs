use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write;

use serde::Serialize;

use super::super::{file_wikilink, module_wikilink};
use super::analysis::retain_deterministic_identity;
use super::{FileOwnership, OwnershipContributor, OwnershipStatus};

pub(super) fn degraded_sources(
    status: &OwnershipStatus,
    by_file: &BTreeMap<String, FileOwnership>,
) -> Vec<String> {
    let mut sources = Vec::new();
    if !status.codeowners_available {
        sources.push("codeowners_unavailable".to_string());
    }
    if !status.blame_available {
        sources.push("git_blame_unavailable".to_string());
    }
    if status.blame_errors {
        sources.push("git_blame_errors".to_string());
    }
    if status.partial {
        sources.push("git_blame_partial".to_string());
    }
    if by_file
        .values()
        .all(|ownership| ownership.declared.is_empty() && ownership.derived.is_empty())
    {
        sources.push("ownership_unknown".to_string());
    }
    sources
}

pub(super) fn ownership_frontmatter(partial: bool, degraded_sources: &[String]) -> String {
    #[derive(Serialize)]
    struct Frontmatter<'a> {
        title: &'static str,
        #[serde(rename = "type")]
        kind: &'static str,
        provenance: Vec<&'a str>,
        generated_by: &'static str,
        trust: &'static str,
        freshness: &'static str,
        #[serde(skip_serializing_if = "Option::is_none")]
        degraded: Option<bool>,
        #[serde(skip_serializing_if = "Vec::is_empty")]
        degraded_sources: Vec<&'a str>,
        #[serde(skip_serializing_if = "is_false")]
        partial: bool,
    }

    let data = Frontmatter {
        title: "Code Ownership",
        kind: "code_ownership",
        provenance: Vec::new(),
        generated_by: gobby_core::codewiki_contract::GENERATED_BY_CODEWIKI,
        trust: gobby_core::codewiki_contract::TRUST_GENERATED,
        freshness: gobby_core::codewiki_contract::FRESHNESS_INDEXED,
        degraded: (!degraded_sources.is_empty()).then_some(true),
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
        partial,
    };
    let yaml = serde_yaml::to_string(&data).expect("ownership frontmatter is YAML-serializable");
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);
    format!("---\n{yaml}---\n\n")
}

fn is_false(value: &bool) -> bool {
    !*value
}

pub(super) fn write_modules(
    doc: &mut String,
    by_file: &BTreeMap<String, FileOwnership>,
    file_modules: &HashMap<String, String>,
) {
    let mut modules: BTreeMap<String, Vec<(&String, &FileOwnership)>> = BTreeMap::new();
    for (file, ownership) in by_file {
        modules
            .entry(file_modules.get(file).cloned().unwrap_or_default())
            .or_default()
            .push((file, ownership));
    }
    doc.push_str("## Modules\n\n");
    for (module, files) in modules {
        let label = if module.is_empty() {
            "[[code/repo|Repository Overview]]".to_string()
        } else {
            module_wikilink(&module)
        };
        let primary = aggregate_primary(&files);
        let contributors = aggregate_contributors(&files);
        let _ = writeln!(doc, "### Module: {label}\n");
        write_owner_line(doc, "Primary owners", &primary);
        write_contributor_line(doc, &contributors);
        doc.push('\n');
    }
}

pub(super) fn write_files(doc: &mut String, by_file: &BTreeMap<String, FileOwnership>) {
    doc.push_str("## Files\n\n");
    for (file, ownership) in by_file {
        let _ = writeln!(doc, "### {}\n", file_wikilink(file));
        if ownership.declared.is_empty() && ownership.derived.is_empty() {
            doc.push_str("Unknown ownership.\n\n");
            continue;
        }
        write_owner_line(doc, "Declared owners", &ownership.declared);
        write_contributor_line(doc, &ownership.derived);
        doc.push('\n');
    }
}

fn aggregate_primary(files: &[(&String, &FileOwnership)]) -> Vec<String> {
    let mut owners = BTreeSet::new();
    for (_, ownership) in files {
        if !ownership.declared.is_empty() {
            owners.extend(ownership.declared.iter().cloned());
        } else if let Some(top) = ownership.derived.first() {
            owners.insert(top.name.clone());
        }
    }
    owners.into_iter().collect()
}

fn aggregate_contributors(files: &[(&String, &FileOwnership)]) -> Vec<OwnershipContributor> {
    let mut counts: BTreeMap<String, (String, Option<String>, usize)> = BTreeMap::new();
    for (_, ownership) in files {
        for contributor in &ownership.derived {
            counts
                .entry(contributor.contributor_id.clone())
                .and_modify(|(stored_name, stored_email, lines)| {
                    retain_deterministic_identity(
                        stored_name,
                        stored_email,
                        &contributor.name,
                        &contributor.email,
                    );
                    *lines += contributor.lines;
                })
                .or_insert_with(|| {
                    (
                        contributor.name.clone(),
                        contributor.email.clone(),
                        contributor.lines,
                    )
                });
        }
    }
    let mut contributors = counts
        .into_iter()
        .map(
            |(contributor_id, (name, email, lines))| OwnershipContributor {
                contributor_id,
                name,
                email,
                lines,
            },
        )
        .collect::<Vec<_>>();
    contributors.sort_by(|left, right| {
        right
            .lines
            .cmp(&left.lines)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.contributor_id.cmp(&right.contributor_id))
    });
    contributors.truncate(5);
    contributors
}

fn write_owner_line(doc: &mut String, label: &str, owners: &[String]) {
    if owners.is_empty() {
        let _ = writeln!(doc, "{label}: unknown");
    } else {
        let _ = writeln!(doc, "{label}: {}", owners.join(", "));
    }
}

fn write_contributor_line(doc: &mut String, contributors: &[OwnershipContributor]) {
    if contributors.is_empty() {
        doc.push_str("Top contributors: unknown\n");
        return;
    }
    let rendered = contributors
        .iter()
        .map(|contributor| {
            format!(
                "{} ({} {})",
                contributor.name,
                contributor.lines,
                if contributor.lines == 1 {
                    "line"
                } else {
                    "lines"
                }
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    let _ = writeln!(doc, "Top contributors: {rendered}");
}
