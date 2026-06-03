use super::*;

pub(crate) fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    if id.trim().is_empty()
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
        if file_name
            .to_str()
            .is_some_and(|name| name.starts_with(&prefix))
        {
            paths.push(Path::new("raw/assets").join(file_name));
        }
    }
    Ok(paths)
}

pub(crate) fn remove_relative_file(
    vault_root: &Path,
    relative_path: &Path,
) -> Result<bool, WikiError> {
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
