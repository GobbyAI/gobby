//! Generated runtime configuration contract consumed by Rust clients.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde::Deserialize;
use thiserror::Error;

const CONTRACT_JSON: &str = include_str!("../../assets/config/runtime_config_contract.json");
const SAFE_SEGMENT_BYTES: &[u8] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~";

static CONTRACT: OnceLock<RuntimeConfigContract> = OnceLock::new();

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeConfigContract {
    exact_keys: Vec<ExactKeySpec>,
    patterns: Vec<PatternSpec>,
    codec: CodecContract,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExactKeySpec {
    key: String,
    machine_export: bool,
    #[serde(default)]
    secrecy: Secrecy,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PatternSpec {
    pattern: String,
    machine_export: bool,
    #[serde(default)]
    secrecy: Secrecy,
    #[serde(default)]
    fields: BTreeMap<String, FieldSpec>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FieldSpec {
    #[serde(default)]
    secrecy: Secrecy,
}

/// Secrecy classification mirrored from the Python registry contract. Values
/// other than `none`/`reference` deserialize as `Unknown` so a newer daemon
/// contract never panics an older binary; `Unknown` is never treated as a
/// resolvable secret reference.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Secrecy {
    #[default]
    None,
    Reference,
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Deserialize)]
struct CodecContract {
    vectors: Vec<CodecVector>,
    invalid: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct CodecVector {
    pub decoded: String,
    pub encoded: String,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DynamicSegmentError {
    #[error("dynamic config segment must not be empty")]
    Empty,
    #[error("truncated percent escape in dynamic config segment")]
    TruncatedEscape,
    #[error("percent escapes must use uppercase hexadecimal digits")]
    InvalidEscape,
    #[error("dynamic config segment is not canonically encoded")]
    NonCanonical,
    #[error("dynamic config segment is not valid UTF-8")]
    InvalidUtf8,
}

pub fn is_registered_runtime_key(key: &str) -> bool {
    let contract = contract();
    contract.exact_keys.iter().any(|spec| spec.key == key)
        || contract.patterns.iter().any(|spec| spec.matches(key))
}

pub fn is_machine_config_key(key: &str) -> bool {
    let contract = contract();
    contract
        .exact_keys
        .iter()
        .any(|spec| spec.machine_export && spec.key == key)
        || contract
            .patterns
            .iter()
            .any(|spec| spec.machine_export && spec.matches(key))
}

/// True when `key` addresses a value the contract stores as a `secret-marker `
/// reference: an exact key with `reference` secrecy, or a pattern key whose
/// pattern (or matched `{field}` segment) carries `reference` secrecy. These
/// are the only keys daemon-mode sources may fall through to a datastore
/// primary for — the daemon itself never serves them.
pub fn is_secret_reference_key(key: &str) -> bool {
    let contract = contract();
    contract
        .exact_keys
        .iter()
        .any(|spec| spec.secrecy == Secrecy::Reference && spec.key == key)
        || contract
            .patterns
            .iter()
            .any(|spec| spec.matches(key) && spec.reference_secrecy_for(key))
}

pub fn runtime_contract_codec_vectors() -> &'static [CodecVector] {
    &contract().codec.vectors
}

pub fn invalid_dynamic_segments() -> &'static [String] {
    &contract().codec.invalid
}

pub fn encode_dynamic_segment(value: &str) -> Result<String, DynamicSegmentError> {
    if value.is_empty() {
        return Err(DynamicSegmentError::Empty);
    }
    let mut encoded = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if SAFE_SEGMENT_BYTES.contains(byte) {
            encoded.push(char::from(*byte));
        } else {
            encoded.push('%');
            encoded.push(hex_digit(byte >> 4));
            encoded.push(hex_digit(byte & 0x0f));
        }
    }
    Ok(encoded)
}

pub fn decode_dynamic_segment(value: &str) -> Result<String, DynamicSegmentError> {
    if value.is_empty() {
        return Err(DynamicSegmentError::Empty);
    }
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' => {
                if index + 2 >= bytes.len() {
                    return Err(DynamicSegmentError::TruncatedEscape);
                }
                let high = decode_hex(bytes[index + 1])?;
                let low = decode_hex(bytes[index + 2])?;
                decoded.push((high << 4) | low);
                index += 3;
            }
            byte if SAFE_SEGMENT_BYTES.contains(&byte) => {
                decoded.push(byte);
                index += 1;
            }
            _ => return Err(DynamicSegmentError::NonCanonical),
        }
    }
    let decoded = String::from_utf8(decoded).map_err(|_| DynamicSegmentError::InvalidUtf8)?;
    if encode_dynamic_segment(&decoded)? != value {
        return Err(DynamicSegmentError::NonCanonical);
    }
    Ok(decoded)
}

impl PatternSpec {
    fn matches(&self, key: &str) -> bool {
        let pattern_segments = self.pattern.split('.');
        let key_segments = key.split('.');
        if pattern_segments.clone().count() != key_segments.clone().count() {
            return false;
        }
        pattern_segments
            .zip(key_segments)
            .all(|(expected, actual)| {
                let Some(placeholder) = expected
                    .strip_prefix('{')
                    .and_then(|value| value.strip_suffix('}'))
                else {
                    return expected == actual;
                };
                let Ok(decoded) = decode_dynamic_segment(actual) else {
                    return false;
                };
                placeholder != "field"
                    || self.fields.is_empty()
                    || self.fields.contains_key(&decoded)
            })
    }

    /// Reference secrecy for an already-matched key: the pattern's own
    /// secrecy, or the secrecy of the field spec addressed by the `{field}`
    /// segment when the pattern declares per-field specs.
    fn reference_secrecy_for(&self, key: &str) -> bool {
        if self.secrecy == Secrecy::Reference {
            return true;
        }
        self.pattern
            .split('.')
            .zip(key.split('.'))
            .any(|(expected, actual)| {
                expected == "{field}"
                    && decode_dynamic_segment(actual).is_ok_and(|decoded| {
                        self.fields
                            .get(&decoded)
                            .is_some_and(|field| field.secrecy == Secrecy::Reference)
                    })
            })
    }
}

fn contract() -> &'static RuntimeConfigContract {
    CONTRACT.get_or_init(|| {
        // The asset is generated from the Python registry and byte-checked in CI.
        match serde_json::from_str(CONTRACT_JSON) {
            Ok(contract) => contract,
            Err(error) => panic!("invalid embedded runtime configuration contract: {error}"),
        }
    })
}

fn decode_hex(value: u8) -> Result<u8, DynamicSegmentError> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'A'..=b'F' => Ok(value - b'A' + 10),
        _ => Err(DynamicSegmentError::InvalidEscape),
    }
}

fn hex_digit(value: u8) -> char {
    char::from(if value < 10 {
        b'0' + value
    } else {
        b'A' + (value - 10)
    })
}
