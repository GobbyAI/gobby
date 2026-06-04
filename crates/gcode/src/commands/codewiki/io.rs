use super::*;

pub fn write_doc_set(out_dir: &Path, docs: &[(String, String)]) -> anyhow::Result<()> {
    std::fs::create_dir_all(out_dir)?;
    for (relative_path, content) in docs {
        write_doc(out_dir, relative_path, content)?;
    }
    Ok(())
}

pub fn write_incremental_doc_set(
    project_root: &Path,
    out_dir: &Path,
    docs: &[(String, String)],
) -> anyhow::Result<Vec<String>> {
    std::fs::create_dir_all(out_dir)?;
    let previous = read_codewiki_meta(out_dir)?;
    let mut next_docs = BTreeMap::new();
    let mut generated_docs = Vec::new();

    for (relative_path, content) in docs {
        let doc_meta = CodewikiDocMeta {
            source_hashes: source_hashes_for_doc(project_root, content)?,
        };
        let target = safe_doc_path(out_dir, relative_path)?;
        let unchanged = target.exists()
            && previous
                .docs
                .get(relative_path)
                .is_some_and(|previous_meta| previous_meta == &doc_meta);

        if !unchanged {
            write_doc(out_dir, relative_path, content)?;
            generated_docs.push(relative_path.clone());
        }
        next_docs.insert(relative_path.clone(), doc_meta);
    }

    for stale_path in previous
        .docs
        .keys()
        .filter(|key| !next_docs.contains_key(*key))
    {
        let target = safe_doc_path(out_dir, stale_path)?;
        reject_symlinked_doc_path(out_dir, &target)?;
        match std::fs::remove_file(&target) {
            Ok(()) => prune_empty_doc_dirs(out_dir, &target)?,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }

    let meta = CodewikiMeta {
        docs: next_docs,
        generated_docs: generated_docs.clone(),
    };
    write_codewiki_meta(out_dir, &meta)?;
    Ok(generated_docs)
}

pub(crate) fn write_doc(out_dir: &Path, relative_path: &str, content: &str) -> anyhow::Result<()> {
    let target = safe_doc_path(out_dir, relative_path)?;
    reject_symlinked_doc_path(out_dir, &target)?;
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(target, content)?;
    Ok(())
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
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

pub(crate) fn read_codewiki_meta(out_dir: &Path) -> anyhow::Result<CodewikiMeta> {
    let path = safe_doc_path(out_dir, CODEWIKI_META_PATH)?;
    match std::fs::read_to_string(&path) {
        Ok(raw) => Ok(serde_json::from_str(&raw)?),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(CodewikiMeta::default()),
        Err(err) => Err(err.into()),
    }
}

pub(crate) fn write_codewiki_meta(out_dir: &Path, meta: &CodewikiMeta) -> anyhow::Result<()> {
    let content = serde_json::to_string_pretty(meta)?;
    write_doc(out_dir, CODEWIKI_META_PATH, &(content + "\n"))
}

pub(crate) fn source_hashes_for_doc(
    project_root: &Path,
    content: &str,
) -> anyhow::Result<BTreeMap<String, String>> {
    let mut hashes = BTreeMap::new();
    for file in source_files_from_frontmatter(content) {
        let hash = hasher::file_content_hash(&project_root.join(&file))
            .map_err(|err| anyhow::anyhow!("failed to hash codewiki source file {file}: {err}"))?;
        hashes.insert(file, hash);
    }
    Ok(hashes)
}

pub(crate) fn source_files_from_frontmatter(content: &str) -> BTreeSet<String> {
    let mut files = BTreeSet::new();
    let mut in_frontmatter = false;
    for line in content.lines() {
        if line == "---" {
            if in_frontmatter {
                break;
            }
            in_frontmatter = true;
            continue;
        }
        if !in_frontmatter {
            continue;
        }
        if let Some(file) = line.strip_prefix("  - file: ").and_then(yaml_file_value) {
            files.insert(file);
        }
    }
    files
}

fn yaml_file_value(value: &str) -> Option<String> {
    let trimmed = value.trim();
    unquote_yaml_string(trimmed).or_else(|| {
        if trimmed.starts_with('"') || trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_string())
        }
    })
}

pub(crate) fn unquote_yaml_string(value: &str) -> Option<String> {
    let value = value.trim();
    let inner = value.strip_prefix('"')?.strip_suffix('"')?;
    let mut out = String::new();
    let mut chars = inner.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            out.push(match chars.next()? {
                '0' => '\0',
                'a' => '\u{0007}',
                'b' => '\u{0008}',
                't' => '\t',
                'n' => '\n',
                'v' => '\u{000b}',
                'f' => '\u{000c}',
                'r' => '\r',
                'e' => '\u{001b}',
                '"' => '"',
                '/' => '/',
                '\\' => '\\',
                _ => return None,
            });
        } else {
            out.push(ch);
        }
    }
    Some(out)
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
