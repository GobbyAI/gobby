use super::strict_markdown;
use super::text::{preserve_commit_lines, strip_commit_lines};
use std::io::Write;
use std::path::{Path, PathBuf};

/// On-disk `.md` pages under the codewiki-owned `code/` tree, as out-dir-relative
/// slash paths (e.g. `code/narrative/01-introduction.md`). Drives `finish`'s
/// cache-independent orphan GC (#900): a page on disk but absent from this run's
/// `seen` set is reclaimed even when the meta log never listed it. Scoped to
/// `code/` so the rest of the vault — the gwiki research notes, `.obsidian/`,
/// `_meta/` — is never walked. Symlinks are not followed and never returned,
/// matching `reject_symlinked_doc_path`.
pub(crate) fn collect_generated_doc_pages(out_dir: &Path) -> anyhow::Result<Vec<String>> {
    let code_root = out_dir.join("code");
    if !code_root.is_dir() {
        return Ok(Vec::new());
    }
    let mut pages = Vec::new();
    let mut stack = vec![code_root];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let file_type = entry.file_type()?;
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                stack.push(path);
            } else if file_type.is_file()
                && path.extension().is_some_and(|ext| ext == "md")
                && let Ok(rel) = path.strip_prefix(out_dir)
            {
                pages.push(
                    rel.to_string_lossy()
                        .replace(std::path::MAIN_SEPARATOR, "/"),
                );
            }
        }
    }
    Ok(pages)
}

pub(crate) fn scoped_file_doc(doc_path: &str) -> Option<&str> {
    doc_path
        .strip_prefix("code/files/")
        .and_then(|path| path.strip_suffix(".md"))
}

pub(crate) fn scoped_module_doc(doc_path: &str) -> Option<&str> {
    doc_path
        .strip_prefix("code/modules/")
        .and_then(|path| path.strip_suffix(".md"))
}

pub(crate) fn write_doc(out_dir: &Path, relative_path: &str, content: &str) -> anyhow::Result<()> {
    write_doc_before_persist(out_dir, relative_path, content, |_, _| Ok(()))
}

pub(super) fn write_doc_before_persist<F>(
    out_dir: &Path,
    relative_path: &str,
    content: &str,
    before_persist: F,
) -> anyhow::Result<()>
where
    F: FnOnce(&Path, &Path) -> anyhow::Result<()>,
{
    let target = safe_doc_path(out_dir, relative_path)?;
    reject_symlinked_doc_path(out_dir, &target)?;
    let parent = target.parent().ok_or_else(|| {
        anyhow::anyhow!("codewiki document path has no parent: {}", target.display())
    })?;
    std::fs::create_dir_all(parent)?;
    let content = if Path::new(relative_path)
        .extension()
        .and_then(|extension| extension.to_str())
        == Some("md")
    {
        strict_markdown::normalize_codewiki_markdown(content)
    } else {
        content.to_string()
    };

    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(content.as_bytes())?;
    temporary.as_file_mut().flush()?;
    before_persist(temporary.path(), &target)?;
    reject_symlinked_doc_path(out_dir, &target)?;
    temporary.persist(&target).map_err(|error| error.error)?;
    Ok(())
}

pub(super) fn refresh_doc_if_needed(
    out_dir: &Path,
    relative_path: &str,
    content: &str,
) -> anyhow::Result<bool> {
    let target = safe_doc_path(out_dir, relative_path)?;
    let existing = match std::fs::read_to_string(&target) {
        Ok(existing) => existing,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    let existing_without_stamp = strip_commit_lines(&existing);
    let content_without_stamp = strip_commit_lines(content);
    if existing_without_stamp == content_without_stamp {
        return Ok(false);
    }
    if !relative_path.ends_with(".md")
        || strict_markdown::normalize_codewiki_markdown(&existing_without_stamp)
            != content_without_stamp
    {
        return Ok(false);
    }
    let refreshed = preserve_commit_lines(&existing, &content_without_stamp);
    write_doc(out_dir, relative_path, &refreshed)?;
    Ok(true)
}

pub(crate) fn reject_symlinked_doc_path(out_dir: &Path, target: &Path) -> anyhow::Result<()> {
    let relative = target.strip_prefix(out_dir)?;
    let mut current = out_dir.to_path_buf();
    for component in relative.components() {
        current.push(component);
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                anyhow::bail!(
                    "refusing to follow symlinked codewiki path: {}",
                    current.display()
                );
            }
            Ok(_) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

pub(crate) fn prune_empty_doc_dirs(out_dir: &Path, target: &Path) -> anyhow::Result<()> {
    let mut current = target.parent();
    while let Some(dir) = current {
        if dir == out_dir {
            break;
        }
        match std::fs::remove_dir(dir) {
            Ok(()) => current = dir.parent(),
            Err(err)
                if matches!(
                    err.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::DirectoryNotEmpty
                ) =>
            {
                break;
            }
            Err(err) => {
                log::warn!(
                    "failed to prune empty codewiki directory {}: {err}",
                    dir.display()
                );
                break;
            }
        }
    }
    Ok(())
}

pub(crate) fn safe_doc_path(out_dir: &Path, relative_path: &str) -> anyhow::Result<PathBuf> {
    let path = Path::new(relative_path);
    if path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        anyhow::bail!("refusing to write unsafe codewiki path: {relative_path}");
    }
    Ok(out_dir.join(path))
}
