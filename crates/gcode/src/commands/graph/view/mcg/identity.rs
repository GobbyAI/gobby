//! File/module seed identity and equivalence-class closure for MCG.

use std::fmt;

use crate::communities::identity::ImportIdentity;

use super::super::{CandidateEndpoint, CandidateEndpointKind};

#[derive(Clone, Debug)]
pub(crate) struct McgSeed {
    pub files: Vec<CandidateEndpoint>,
    pub modules: Vec<CandidateEndpoint>,
    pub input: String,
    pub kind: String,
    pub file: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum McgSeedSelector<'a> {
    File(&'a str),
    Module(&'a str),
}

impl<'a> McgSeedSelector<'a> {
    pub(crate) fn value(self) -> &'a str {
        match self {
            Self::File(value) | Self::Module(value) => value,
        }
    }
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
    selector: McgSeedSelector<'_>,
    identity: &ImportIdentity,
) -> Result<McgSeed, McgSeedError> {
    let seed = selector.value();
    if matches!(selector, McgSeedSelector::File(_)) {
        return identity
            .visible_files
            .contains(seed)
            .then(|| closed_file_seed(seed, "file", identity))
            .ok_or_else(|| McgSeedError::Missing {
                input: seed.to_string(),
            });
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
    identity: &ImportIdentity,
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

fn closed_file_seed(file: &str, kind: &str, identity: &ImportIdentity) -> McgSeed {
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
