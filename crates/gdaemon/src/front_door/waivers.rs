//! The committed behaviour-level parity waiver manifest that compare readiness checks against.
//!
//! Python's loader in `scripts/parity_waivers.py` validates the same file against the ledger
//! inventory; this reader keeps readiness bound to the committed entries, not to whatever a
//! compare record claims.

use std::collections::BTreeMap;

use anyhow::{Context, Result, bail};
use serde::Deserialize;

const MANIFEST: &str = include_str!("../../../../docs/contracts/parity-waivers.json");
const SCHEMA_VERSION: u32 = 1;

/// One committed waiver: the surface it covers, what diverges, and the decision behind it.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WaiverEntry {
    pub id: String,
    pub surface: String,
    pub behaviour: String,
    pub decision: String,
    pub replacement: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestFile {
    schema_version: u32,
    waivers: Vec<WaiverEntry>,
}

/// The waiver manifest keyed by id, as committed in `docs/contracts/parity-waivers.json`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WaiverManifest {
    entries: BTreeMap<String, WaiverEntry>,
}

impl WaiverManifest {
    /// The manifest compiled into this binary.
    pub fn embedded() -> Result<Self> {
        Self::parse(MANIFEST).context("docs/contracts/parity-waivers.json")
    }

    pub fn parse(text: &str) -> Result<Self> {
        let file: ManifestFile = serde_json::from_str(text).context("malformed waiver manifest")?;
        if file.schema_version != SCHEMA_VERSION {
            bail!(
                "waiver manifest schema_version {} is not {SCHEMA_VERSION}",
                file.schema_version
            );
        }
        let mut entries = BTreeMap::new();
        for entry in file.waivers {
            let id = entry.id.clone();
            if entries.insert(id.clone(), entry).is_some() {
                bail!("duplicate waiver id {id}");
            }
        }
        Ok(Self { entries })
    }

    pub fn get(&self, id: &str) -> Option<&WaiverEntry> {
        self.entries.get(id)
    }
}

/// Whether a waiver id names a native-only surface (`<family>.<surface>.native-extension`).
pub fn is_native_extension(id: &str) -> bool {
    id.ends_with(".native-extension")
}

/// Whether a manifest surface `HTTP <METHOD> <route template>` covers one request.
///
/// The method must match exactly. A `{name}` template segment matches one non-empty path
/// segment, and every other segment must match literally.
pub fn surface_matches(surface: &str, method: &str, path: &str) -> bool {
    let Some((surface_method, template)) = surface
        .strip_prefix("HTTP ")
        .and_then(|rest| rest.split_once(' '))
    else {
        return false;
    };
    if surface_method != method
        || !template.starts_with('/')
        || template.contains(char::is_whitespace)
    {
        return false;
    }
    let template: Vec<&str> = template.split('/').collect();
    let actual: Vec<&str> = path.split('/').collect();
    template.len() == actual.len()
        && template.iter().zip(&actual).all(|(segment, value)| {
            (segment.starts_with('{') && segment.ends_with('}') && !value.is_empty())
                || segment == value
        })
}
