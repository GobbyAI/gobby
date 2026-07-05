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
use crate::synthesis::{SynthesizedPage, relative_path, slugify as page_slugify};

use super::*;

pub(crate) fn write_provenance(
    vault_root: &Path,
    article: &SynthesizedPage,
    sources: &[AcceptedCompileSource],
    outline: &[String],
) -> Result<(), WikiError> {
    let _lock = lock_provenance(vault_root)?;
    let provenance_path = vault_root.join("meta").join("provenance.json");
    let mut graph = if provenance_path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)?
    } else {
        ProvenanceGraph::default()
    };
    let sections = provenance_sections(
        PathBuf::from(relative_path(vault_root, &article.path)),
        article,
        outline,
    );
    let manifest_records = source_records_for_paths(
        vault_root,
        &sources
            .iter()
            .map(|source| source.path.clone())
            .collect::<Vec<_>>(),
    )?;

    let mut chunk_ordinal = 0;
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
                section: section_for_chunk(&sections, chunk_ordinal).clone(),
                claim: Some(chunk.clone()),
            });
            chunk_ordinal += 1;
        }
    }

    graph.save_to_vault(vault_root)
}

fn lock_provenance(vault_root: &Path) -> Result<fs::File, WikiError> {
    let lock_path = vault_root
        .join(crate::vault::STATE_ROOT)
        .join("provenance.lock");
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

fn provenance_sections(
    page_path: PathBuf,
    article: &SynthesizedPage,
    outline: &[String],
) -> Vec<WikiSectionRef> {
    let mut headings: Vec<String> = outline
        .iter()
        .map(|heading| heading.trim())
        .filter(|heading| !heading.is_empty())
        .map(ToString::to_string)
        .collect();
    if headings.is_empty() {
        headings = rendered_article_sections(&article.markdown);
    }
    if headings.is_empty() {
        headings.push("Overview".to_string());
    }

    headings
        .into_iter()
        .map(|heading| WikiSectionRef {
            section_id: section_id_for_article(article, &heading),
            heading,
            page_path: page_path.clone(),
        })
        .collect()
}

fn section_for_chunk(sections: &[WikiSectionRef], chunk_ordinal: usize) -> &WikiSectionRef {
    let index = chunk_ordinal.min(sections.len().saturating_sub(1));
    &sections[index]
}

fn rendered_article_sections(markdown: &str) -> Vec<String> {
    markdown
        .lines()
        .filter_map(|line| {
            line.strip_prefix("## ")
                .map(str::trim)
                .filter(|heading| !heading.is_empty())
                .map(ToString::to_string)
        })
        .collect()
}

fn section_id_for_article(article: &SynthesizedPage, heading: &str) -> String {
    if heading == "Overview" {
        page_slugify(&article.title)
    } else {
        page_slugify(heading)
    }
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

pub(crate) fn lock_file(
    lock: &fs::File,
    lock_path: &Path,
    action: &'static str,
) -> Result<(), WikiError> {
    let timeout = index_lock_timeout();
    let started = Instant::now();

    loop {
        // fs4 names the exclusive lock attempt `try_lock`; shared locking is
        // exposed separately as `try_lock_shared`.
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
