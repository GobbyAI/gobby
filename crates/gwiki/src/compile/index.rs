use std::fs;
use std::fs::OpenOptions;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use crate::WikiError;
use crate::citations::{source_record_matches_path, source_records_for_paths};
use crate::provenance::{ProvenanceGraph, ProvenanceLink, SourceChunkRef, WikiSectionRef};
use crate::sources::{CompileStatus, SourceManifest};
use crate::synthesis::{SynthesizedPage, relative_path, slugify as page_slugify, wiki_link};

use super::*;

pub(crate) fn update_wiki_index(
    vault_root: &Path,
    article: &SynthesizedPage,
) -> Result<(), WikiError> {
    let lock_path = vault_root.join(".gwiki").join("index.lock");
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create wiki index lock directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open wiki index lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    lock_wiki_index(&lock, &lock_path)?;

    let index_path = vault_root.join("_index.md");
    let mut index = if index_path.exists() {
        fs::read_to_string(&index_path).map_err(|error| WikiError::Io {
            action: "read wiki index",
            path: Some(index_path.clone()),
            source: error,
        })?
    } else {
        "# Wiki Index\n\n".to_string()
    };

    let link = wiki_link(vault_root, &article.path, &article.title);
    if !has_compiled_page_link(&index, &link) {
        insert_compiled_page_link(&mut index, &link)?;
    }

    fs::write(&index_path, index).map_err(|error| WikiError::Io {
        action: "write wiki index",
        path: Some(index_path),
        source: error,
    })?;
    Ok(())
}

pub(super) fn insert_compiled_page_link(index: &mut String, link: &str) -> Result<(), WikiError> {
    let heading = "## Compiled pages";
    if !has_compiled_pages_heading(index) {
        if !index.is_empty() {
            if !index.ends_with('\n') {
                index.push('\n');
            }
            index.push('\n');
        }
        index.push_str(heading);
        index.push_str("\n\n");
    }
    let section_body_start = exact_line_end(index, heading).ok_or_else(|| WikiError::Config {
        detail: "wiki index is missing ## Compiled pages heading".to_string(),
    })?;
    let mut insertion_point =
        next_section_heading_offset(index, section_body_start).unwrap_or(index.len());

    if insertion_point > 0 && !index[..insertion_point].ends_with('\n') {
        index.insert(insertion_point, '\n');
        insertion_point += 1;
    }

    let mut entry = format!("- {link}\n");
    if insertion_point < index.len() && !index[insertion_point..].starts_with('\n') {
        entry.push('\n');
    }
    index.insert_str(insertion_point, &entry);
    Ok(())
}

fn has_compiled_pages_heading(index: &str) -> bool {
    has_exact_line(index, "## Compiled pages")
}

fn has_compiled_page_link(index: &str, link: &str) -> bool {
    has_exact_line(index, &format!("- {link}"))
}

fn has_exact_line(markdown: &str, expected: &str) -> bool {
    markdown.lines().any(|line| line == expected)
}

fn exact_line_end(markdown: &str, expected: &str) -> Option<usize> {
    let mut offset = 0;
    for line in markdown.split_inclusive('\n') {
        if line_body(line) == expected {
            return Some(offset + line.len());
        }
        offset += line.len();
    }
    None
}

fn next_section_heading_offset(markdown: &str, start: usize) -> Option<usize> {
    let mut offset = start;
    for line in markdown[start..].split_inclusive('\n') {
        if line_body(line).starts_with("## ") {
            return Some(offset);
        }
        offset += line.len();
    }
    None
}

fn line_body(line: &str) -> &str {
    line.trim_end_matches('\n').trim_end_matches('\r')
}

pub(crate) fn write_provenance(
    vault_root: &Path,
    article: &SynthesizedPage,
    sources: &[AcceptedCompileSource],
) -> Result<(), WikiError> {
    let _lock = lock_provenance(vault_root)?;
    let provenance_path = vault_root.join("meta").join("provenance.json");
    let mut graph = if provenance_path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)?
    } else {
        ProvenanceGraph::default()
    };
    let heading =
        first_rendered_article_section(&article.markdown).unwrap_or_else(|| "Overview".to_string());
    let section_id = if heading == "Overview" {
        page_slugify(&article.title)
    } else {
        page_slugify(&heading)
    };
    let section = WikiSectionRef {
        page_path: PathBuf::from(relative_path(vault_root, &article.path)),
        heading,
        section_id,
    };
    let manifest_records = source_records_for_paths(
        vault_root,
        &sources
            .iter()
            .map(|source| source.path.clone())
            .collect::<Vec<_>>(),
    )?;

    for source in sources {
        let source_id = manifest_records
            .iter()
            .find(|record| source_record_matches_path(record, vault_root, &source.path))
            .map(|record| record.id.clone())
            .unwrap_or_else(|| page_slugify(&source.title));
        for (index, chunk) in source.chunks.iter().enumerate() {
            let offset =
                source
                    .chunk_offsets
                    .get(index)
                    .cloned()
                    .unwrap_or(AcceptedCompileChunkOffset {
                        byte_start: 0,
                        byte_end: chunk.len(),
                    });
            graph.add_link(ProvenanceLink {
                source: SourceChunkRef {
                    source_id: source_id.clone(),
                    chunk_id: format!("{source_id}#chunk-{index}"),
                    path: PathBuf::from(relative_path(vault_root, &source.path)),
                    byte_start: offset.byte_start,
                    byte_end: offset.byte_end,
                },
                section: section.clone(),
                claim: Some(chunk.clone()),
            });
        }
    }

    graph.save_to_vault(vault_root)
}

fn lock_provenance(vault_root: &Path) -> Result<fs::File, WikiError> {
    let lock_path = vault_root.join(".gwiki").join("provenance.lock");
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create provenance lock directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open provenance lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    lock_file(&lock, &lock_path, "lock provenance")?;
    Ok(lock)
}

fn first_rendered_article_section(markdown: &str) -> Option<String> {
    markdown.lines().find_map(|line| {
        line.strip_prefix("## ")
            .map(str::trim)
            .filter(|heading| !heading.is_empty())
            .map(ToString::to_string)
    })
}

pub(crate) fn mark_sources_compiled(
    vault_root: &Path,
    source_paths: &[PathBuf],
) -> Result<(), WikiError> {
    SourceManifest::update(vault_root, |manifest| {
        let mut changed = false;
        for entry in &mut manifest.entries {
            if source_paths
                .iter()
                .any(|path| source_record_matches_path(entry, vault_root, path))
                && entry.compile_status != CompileStatus::Compiled
            {
                entry.compile_status = CompileStatus::Compiled;
                changed = true;
            }
        }
        Ok(changed)
    })
}

fn lock_wiki_index(lock: &fs::File, lock_path: &Path) -> Result<(), WikiError> {
    lock_file(lock, lock_path, "lock wiki index")
}

fn lock_file(lock: &fs::File, lock_path: &Path, action: &'static str) -> Result<(), WikiError> {
    let timeout = index_lock_timeout();
    let started = Instant::now();

    loop {
        match fs4::FileExt::try_lock(lock) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= timeout {
                    // Returning drops the lock file handle, which releases any
                    // fs4 lock acquired by this process through RAII.
                    return Err(WikiError::Io {
                        action,
                        path: Some(lock_path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", timeout.as_millis()),
                        ),
                    });
                }
                thread::sleep(Duration::from_millis(25).min(timeout - elapsed));
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action,
                    path: Some(lock_path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_compiled_page_link_creates_missing_section() {
        let mut index = "# Wiki Index\n\n## Notes\n\n- [[Existing]]\n".to_string();

        insert_compiled_page_link(&mut index, "[[Compiled/Page]]").expect("insert link");

        assert!(index.contains("## Compiled pages\n\n- [[Compiled/Page]]\n"));
        assert!(index.contains("## Notes\n\n- [[Existing]]"));
    }
}
