//! JSON decoding for daemon payloads nested past serde_json's default depth.

use serde::de::{DeserializeOwned, Error as _};

/// Deepest nesting accepted. A right-deep workspace layout spends two levels per
/// split, so this admits layouts of about 500 splits while bounding stack growth.
const MAX_NESTING: usize = 1024;

/// Decodes one complete JSON document nested up to `MAX_NESTING` levels.
///
/// serde_json stops at 128 levels, and a right-deep workspace layout spends two
/// levels per split. The limit is lifted and `serde_stacker` grows the stack on
/// demand. A byte scan caps nesting before any recursion, so a hostile document
/// cannot grow the stack without bound. Trailing bytes are still rejected,
/// matching `serde_json::from_slice`.
pub(crate) fn from_slice_nested<T: DeserializeOwned>(bytes: &[u8]) -> serde_json::Result<T> {
    if !nesting_within_limit(bytes) {
        return Err(serde_json::Error::custom(format!(
            "JSON nesting exceeds {MAX_NESTING} levels"
        )));
    }
    let mut deserializer = serde_json::Deserializer::from_slice(bytes);
    deserializer.disable_recursion_limit();
    let value = T::deserialize(serde_stacker::Deserializer::new(&mut deserializer))?;
    deserializer.end()?;
    Ok(value)
}

/// Tracks bracket depth outside string literals in one pass, without recursion.
fn nesting_within_limit(bytes: &[u8]) -> bool {
    let mut depth = 0_usize;
    let mut in_string = false;
    let mut escaped = false;
    for &byte in bytes {
        if in_string {
            match byte {
                _ if escaped => escaped = false,
                b'\\' => escaped = true,
                b'"' => in_string = false,
                _ => {}
            }
        } else {
            match byte {
                b'"' => in_string = true,
                b'[' | b'{' => {
                    depth += 1;
                    if depth > MAX_NESTING {
                        return false;
                    }
                }
                b']' | b'}' => depth = depth.saturating_sub(1),
                _ => {}
            }
        }
    }
    true
}

#[cfg(test)]
#[path = "nested_json/tests.rs"]
mod tests;
