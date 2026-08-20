use std::fmt;

use crate::commands::scope;
use crate::visibility;

use super::CodewikiFacts;

/// Stable file identifier used by the CodeWiki facts boundary.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct FileId(String);

impl FileId {
    pub fn new(path: impl Into<String>) -> Self {
        Self(path.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for FileId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// File selection relative to the indexed project root.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ScopeSelector {
    paths: Vec<String>,
    symbols: Vec<String>,
    files: Vec<String>,
    modules: Vec<String>,
}

impl ScopeSelector {
    pub fn all() -> Self {
        Self::default()
    }

    pub fn paths(paths: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            paths: paths.into_iter().map(Into::into).collect(),
            symbols: Vec::new(),
            files: Vec::new(),
            modules: Vec::new(),
        }
    }

    pub fn symbols(ids: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            paths: Vec::new(),
            symbols: ids.into_iter().map(Into::into).collect(),
            files: Vec::new(),
            modules: Vec::new(),
        }
    }

    pub fn endpoints(
        files: impl IntoIterator<Item = impl Into<String>>,
        modules: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            paths: Vec::new(),
            symbols: Vec::new(),
            files: files.into_iter().map(Into::into).collect(),
            modules: modules.into_iter().map(Into::into).collect(),
        }
    }

    pub fn is_all(&self) -> bool {
        self.paths.is_empty()
            && self.symbols.is_empty()
            && self.files.is_empty()
            && self.modules.is_empty()
    }

    pub fn symbol_ids(&self) -> &[String] {
        &self.symbols
    }

    pub fn endpoint_files(&self) -> &[String] {
        &self.files
    }

    pub fn endpoint_modules(&self) -> &[String] {
        &self.modules
    }

    pub(crate) fn normalized(&self, facts: &CodewikiFacts) -> Vec<String> {
        self.paths
            .iter()
            .map(|path| scope::normalize_file_arg(facts.context(), path))
            .collect()
    }
}

/// Indexed file metadata owned by the facade.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FileFact {
    pub id: FileId,
    pub path: String,
    pub language: String,
    pub symbol_count: i64,
}

impl CodewikiFacts {
    pub fn scoped_files(&self, selector: &ScopeSelector) -> anyhow::Result<Vec<FileFact>> {
        let scopes = selector.normalized(self);
        let scope_prefixes = scopes
            .iter()
            .map(|scope| format!("{}/", scope.trim_end_matches('/')))
            .collect::<Vec<_>>();
        let mut conn = self.read_connection()?;
        visibility::visible_tree(&mut conn, self.context())?
            .into_iter()
            .filter(|file| in_scope(&file.file_path, &scopes, &scope_prefixes))
            .map(|file| {
                let path = file.file_path;
                Ok(FileFact {
                    id: FileId::new(path.clone()),
                    path,
                    language: file.language,
                    symbol_count: file.symbol_count,
                })
            })
            .collect()
    }
}

fn in_scope(file: &str, scopes: &[String], scope_prefixes: &[String]) -> bool {
    scopes.is_empty()
        || scopes.iter().any(|scope| scope.is_empty())
        || scopes.iter().any(|scope| file == scope)
        || scope_prefixes.iter().any(|prefix| file.starts_with(prefix))
}
