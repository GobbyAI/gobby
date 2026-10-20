//! Canonicalization of the public call_tool wrapper input.

use serde_json::{Value, json};

fn text(value: &Value) -> Option<&str> {
    value.as_str().filter(|value| !value.is_empty())
}

fn ambiguity(field: &str) -> String {
    format!(
        "Ambiguous call_tool input in '{field}': both 'arguments' and 'args' were provided; pass exactly one."
    )
}

fn named_character(name: &str) -> Option<char> {
    let character = unicode_names2::character(name)?;
    if unicode_names2::name(character)
        .is_some_and(|canonical| canonical.to_string().eq_ignore_ascii_case(name))
    {
        return Some(character);
    }
    let aliases: Value =
        serde_json::from_str(include_str!("../contracts/unicode_name_aliases.v16.json")).ok()?;
    (aliases["aliases"][name.to_ascii_uppercase()].as_u64()? == u64::from(character as u32))
        .then_some(character)
}

/// Python's unicode_escape codec first encodes a str as UTF-8, then treats
/// unescaped bytes as Latin-1. Only the last, legacy parsing attempt uses it.
fn legacy_decode(raw: &str) -> Option<String> {
    let mut bytes = raw.bytes().peekable();
    let mut decoded = String::new();
    while let Some(byte) = bytes.next() {
        if byte != b'\\' {
            decoded.push(char::from(byte));
            continue;
        }
        let escaped = bytes.next()?;
        match escaped {
            b'\n' => {}
            b'\\' | b'\'' | b'"' => decoded.push(char::from(escaped)),
            b'a' => decoded.push('\u{7}'),
            b'b' => decoded.push('\u{8}'),
            b'f' => decoded.push('\u{c}'),
            b'n' => decoded.push('\n'),
            b'r' => decoded.push('\r'),
            b't' => decoded.push('\t'),
            b'v' => decoded.push('\u{b}'),
            b'0'..=b'7' => {
                let mut value = u32::from(escaped - b'0');
                for _ in 0..2 {
                    if let Some(digit @ b'0'..=b'7') = bytes.peek().copied() {
                        bytes.next();
                        value = value * 8 + u32::from(digit - b'0');
                    } else {
                        break;
                    }
                }
                decoded.push(char::from_u32(value)?);
            }
            b'x' | b'u' | b'U' => {
                let count = match escaped {
                    b'x' => 2,
                    b'u' => 4,
                    _ => 8,
                };
                let mut value = 0_u32;
                for _ in 0..count {
                    value = value.checked_mul(16)? + char::from(bytes.next()?).to_digit(16)?;
                }
                decoded.push(char::from_u32(value)?);
            }
            b'N' => {
                if bytes.next()? != b'{' {
                    return None;
                }
                let mut name = String::new();
                loop {
                    match bytes.next()? {
                        b'}' => break,
                        byte => name.push(char::from(byte)),
                    }
                }
                decoded.push(named_character(&name)?);
            }
            _ => {
                decoded.push('\\');
                decoded.push(char::from(escaped));
            }
        }
    }
    Some(decoded)
}

fn coerce(value: &Value, field: &str) -> Result<Value, String> {
    if value.is_null() || value.is_object() {
        return Ok(value.clone());
    }
    if let Some(raw) = value.as_str() {
        for candidate in [raw.to_owned(), raw.replace("\\\"", "\"")] {
            if let Ok(parsed) = serde_json::from_str::<Value>(&candidate)
                && parsed.is_object()
            {
                return Ok(parsed);
            }
        }
        if raw.contains("\\\"")
            && let Some(decoded) = legacy_decode(raw)
            && let Ok(parsed) = serde_json::from_str::<Value>(&decoded)
            && parsed.is_object()
        {
            return Ok(parsed);
        }
    }
    let raw = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    Err(format!(
        "Invalid JSON in '{field}' parameter: {}",
        raw.chars().take(200).collect::<String>()
    ))
}

/// Return the canonical routing context and target arguments.
pub fn canonicalize(input: &Value) -> Result<Value, String> {
    if !input["arguments"].is_null() && !input["args"].is_null() {
        return Err(ambiguity("arguments,args"));
    }
    let field = if input["arguments"].is_null() {
        "args"
    } else {
        "arguments"
    };
    let complete_route =
        text(&input["server_name"]).is_some() && text(&input["tool_name"]).is_some();
    let mut arguments = match coerce(&input[field], field) {
        Ok(arguments) => arguments,
        Err(_) if complete_route => input[field].clone(),
        Err(error) => return Err(error),
    };
    let server = text(&input["server_name"]).or_else(|| text(&arguments["server_name"]));
    let tool = text(&input["tool_name"]).or_else(|| text(&arguments["tool_name"]));
    let nested_route = !complete_route
        && (text(&input["server_name"]).is_none() && text(&arguments["server_name"]).is_some()
            || text(&input["tool_name"]).is_none() && text(&arguments["tool_name"]).is_some());
    let nested_intent =
        nested_route && text(&input["intent"]).is_none() && text(&arguments["intent"]).is_some();
    let mut canonical = json!({
        "server_name":server, "tool_name":tool,
        "session_id":text(&input["session_id"]),
        "project_id":text(&input["project_id"]).or_else(|| nested_route.then(|| text(&arguments["project_id"])).flatten()),
        "intent":text(&input["intent"]).or_else(|| nested_route.then(|| text(&arguments["intent"])).flatten()),
    });
    if nested_route && arguments.is_object() {
        if !arguments["arguments"].is_null() && !arguments["args"].is_null() {
            return Err(ambiguity(&format!("{field}.arguments,{field}.args")));
        }
        let nested_field = ["arguments", "args"]
            .into_iter()
            .find(|key| !arguments[*key].is_null())
            .or_else(|| {
                ["arguments", "args"]
                    .into_iter()
                    .find(|key| arguments.get(*key).is_some())
            });
        if let Some(nested_field) = nested_field {
            let nested = &arguments[nested_field];
            arguments = if nested.is_null() {
                json!({})
            } else if nested.is_string() {
                coerce(nested, &format!("{field}.{nested_field}"))?
            } else {
                nested.clone()
            };
        } else if let Some(object) = arguments.as_object_mut() {
            for key in ["server_name", "tool_name", "project_id"] {
                object.remove(key);
            }
            if nested_intent {
                object.remove("intent");
            }
        }
    }
    canonical["arguments"] = arguments;
    Ok(canonical)
}
