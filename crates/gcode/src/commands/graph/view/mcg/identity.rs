//! File/module seed identity and equivalence-class closure for MCG.

use std::collections::{HashMap, HashSet};
use std::fmt;

use crate::index::import_resolution::ImportResolutionContext;

use super::super::{CandidateEndpoint, CandidateEndpointKind};

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
            let providers = identity.providers_for(&endpoint.id);
            let Some(file) = providers.first().filter(|_| providers.len() == 1) else {
                return Vec::new();
            };
            let mut closed = vec![file_endpoint(file)];
            if let Some(aliases) = identity.aliases.get(file) {
                closed.extend(aliases.iter().map(|name| module_endpoint(name, Some(file))));
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

    pub(super) fn from_resolution(
        visible: &HashSet<String>,
        resolver: &ImportResolutionContext,
        imports: &[(String, String)],
    ) -> Self {
        let mut aliases = HashMap::new();
        for file in visible {
            aliases.insert(
                file.clone(),
                equivalence_for_file(file, visible, resolver, imports),
            );
        }
        let mut modules = HashSet::new();
        for names in aliases.values() {
            modules.extend(names.iter().cloned());
        }
        for (_, module) in imports {
            modules.insert(module.clone());
        }
        let mut providers = HashMap::new();
        for module in modules {
            providers.insert(
                module.clone(),
                module_providers(&module, visible, resolver, imports),
            );
        }
        Self {
            visible_files: visible.clone(),
            providers,
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

fn equivalence_for_file(
    file: &str,
    visible: &HashSet<String>,
    resolver: &ImportResolutionContext,
    imports: &[(String, String)],
) -> Vec<String> {
    let mut names = Vec::new();
    for name in resolver.path_derived_module_names(file) {
        if module_providers(&name, visible, resolver, imports).as_slice() == [file] {
            names.push(name);
        }
    }
    for (source, module) in imports {
        let mut files = resolver.candidate_files_for_module(module, Some(source));
        files.retain(|path| visible.contains(path));
        files.sort();
        files.dedup();
        if files.as_slice() == [file] {
            names.push(module.clone());
        }
    }
    names.sort();
    names.dedup();
    names
}

fn module_providers(
    module: &str,
    visible: &HashSet<String>,
    resolver: &ImportResolutionContext,
    imports: &[(String, String)],
) -> Vec<String> {
    let mut files = resolver.candidate_files_for_module(module, None);
    for (source, target) in imports {
        if target == module {
            files.extend(resolver.candidate_files_for_module(module, Some(source)));
        }
    }
    files.retain(|file| visible.contains(file));
    files.sort();
    files.dedup();
    files
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
