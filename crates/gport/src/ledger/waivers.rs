//! The waiver manifest, `docs/contracts/parity-waivers.json`, as scripts/parity_waivers.py loads it
//! for the ledger.
//!
//! The additive-surface set is empty in gport, exactly as `ADDITIVE_SURFACES` is empty in the
//! Python generator, so every `native-extension` entry is refused.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use anyhow::{Context, Result, anyhow, bail};
use fancy_regex::Regex;
use serde_json::{Map, Value};

use super::pyfmt::{py_string_repr, py_strip};

const SCHEMA_VERSION: u64 = 1;
const SHAPE_ERROR: &str =
    "Waiver entries need exactly id, surface, behaviour, decision, replacement";
const SEGMENT: &str = "[a-z0-9]+(?:-[a-z0-9]+)*";
const DECISION_PATTERN: &str =
    r"^(?:#[1-9][0-9]*|\.gobby/plans/[A-Za-z0-9._-]+\.md#[A-Za-z0-9._-]+)$";

/// One deliberate divergence cited against a ledger row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Waiver {
    pub id: String,
    pub surface: String,
    pub behaviour: String,
    pub decision: String,
    pub replacement: String,
}

/// Load and validate every entry against the frozen row keys, keyed by waiver id.
pub fn load(path: &Path, surfaces: &BTreeSet<String>) -> Result<BTreeMap<String, Waiver>> {
    let mut loaded: BTreeMap<String, Waiver> = BTreeMap::new();
    for entry in read_entries(path)? {
        let waiver = parse_entry(&entry)?;
        if loaded.contains_key(&waiver.id) {
            bail!("Duplicate waiver id {}", py_string_repr(&waiver.id));
        }
        if waiver.id.ends_with(".native-extension") {
            bail!(
                "{}: {} is not a declared additive surface",
                waiver.id,
                py_string_repr(&waiver.surface)
            );
        }
        if !surfaces.contains(&waiver.surface) {
            bail!(
                "{}: surface {} is not an exact ledger key",
                waiver.id,
                py_string_repr(&waiver.surface)
            );
        }
        loaded.insert(waiver.id.clone(), waiver);
    }
    Ok(loaded)
}

/// The manifest's `waivers` list after the document checks.
fn read_entries(path: &Path) -> Result<Vec<Value>> {
    let parsed = fs::read_to_string(path)
        .map_err(|error| error.to_string())
        .and_then(|text| serde_json::from_str::<Value>(&text).map_err(|error| error.to_string()));
    let document = parsed
        .map_err(|error| anyhow!("Cannot read waiver manifest {}: {error}", path.display()))?;
    let Some(object) = document.as_object() else {
        bail!("Waiver manifest must be an object with schema_version and waivers");
    };
    if object.len() != 2
        || !object.contains_key("schema_version")
        || !object.contains_key("waivers")
    {
        bail!("Waiver manifest must be an object with schema_version and waivers");
    }
    if object.get("schema_version").and_then(Value::as_u64) != Some(SCHEMA_VERSION) {
        bail!("Waiver manifest schema_version must be {SCHEMA_VERSION}");
    }
    match object.get("waivers").and_then(Value::as_array) {
        Some(entries) => Ok(entries.clone()),
        None => bail!("Waiver manifest waivers must be a list"),
    }
}

/// One entry: exactly the five fields, each a non-empty string, with a slug id and a decision
/// that is `#N` or a plan section.
fn parse_entry(entry: &Value) -> Result<Waiver> {
    let Some(object) = entry.as_object() else {
        bail!(SHAPE_ERROR);
    };
    if object.len() != 5 || !has_fields(object) {
        bail!(SHAPE_ERROR);
    }
    // Checked in sorted field order, as the Python loader does.
    let behaviour = text_field(object, "behaviour")?;
    let decision = text_field(object, "decision")?;
    let id = text_field(object, "id")?;
    let replacement = text_field(object, "replacement")?;
    let surface = text_field(object, "surface")?;
    if !matches_pattern(&id_pattern(), &id)? {
        bail!(
            "Waiver id {} must be <family>.<surface>.<behaviour> slugs",
            py_string_repr(&id)
        );
    }
    if !matches_pattern(DECISION_PATTERN, &decision)? {
        bail!("{id}: decision must be #N or .gobby/plans/<plan>.md#<section>");
    }
    Ok(Waiver {
        id,
        surface,
        behaviour,
        decision,
        replacement,
    })
}

fn has_fields(object: &Map<String, Value>) -> bool {
    ["id", "surface", "behaviour", "decision", "replacement"]
        .iter()
        .all(|field| object.contains_key(*field))
}

fn text_field(object: &Map<String, Value>, field: &str) -> Result<String> {
    match object.get(field).and_then(Value::as_str) {
        Some(text) if !py_strip(text).is_empty() => Ok(text.to_owned()),
        _ => bail!("Waiver field {field} must be a non-empty string"),
    }
}

/// `<family>.<surface>.<behaviour>`, each a lowercase slug.
fn id_pattern() -> String {
    format!("^{SEGMENT}\\.{SEGMENT}\\.{SEGMENT}$")
}

/// Whether the whole of `text` matches `pattern`, as Python's `re.fullmatch` does.
fn matches_pattern(pattern: &str, text: &str) -> Result<bool> {
    let regex = Regex::new(pattern).context("internal waiver pattern does not compile")?;
    regex.is_match(text).context("waiver pattern failed to run")
}
