//! The chunk map, docs/port/map-v2.json.

use std::path::Path;

use anyhow::Context;
use serde::Deserialize;
use serde_json::Value;

/// One chunk of the map. Only the fields the checks read are typed.
#[derive(Debug, Clone, Deserialize)]
pub struct Chunk {
    pub id: String,
    pub title: String,
    pub kind: String,
    #[serde(default, deserialize_with = "null_as_default")]
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
        let chunks: Vec<Chunk> =
            serde_json::from_value(chunks).context("the map's chunks do not parse")?;
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
