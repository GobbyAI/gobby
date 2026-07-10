use std::path::{Component, Path, PathBuf};

/// Why a requested vault-relative path was rejected.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PathViolation {
    Absolute,
    Escape,
    Empty,
}

/// Normalize a vault-relative request path shared by the read and page
/// commands: strips `.` components and rejects absolute paths, parent-dir
/// traversal, and empty results. Callers map violations onto their own
/// error surface so command-specific messages stay stable.
pub(crate) fn normalize_requested_path(path: &Path) -> Result<PathBuf, PathViolation> {
    if path.is_absolute() {
        return Err(PathViolation::Absolute);
    }

    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(value) => normalized.push(value),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(PathViolation::Escape);
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(PathViolation::Empty);
    }

    Ok(normalized)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_strips_curdir_and_rejects_violations() {
        assert_eq!(
            normalize_requested_path(Path::new("./knowledge/./topics/a.md")),
            Ok(PathBuf::from("knowledge/topics/a.md"))
        );
        assert_eq!(
            normalize_requested_path(Path::new("/etc/passwd")),
            Err(PathViolation::Absolute)
        );
        assert_eq!(
            normalize_requested_path(Path::new("knowledge/../outputs/a.md")),
            Err(PathViolation::Escape)
        );
        assert_eq!(
            normalize_requested_path(Path::new(".")),
            Err(PathViolation::Empty)
        );
    }
}
