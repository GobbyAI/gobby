use std::collections::BTreeMap;
use std::path::{Component, Path};

#[derive(Debug, Clone, Copy)]
pub(crate) struct CapturedSources<'a> {
    by_path: &'a BTreeMap<String, Vec<u8>>,
}

impl<'a> CapturedSources<'a> {
    pub(crate) fn new(by_path: &'a BTreeMap<String, Vec<u8>>) -> anyhow::Result<Self> {
        for path in by_path.keys() {
            anyhow::ensure!(is_valid_logical_path(path), "unsafe captured path: {path}");
        }
        Ok(Self { by_path })
    }

    pub(crate) fn get(&self, rel_path: &str) -> Option<&'a [u8]> {
        self.by_path.get(rel_path).map(Vec::as_slice)
    }

    pub(crate) fn contains(&self, rel_path: &str) -> bool {
        self.by_path.contains_key(rel_path)
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = (&'a str, &'a [u8])> + 'a {
        self.by_path
            .iter()
            .map(|(path, bytes)| (path.as_str(), bytes.as_slice()))
    }
}

pub(crate) fn is_valid_logical_path(path: &str) -> bool {
    let parsed = Path::new(path);
    !path.is_empty()
        && !path.contains('\\')
        && !parsed.is_absolute()
        && !parsed.components().any(|component| {
            matches!(
                component,
                Component::ParentDir
                    | Component::CurDir
                    | Component::RootDir
                    | Component::Prefix(_)
            )
        })
}
