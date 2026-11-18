//! The chunk map, docs/port/map-v2.json.

use std::path::Path;

use anyhow::{Context, bail};
use serde::Deserialize;
use serde_json::Value;

/// One chunk of the map. Only the fields the checks read are typed. `ledger_selectors` is filled
/// by [`ledger_selectors`] when the map parses, so every verb reads it by the same rule.
#[derive(Debug, Clone, Deserialize)]
pub struct Chunk {
    pub id: String,
    pub title: String,
    pub kind: String,
    #[serde(skip)]
    pub ledger_selectors: Vec<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub depends_on: Vec<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub python_edits: String,
}

fn null_as_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Default + Deserialize<'de>,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

/// A chunk's `ledger_selectors`, the one rule every verb reads the field by: a list of strings. A
/// falsy value means no selectors, as Python's `chunk.get("ledger_selectors") or []` reads it: a
/// missing key, null, false, 0, 0.0, "", [] and {}. Any other non-list is an input error, and so
/// is a list with an item that is not a string. Both errors name the chunk `id`.
pub fn ledger_selectors(id: &str, value: Option<&Value>) -> anyhow::Result<Vec<String>> {
    match value {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_owned)
                    .with_context(|| format!("chunk {id}: a ledger selector is not a string"))
            })
            .collect(),
        Some(value) if is_falsy(value) => Ok(Vec::new()),
        Some(_) => bail!("chunk {id}: ledger_selectors is not a list"),
    }
}

/// Python's truth value of a JSON value, for every kind `ledger_selectors` can hold.
fn is_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(flag) => !flag,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(fields) => fields.is_empty(),
    }
}

/// The map: its chunks typed, and the whole document for the record generator.
#[derive(Debug, Clone)]
pub struct Map {
    pub chunks: Vec<Chunk>,
    pub raw: Value,
}

impl Map {
    pub fn from_bytes(bytes: &[u8]) -> anyhow::Result<Self> {
        let raw: Value = serde_json::from_slice(bytes).context("the map is not valid JSON")?;
        let chunks = raw
            .get("chunks")
            .cloned()
            .context("the map has no chunks key")?;
        let values: Vec<Value> =
            serde_json::from_value(chunks).context("the map's chunks do not parse")?;
        let chunks = values
            .iter()
            .map(parse_chunk)
            .collect::<anyhow::Result<Vec<Chunk>>>()?;
        Ok(Self { chunks, raw })
    }

    /// Read the map from the working tree under `root`.
    pub fn load(root: &Path) -> anyhow::Result<Self> {
        let path = root.join(crate::MAP_PATH);
        let bytes =
            std::fs::read(&path).with_context(|| format!("cannot read {}", path.display()))?;
        Self::from_bytes(&bytes)
    }

    pub fn chunk(&self, id: &str) -> Option<&Chunk> {
        self.chunks.iter().find(|chunk| chunk.id == id)
    }
}

/// One chunk from its JSON object: its typed fields, then its selectors by [`ledger_selectors`].
fn parse_chunk(value: &Value) -> anyhow::Result<Chunk> {
    let mut chunk: Chunk =
        serde_json::from_value(value.clone()).context("the map's chunks do not parse")?;
    chunk.ledger_selectors = ledger_selectors(&chunk.id, value.get("ledger_selectors"))?;
    Ok(chunk)
}
