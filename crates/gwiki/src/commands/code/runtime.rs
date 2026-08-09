use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};

use gobby_code::codewiki_facts::CodewikiFacts;
use gobby_code::index::languages::is_supported_language;
use gobby_core::ai::generation::DirectGenerationTarget;
use gobby_core::ai_context::AiContext;
use gobby_core::config::AiCapability;

/// Non-datastore state carried by the wiki-owned CodeWiki engine.
#[derive(Clone)]
pub struct CodeEngineRuntime {
    pub(crate) project_root: PathBuf,
    pub(crate) project_id: String,
    pub(crate) quiet: bool,
    pub(crate) verbose: bool,
    pub(crate) ai: AiContext,
    direct_targets: BTreeMap<String, DirectGenerationTarget>,
    pub(crate) facts: CodewikiFacts,
}

impl CodeEngineRuntime {
    pub fn new(
        project_root: PathBuf,
        project_id: String,
        quiet: bool,
        verbose: bool,
        ai: AiContext,
        facts: CodewikiFacts,
    ) -> Self {
        Self {
            project_root,
            project_id,
            quiet,
            verbose,
            ai,
            direct_targets: BTreeMap::new(),
            facts,
        }
    }

    pub fn with_direct_targets(
        mut self,
        targets: impl IntoIterator<Item = (String, DirectGenerationTarget)>,
    ) -> Self {
        self.direct_targets = targets.into_iter().collect();
        self
    }

    pub(crate) fn direct_target(&self, profile: &str) -> DirectGenerationTarget {
        self.direct_targets
            .get(profile)
            .cloned()
            .unwrap_or_else(|| {
                let binding = self.ai.binding(AiCapability::TextGenerate);
                DirectGenerationTarget {
                    api_base: binding.api_base.clone(),
                    api_key: binding.api_key.clone(),
                    model: binding.model.clone(),
                    provider: binding.provider.clone(),
                    reasoning_effort: binding.reasoning_effort.clone(),
                }
            })
    }
}

pub(crate) fn resolve_output_path(project_root: &Path, out: Option<&str>) -> PathBuf {
    let path = Path::new(out.unwrap_or(super::DEFAULT_OUT_DIR));
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        project_root.join(path)
    }
}

pub(crate) mod hasher {
    use std::path::Path;

    pub(crate) fn content_hash(source: &[u8]) -> String {
        gobby_core::indexing::content_hash(source)
    }

    pub(crate) fn file_content_hash(path: &Path) -> anyhow::Result<String> {
        Ok(gobby_core::indexing::file_content_hash(path)?)
    }
}

pub(crate) fn normalize_storage_path(path: &Path) -> String {
    normalize_storage_path_str(&path.to_string_lossy())
}

pub(crate) fn normalize_storage_path_str(path: &str) -> String {
    #[cfg(windows)]
    {
        path.replace('\\', "/")
    }
    #[cfg(not(windows))]
    {
        path.to_owned()
    }
}

pub(crate) fn normalize_file_arg(
    runtime: &CodeEngineRuntime,
    file: &str,
) -> anyhow::Result<String> {
    let path = Path::new(file);
    if path.is_absolute() {
        if let Ok(relative) = path.strip_prefix(&runtime.project_root) {
            return Ok(clean_relative_path(relative));
        }
        if let (Ok(absolute), Ok(root)) = (path.canonicalize(), runtime.project_root.canonicalize())
            && let Ok(relative) = absolute.strip_prefix(root)
        {
            return Ok(clean_relative_path(relative));
        }
        anyhow::bail!("codewiki scope path `{file}` is outside the project root");
    }
    Ok(clean_relative_path(path))
}

fn clean_relative_path(path: &Path) -> String {
    let mut clean = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                clean.pop();
            }
            Component::Normal(part) => clean.push(part),
            Component::RootDir | Component::Prefix(_) => {}
        }
    }
    normalize_storage_path(&clean)
}

pub(crate) fn is_indexed_language(file_path: &str) -> bool {
    is_supported_language(file_path)
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use super::resolve_output_path;

    #[test]
    fn output_paths_are_resolved_from_the_project_root() {
        let project_root = Path::new("/tmp/project");

        assert_eq!(
            resolve_output_path(project_root, Some("docs/wiki")),
            project_root.join("docs/wiki")
        );
        assert_eq!(
            resolve_output_path(project_root, Some("/tmp/gwiki-output")),
            PathBuf::from("/tmp/gwiki-output")
        );
    }
}
