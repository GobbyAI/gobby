use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TypedQuery {
    pub cypher: String,
    pub params: HashMap<String, String>,
}

pub fn cypher_string_literal(s: &str) -> String {
    let escaped = s.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{escaped}'")
}

pub fn string_params(values: &[(&str, &str)]) -> HashMap<String, String> {
    values
        .iter()
        .map(|(key, value)| ((*key).to_string(), cypher_string_literal(value)))
        .collect()
}

pub fn clamp_limit(limit: usize, max: usize) -> usize {
    limit.clamp(1, max)
}

pub fn clamp_offset(offset: usize, max: usize) -> usize {
    offset.min(max)
}

pub fn id_list_literal(ids: &[String]) -> String {
    ids.iter()
        .map(|id| cypher_string_literal(id))
        .collect::<Vec<_>>()
        .join(", ")
}
