use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use gobby_core::vault::links::canonical_target_key;

use crate::WikiError;
use crate::links::{LinkKind, extract_links};
use crate::synthesis::ArticleKind;

fn reserved_migration_dirs() -> [(&'static str, &'static str); 3] {
    [
        (
            ArticleKind::Concept.directory(),
            ArticleKind::Concept.reserved_suffix(),
        ),
        (
            ArticleKind::Topic.directory(),
            ArticleKind::Topic.reserved_suffix(),
        ),
        (
            ArticleKind::Source.directory(),
            ArticleKind::Source.reserved_suffix(),
        ),
    ]
}

pub(super) fn migrate_reserved_pages(
    vault_root: &Path,
    dry_run: bool,
    notes: &mut Vec<String>,
) -> Result<(), WikiError> {
    use gobby_core::vault::reserved::is_reserved_instruction_stem;

    let mut renames: Vec<(PathBuf, PathBuf)> = Vec::new();
    let mut planned_destinations: BTreeSet<PathBuf> = BTreeSet::new();
    for (directory, suffix) in reserved_migration_dirs() {
        let absolute = vault_root.join(directory);
        let Ok(entries) = fs::read_dir(&absolute) else {
            continue;
        };
        let mut paths: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).collect();
        paths.sort();
        for path in paths {
            if path.extension().and_then(|ext| ext.to_str()) != Some("md") {
                continue;
            }
            let Some(stem) = path.file_stem().and_then(|stem| stem.to_str()) else {
                continue;
            };
            if !is_reserved_instruction_stem(stem) {
                continue;
            }
            let base = format!("{}-{suffix}", stem.to_ascii_lowercase());
            let mut new_stem = None;
            for index in 1usize..=99 {
                let candidate = if index == 1 {
                    base.clone()
                } else {
                    format!("{base}-{index}")
                };
                let candidate_relative = PathBuf::from(directory).join(format!("{candidate}.md"));
                if absolute.join(format!("{candidate}.md")).exists()
                    || planned_destinations.contains(&candidate_relative)
                {
                    continue;
                }
                planned_destinations.insert(candidate_relative);
                new_stem = Some(candidate);
                break;
            }
            let Some(new_stem) = new_stem else {
                notes.push(format!(
                    "reserved-slug migration: no free name for {directory}/{stem}.md; left in place"
                ));
                continue;
            };
            renames.push((
                PathBuf::from(directory).join(format!("{stem}.md")),
                PathBuf::from(directory).join(format!("{new_stem}.md")),
            ));
        }
    }
    if renames.is_empty() {
        return Ok(());
    }

    if dry_run {
        for (old, new) in &renames {
            notes.push(format!(
                "reserved-slug migration (dry run): would rename {} -> {}",
                old.display(),
                new.display()
            ));
        }
        return Ok(());
    }

    let mut completed: Vec<(PathBuf, PathBuf)> = Vec::new();
    for (old, new) in &renames {
        if let Err(error) = fs::rename(vault_root.join(old), vault_root.join(new)) {
            if let Err(cleanup_error) =
                retarget_completed_reserved_renames(vault_root, &completed, notes)
            {
                notes.push(format!(
                    "reserved-slug migration: failed to retarget completed renames after rename failure: {cleanup_error}"
                ));
            }
            return Err(WikiError::Io {
                action: "rename reserved-slug page",
                path: Some(vault_root.join(old)),
                source: error,
            });
        }
        completed.push((old.clone(), new.clone()));
        notes.push(format!(
            "reserved-slug migration: renamed {} -> {} (agent instruction filename collision)",
            old.display(),
            new.display()
        ));
    }
    retarget_completed_reserved_renames(vault_root, &completed, notes)
}

fn retarget_completed_reserved_renames(
    vault_root: &Path,
    renames: &[(PathBuf, PathBuf)],
    notes: &mut Vec<String>,
) -> Result<(), WikiError> {
    if renames.is_empty() {
        return Ok(());
    }
    let rewritten = retarget_renamed_links(vault_root, renames)?;
    if rewritten > 0 {
        notes.push(format!(
            "reserved-slug migration: retargeted {rewritten} path-form links"
        ));
    }
    Ok(())
}

fn retarget_renamed_links(
    vault_root: &Path,
    renames: &[(PathBuf, PathBuf)],
) -> Result<usize, WikiError> {
    // Old canonical target key (with and without the .md suffix) -> new
    // extensionless vault path.
    let mut targets: BTreeMap<String, String> = BTreeMap::new();
    for (old, new) in renames {
        let old_page = old.to_string_lossy().replace('\\', "/");
        let old_stemless = old.with_extension("").to_string_lossy().replace('\\', "/");
        let new_stemless = new.with_extension("").to_string_lossy().replace('\\', "/");
        targets.insert(canonical_target_key(&old_page), new_stemless.clone());
        targets.insert(canonical_target_key(&old_stemless), new_stemless);
    }

    let mut markdown_files: Vec<PathBuf> = Vec::new();
    collect_retarget_files(vault_root, vault_root, &mut markdown_files)?;
    markdown_files.sort();

    let mut rewritten = 0usize;
    for path in markdown_files {
        let Ok(markdown) = fs::read_to_string(&path) else {
            continue;
        };
        let links = extract_links(&markdown, std::iter::empty::<&str>());
        let mut edits: Vec<(usize, usize, String)> = Vec::new();
        for link in &links {
            let Some(new_target) = targets.get(&canonical_target_key(&link.normalized_target))
            else {
                continue;
            };
            let keeps_extension = link.target.to_ascii_lowercase().ends_with(".md");
            let destination = if keeps_extension {
                format!("{new_target}.md")
            } else {
                new_target.clone()
            };
            let anchor = link
                .anchor
                .as_deref()
                .map(|anchor| format!("#{anchor}"))
                .unwrap_or_default();
            let replacement = match link.kind {
                LinkKind::Wikilink => match link.alias.as_deref() {
                    Some(alias) => format!("[[{destination}{anchor}|{alias}]]"),
                    None => format!("[[{destination}{anchor}]]"),
                },
                LinkKind::Markdown => format!(
                    "[{}]({destination}{anchor})",
                    link.alias.as_deref().unwrap_or(new_target)
                ),
            };
            edits.push((link.byte_start, link.byte_end, replacement));
        }
        if edits.is_empty() {
            continue;
        }
        let mut updated = markdown.clone();
        for (byte_start, byte_end, replacement) in edits.into_iter().rev() {
            updated.replace_range(byte_start..byte_end, &replacement);
            rewritten += 1;
        }
        fs::write(&path, updated).map_err(|error| WikiError::Io {
            action: "retarget renamed page links",
            path: Some(path.clone()),
            source: error,
        })?;
    }
    Ok(rewritten)
}

fn collect_retarget_files(
    vault_root: &Path,
    directory: &Path,
    files: &mut Vec<PathBuf>,
) -> Result<(), WikiError> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Ok(());
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if path.is_dir() {
            let is_vault_top_level = directory == vault_root;
            if name.starts_with('.')
                || (is_vault_top_level && (name == "raw" || name == gobby_core::vault::STATE_ROOT))
            {
                continue;
            }
            collect_retarget_files(vault_root, &path, files)?;
        } else if path.extension().and_then(|ext| ext.to_str()) == Some("md") {
            files.push(path);
        }
    }
    Ok(())
}
