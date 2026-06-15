use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::time::Duration;

use serde::{Deserialize, Serialize};

mod analysis;
mod codeowners;
mod render;

#[cfg(test)]
use analysis::content_hash;
use analysis::derived_owners_for_files;
#[cfg(all(test, unix))]
use analysis::git_blame_output_with_timeout;
use codeowners::{declared_owners_for_files, read_codeowners};
use render::{degraded_sources, ownership_frontmatter, write_files, write_modules};

#[derive(Debug, Clone)]
pub(crate) struct OwnershipOptions {
    pub blame_file_cap: usize,
    pub blame_timeout: Duration,
}

impl Default for OwnershipOptions {
    fn default() -> Self {
        Self {
            blame_file_cap: 200,
            blame_timeout: Duration::from_secs(20),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct OwnershipMeta {
    #[serde(default)]
    pub files: BTreeMap<String, CachedBlameSummary>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct CachedBlameSummary {
    pub content_hash: String,
    pub contributors: Vec<OwnershipContributor>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct OwnershipContributor {
    pub contributor_id: String,
    pub name: String,
    #[serde(default, skip_serializing)]
    pub email: Option<String>,
    pub lines: usize,
}

#[derive(Debug, Default)]
struct OwnershipStatus {
    codeowners_available: bool,
    blame_available: bool,
    blame_errors: bool,
    partial: bool,
}

#[derive(Debug, Default)]
struct FileOwnership {
    declared: Vec<String>,
    derived: Vec<OwnershipContributor>,
}

pub(crate) fn build_ownership_doc(
    project_root: &Path,
    files: &[String],
    file_modules: &HashMap<String, String>,
    meta: &mut OwnershipMeta,
    options: OwnershipOptions,
) -> anyhow::Result<String> {
    let codeowners = read_codeowners(project_root)?;
    let mut status = OwnershipStatus {
        codeowners_available: codeowners.is_some(),
        ..OwnershipStatus::default()
    };
    let declared = declared_owners_for_files(codeowners.as_ref(), files);
    let derived = derived_owners_for_files(project_root, files, meta, &options, &mut status);

    let mut by_file = BTreeMap::new();
    for file in files {
        by_file.insert(
            file.clone(),
            FileOwnership {
                declared: declared.get(file).cloned().unwrap_or_default(),
                derived: derived.get(file).cloned().unwrap_or_default(),
            },
        );
    }
    let degraded_sources = degraded_sources(&status, &by_file);
    let mut doc = ownership_frontmatter(status.partial, &degraded_sources);
    doc.push_str("# Code Ownership\n\n");
    if status.partial {
        doc.push_str(
            "> Ownership is partial because blame hit the configured file cap or timeout.\n\n",
        );
    }

    if by_file
        .values()
        .all(|ownership| ownership.declared.is_empty() && ownership.derived.is_empty())
    {
        doc.push_str("Unknown ownership. No CODEOWNERS declarations or git blame contributors were available.\n");
        return Ok(doc);
    }

    write_modules(&mut doc, &by_file, file_modules);
    write_files(&mut doc, &by_file);
    Ok(doc)
}

#[cfg(all(test, unix))]
#[path = "ownership_timeout_tests.rs"]
mod ownership_timeout_tests;

#[cfg(test)]
mod tests;
