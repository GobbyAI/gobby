use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};

use gobby_code::codewiki_facts::CodewikiFacts;
use gobby_core::ai::generation::DirectGenerationTarget;
use gobby_core::ai_context::AiContext;
use gobby_core::config::AiCapability;

pub(crate) use crate::output::Format;

/// Non-datastore state carried by the wiki-owned CodeWiki engine.
#[derive(Clone)]
pub struct CodeEngineRuntime {
    pub(crate) project_root: PathBuf,
    pub(crate) project_id: String,
    pub(crate) quiet: bool,
    pub(crate) verbose: bool,
    pub(crate) output: Format,
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
        output: Format,
        ai: AiContext,
        facts: CodewikiFacts,
    ) -> Self {
        Self {
            project_root,
            project_id,
            quiet,
            verbose,
            output,
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

    pub fn output(&self) -> &Format {
        &self.output
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

pub(crate) fn print_json<T: serde::Serialize + ?Sized>(value: &T) -> anyhow::Result<()> {
    let mut stdout = std::io::stdout().lock();
    crate::output::print_json(&mut stdout, value)?;
    Ok(())
}

pub(crate) fn print_text(text: &str) -> anyhow::Result<()> {
    let mut stdout = std::io::stdout().lock();
    crate::output::print_text(&mut stdout, text)?;
    Ok(())
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

pub(crate) fn normalize_file_arg(runtime: &CodeEngineRuntime, file: &str) -> String {
    let path = Path::new(file);
    if path.is_absolute() {
        if let Ok(relative) = path.strip_prefix(&runtime.project_root) {
            return clean_relative_path(relative);
        }
        if let (Ok(absolute), Ok(root)) = (path.canonicalize(), runtime.project_root.canonicalize())
            && let Ok(relative) = absolute.strip_prefix(root)
        {
            return clean_relative_path(relative);
        }
    }
    clean_relative_path(path)
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
    let extension = Path::new(file_path)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    matches!(
        extension.as_str(),
        "bash"
            | "c"
            | "cc"
            | "cjs"
            | "cpp"
            | "cs"
            | "cxx"
            | "dart"
            | "ex"
            | "exs"
            | "gemspec"
            | "go"
            | "h"
            | "hh"
            | "hpp"
            | "hxx"
            | "java"
            | "js"
            | "jsx"
            | "json"
            | "jsonc"
            | "kt"
            | "kts"
            | "lua"
            | "m"
            | "mm"
            | "mjs"
            | "php"
            | "py"
            | "pyi"
            | "rake"
            | "rb"
            | "rs"
            | "sc"
            | "scala"
            | "sh"
            | "swift"
            | "ts"
            | "tsx"
            | "yaml"
            | "yml"
    )
}
