//! The committed behaviour-level parity waiver manifest that compare readiness checks against.
//!
//! Python's loader in `scripts/parity_waivers.py` validates the same file against the ledger
//! inventory; this reader keeps readiness bound to the committed entries, not to whatever a
//! compare record claims.

use std::collections::BTreeMap;
use std::fmt;

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

    /// The entry a replayed case cites, or the typed reason the citation does not stand.
    ///
    /// Mirrors Python's `cite_waiver` in `scripts/parity_waivers.py`: a citation is valid only
    /// when the waiver exists and its surface covers the case's kind.
    pub fn cite(
        &self,
        waiver_id: &str,
        case: CaseSurface<'_>,
    ) -> Result<&WaiverEntry, WaiverError> {
        let entry = self
            .get(waiver_id)
            .ok_or_else(|| WaiverError::UnknownWaiver(waiver_id.to_owned()))?;
        if surface_matches_case(&entry.surface, case) {
            Ok(entry)
        } else {
            Err(WaiverError::SurfaceMismatch {
                case: case.describe(),
                surface: entry.surface.clone(),
            })
        }
    }
}

/// Whether a waiver id names a native-only surface (`<family>.<surface>.native-extension`).
pub fn is_native_extension(id: &str) -> bool {
    id.ends_with(".native-extension")
}

/// Whether a manifest surface `HTTP <METHOD> <route template>` covers one request.
///
/// The surface method must be uppercase (`[A-Z]+`, as `scripts/parity_waivers.py` requires) and
/// must equal the request method exactly. A `{name}` template segment matches one non-empty path
/// segment, and every other segment must match literally.
pub fn surface_matches(surface: &str, method: &str, path: &str) -> bool {
    let Some((surface_method, template)) = surface
        .strip_prefix("HTTP ")
        .and_then(|rest| rest.split_once(' '))
    else {
        return false;
    };
    if !is_uppercase_method(surface_method)
        || surface_method != method
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

/// Whether an HTTP method is one or more uppercase ASCII letters.
fn is_uppercase_method(method: &str) -> bool {
    !method.is_empty() && method.bytes().all(|byte| byte.is_ascii_uppercase())
}

/// The surface a replayed case presents to the matcher: an HTTP request, or the exact ledger key
/// of an MCP, WS or CLI case.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaseSurface<'a> {
    Http { method: &'a str, path: &'a str },
    Keyed { key: &'a str },
}

impl CaseSurface<'_> {
    fn describe(self) -> String {
        match self {
            Self::Http { method, path } => format!("{method} {path}"),
            Self::Keyed { key } => key.to_owned(),
        }
    }
}

/// Whether a manifest surface covers one replayed case of its kind.
///
/// An HTTP case uses the template rule of [`surface_matches`]. A keyed case (MCP, WS or CLI)
/// matches only the exact ledger key it carries, so an HTTP surface never covers a keyed case and
/// a keyed surface never covers an HTTP case.
pub fn surface_matches_case(surface: &str, case: CaseSurface<'_>) -> bool {
    match case {
        CaseSurface::Http { method, path } => surface_matches(surface, method, path),
        CaseSurface::Keyed { key } => surface == key,
    }
}

/// Why a waiver citation does not stand. Display keeps the phrases the replay assertions match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaiverError {
    /// The case cites a waiver id that the committed manifest does not hold.
    UnknownWaiver(String),
    /// The cited waiver's surface does not cover the case's kind and surface.
    SurfaceMismatch { case: String, surface: String },
}

impl fmt::Display for WaiverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownWaiver(id) => write!(f, "unknown waiver {id}"),
            Self::SurfaceMismatch { case, surface } => {
                write!(f, "{case} does not match waiver surface {surface}")
            }
        }
    }
}

impl std::error::Error for WaiverError {}
