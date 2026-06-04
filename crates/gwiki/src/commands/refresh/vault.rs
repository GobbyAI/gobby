use super::*;

pub(crate) fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    let id = id.trim();
    if id.is_empty()
        || id.contains('/')
        || id.contains('\\')
        || Path::new(id)
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(WikiError::InvalidInput {
            field: "source_id",
            message: format!("unsafe source id `{id}`"),
        });
    }
    Ok(Path::new("raw").join(format!("{id}.md")))
}

pub(crate) fn source_asset_paths_for_id(
    vault_root: &Path,
    id: &str,
) -> Result<Vec<PathBuf>, WikiError> {
    let id = id.trim();
    let asset_dir = vault_root.join("raw/assets");
    if !asset_dir.exists() {
        return Ok(Vec::new());
    }
    let prefix = format!("{id}.");
    let mut paths = Vec::new();
    for entry in fs::read_dir(&asset_dir).map_err(|error| WikiError::Io {
        action: "read raw source assets",
        path: Some(asset_dir.clone()),
        source: error,
    })? {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw source asset entry",
            path: Some(asset_dir.clone()),
            source: error,
        })?;
        let file_name = entry.file_name();
        if file_name.to_str().is_some_and(|name| {
            let path = Path::new(name);
            path.file_stem().and_then(|stem| stem.to_str()) == Some(id)
                && path
                    .extension()
                    .and_then(|extension| extension.to_str())
                    .is_some_and(|extension| !extension.is_empty())
                && name.starts_with(&prefix)
        }) {
            paths.push(Path::new("raw/assets").join(file_name));
        }
    }
    Ok(paths)
}

pub(crate) fn remove_relative_file(
    vault_root: &Path,
    relative_path: &Path,
) -> Result<bool, WikiError> {
    let relative_path = safe_refresh_relative_path(relative_path)?;
    let full_path = vault_root.join(relative_path);
    match fs::remove_file(&full_path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(WikiError::Io {
            action: "remove superseded raw source path",
            path: Some(full_path),
            source: error,
        }),
    }
}

fn safe_refresh_relative_path(relative_path: &Path) -> Result<PathBuf, WikiError> {
    if relative_path.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "relative_path",
            message: "path must be a non-empty vault-relative path".to_string(),
        });
    }

    let mut normalized = PathBuf::new();
    for component in relative_path.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir
            | Component::ParentDir
            | Component::RootDir
            | Component::Prefix(_) => {
                return Err(WikiError::InvalidInput {
                    field: "relative_path",
                    message: format!(
                        "path `{}` must stay inside the vault",
                        relative_path.display()
                    ),
                });
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "relative_path",
            message: "path must include a file name".to_string(),
        });
    }

    Ok(normalized)
}

pub(crate) fn ensure_scope_root(root: &Path) -> Result<(), WikiError> {
    if root.is_dir() {
        Ok(())
    } else {
        Err(WikiError::NotFound {
            resource: "wiki scope",
            id: root.display().to_string(),
        })
    }
}
