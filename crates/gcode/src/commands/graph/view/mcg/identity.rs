//! File/module seed identity and equivalence-class closure for MCG.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fmt;

use crate::index::import_resolution::ImportResolutionContext;

use super::super::{CandidateEndpoint, CandidateEndpointKind};

/// Module-name ↔ file identity for the MCG walk.
///
/// `providers[module]` lists the visible files a module name can resolve to
/// across the whole project; `aliases[file]` lists the module names whose only
/// provider is that file. Alias membership is global: a relative specifier that
/// resolves to different files from different importers is ambiguous and
/// belongs to no file's equivalence class, because stored `IMPORTS` targets are
/// `CodeModule` nodes keyed by name and admit every consumer of that name.
#[derive(Clone, Debug)]
pub(crate) struct McgIdentity {
    pub visible_files: HashSet<String>,
    pub providers: HashMap<String, Vec<String>>,
    pub aliases: HashMap<String, Vec<String>>,
}

#[derive(Clone, Debug)]
pub(crate) struct McgSeed {
    pub files: Vec<CandidateEndpoint>,
    pub modules: Vec<CandidateEndpoint>,
    pub input: String,
    pub kind: String,
    pub file: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum McgSeedError {
    Missing {
        input: String,
    },
    Ambiguous {
        input: String,
        providers: Vec<String>,
    },
}

impl fmt::Display for McgSeedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing { input } => {
                write!(f, "No file or module matching '{input}' found")
            }
            Self::Ambiguous { input, providers } => write!(
                f,
                "Ambiguous module '{input}'. Refine the query. Provider files: {}",
                providers.join(", ")
            ),
        }
    }
}

impl std::error::Error for McgSeedError {}

pub(crate) fn resolve_mcg_seed(
    seed: &str,
    identity: &McgIdentity,
) -> Result<McgSeed, McgSeedError> {
    if identity.visible_files.contains(seed) {
        return Ok(closed_file_seed(seed, "file", identity));
    }
    let providers = identity.providers_for(seed);
    match providers.as_slice() {
        [] => Err(McgSeedError::Missing {
            input: seed.to_string(),
        }),
        [file] => {
            let mut resolved = closed_file_seed(file, "module", identity);
            resolved.input = seed.to_string();
            Ok(resolved)
        }
        _ => Err(McgSeedError::Ambiguous {
            input: seed.to_string(),
            providers,
        }),
    }
}

pub(crate) fn close_endpoint(
    endpoint: &CandidateEndpoint,
    identity: &McgIdentity,
) -> Vec<CandidateEndpoint> {
    match endpoint.kind {
        CandidateEndpointKind::File => identity
            .aliases
            .get(&endpoint.id)
            .into_iter()
            .flatten()
            .map(|name| module_endpoint(name, Some(&endpoint.id)))
            .collect(),
        CandidateEndpointKind::Module => {
            let Some(file) = identity.unique_provider(&endpoint.id) else {
                return Vec::new();
            };
            let mut closed = vec![file_endpoint(&file)];
            if let Some(aliases) = identity.aliases.get(&file) {
                closed.extend(
                    aliases
                        .iter()
                        .map(|name| module_endpoint(name, Some(&file))),
                );
            }
            closed
        }
        _ => Vec::new(),
    }
}

impl McgIdentity {
    fn providers_for(&self, module: &str) -> Vec<String> {
        self.providers
            .get(module)
            .into_iter()
            .flatten()
            .filter(|file| self.visible_files.contains(*file))
            .cloned()
            .collect()
    }

    /// The single visible file providing `module`, or `None` when the name is
    /// unknown or resolves to several files.
    pub(super) fn unique_provider(&self, module: &str) -> Option<String> {
        match self.providers_for(module).as_slice() {
            [file] => Some(file.clone()),
            _ => None,
        }
    }

    /// One-shot identity build: `O(|rows| + |visible| + index)` resolver work.
    ///
    /// Every distinct module name (import targets plus the path-derived names of
    /// each visible file) gets its importer-independent candidates once; every
    /// distinct `(importer, module)` row adds the importer-aware candidates.
    pub(super) fn from_resolution(
        visible: &HashSet<String>,
        resolver: &ImportResolutionContext,
        imports: &[(String, String)],
    ) -> Self {
        let rows = imports
            .iter()
            .map(|(source, module)| (source.as_str(), module.as_str()))
            .collect::<HashSet<_>>();
        let derived =
            resolver.path_derived_module_names_for_files(visible.iter().map(String::as_str));
        let modules = rows
            .iter()
            .map(|(_, module)| *module)
            .chain(derived.values().flatten().map(String::as_str))
            .collect::<HashSet<_>>();

        let mut providers: HashMap<String, BTreeSet<String>> = HashMap::new();
        for module in modules {
            providers.entry(module.to_string()).or_default().extend(
                resolver
                    .importer_independent_candidates(module)
                    .into_iter()
                    .filter(|file| visible.contains(file)),
            );
        }
        for (source, module) in &rows {
            providers.entry((*module).to_string()).or_default().extend(
                resolver
                    .importer_candidates(module, source)
                    .into_iter()
                    .filter(|file| visible.contains(file)),
            );
        }

        let mut aliases = visible
            .iter()
            .map(|file| (file.clone(), Vec::new()))
            .collect::<HashMap<String, Vec<String>>>();
        for (module, files) in &providers {
            if let [file] = files.iter().collect::<Vec<_>>().as_slice() {
                aliases
                    .entry((*file).clone())
                    .or_default()
                    .push(module.clone());
            }
        }
        for names in aliases.values_mut() {
            names.sort();
        }
        Self {
            visible_files: visible.clone(),
            providers: providers
                .into_iter()
                .map(|(module, files)| (module, files.into_iter().collect()))
                .collect(),
            aliases,
        }
    }
}

fn closed_file_seed(file: &str, kind: &str, identity: &McgIdentity) -> McgSeed {
    let modules = identity
        .aliases
        .get(file)
        .into_iter()
        .flatten()
        .map(|name| module_endpoint(name, Some(file)))
        .collect();
    McgSeed {
        files: vec![file_endpoint(file)],
        modules,
        input: file.to_string(),
        kind: kind.to_string(),
        file: Some(file.to_string()),
    }
}

fn file_endpoint(path: &str) -> CandidateEndpoint {
    CandidateEndpoint {
        kind: CandidateEndpointKind::File,
        id: path.to_string(),
        name: Some(path.to_string()),
        file: Some(path.to_string()),
        content_hash: None,
        machine_id: None,
    }
}

fn module_endpoint(name: &str, provider: Option<&str>) -> CandidateEndpoint {
    CandidateEndpoint {
        kind: CandidateEndpointKind::Module,
        id: name.to_string(),
        name: Some(name.to_string()),
        file: provider.map(str::to_string),
        content_hash: None,
        machine_id: None,
    }
}
