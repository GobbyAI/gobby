//! Launch-owned agent workspace authority; never accepted from serialized Sandbox config.

use super::{PolicyInputs, paths};
use crate::casefold::casefold;
use anyhow::{Context, Result, bail};
use std::path::{Path, PathBuf};

pub(super) struct WorkspaceScope {
    pub(super) root: PathBuf,
    pub(super) reads: Vec<PathBuf>,
    pub(super) writes: Vec<PathBuf>,
}

fn within(path: &Path, parent: &Path) -> bool {
    let mut parts = path.components();
    parent.components().all(|expected| {
        parts.next().is_some_and(|actual| {
            casefold(&actual.as_os_str().to_string_lossy())
                == casefold(&expected.as_os_str().to_string_lossy())
        })
    })
}

fn checked_scope(items: Vec<PathBuf>, lexical_root: &Path, root: &Path) -> Result<Vec<PathBuf>> {
    items
        .into_iter()
        .map(|path| {
            if paths::text(&path).contains(['*', '?', '[', '{']) {
                bail!(
                    "agent workspace scope contains glob syntax: {}",
                    path.display()
                );
            }
            let relative = path
                .strip_prefix(lexical_root)
                .context("agent workspace scope is outside agents_workspaces")?;
            let expected = if relative.as_os_str().is_empty() {
                root.to_path_buf()
            } else {
                root.join(relative)
            };
            if paths::resolve_nonstrict(&path) != expected {
                bail!("agent workspace scope follows an alias: {}", path.display());
            }
            Ok(expected)
        })
        .collect()
}

impl WorkspaceScope {
    pub(super) fn resolve(inputs: &PolicyInputs) -> Result<Self> {
        let lexical_root = inputs.gobby_home.join("agents_workspaces");
        let root = paths::resolve_nonstrict(&lexical_root);
        let mut writes = Vec::new();
        let mut reads = Vec::new();
        if let Some(session_seq) = inputs.session_seq {
            let anchor = lexical_root.join(&inputs.project_id);
            let definition = inputs
                .definition
                .as_deref()
                .filter(|name| !name.is_empty())
                .unwrap_or("default");
            writes.push(
                anchor
                    .join(definition)
                    .join(format!("session-{session_seq}")),
            );
            if let Some(epic_seq) = inputs.epic_seq {
                writes.push(anchor.join("epics").join(format!("epic-{epic_seq}")));
            }
            reads.clone_from(&writes);
            reads.push(anchor.join("README.md"));
            if inputs.read_all_agent_workspaces {
                reads = vec![lexical_root.clone()];
            } else {
                reads.extend(inputs.predecessor_workspace_folders.iter().cloned());
            }
        }
        Ok(Self {
            reads: checked_scope(reads, &lexical_root, &root)?,
            writes: checked_scope(writes, &lexical_root, &root)?,
            root,
        })
    }

    pub(super) fn restrict(&self, reads: Vec<String>, writes: &[String]) -> Result<Vec<String>> {
        // Linux restores write binds beneath denyRead roots. Check them independently.
        for item in writes {
            let path = Path::new(item);
            if (within(path, &self.root) || within(&self.root, path))
                && !self.writes.iter().any(|folder| path.starts_with(folder))
            {
                bail!(
                    "write grant exceeds agent workspace scope: {}",
                    path.display()
                );
            }
        }
        Ok(paths::dedupe(
            reads
                .into_iter()
                .filter(|item| {
                    let path = Path::new(item);
                    !(within(path, &self.root) || within(&self.root, path))
                })
                .chain(self.reads.iter().map(|path| paths::text(path))),
        ))
    }
}

#[cfg(test)]
#[path = "workspace_scope/tests.rs"]
mod tests;
