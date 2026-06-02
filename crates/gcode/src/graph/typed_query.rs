use std::collections::{BTreeMap, HashMap};
use std::fmt;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TypedQuery {
    pub cypher: String,
    pub params: HashMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum TypedValue {
    Null,
    String(String),
    Integer(i64),
    Float(f64),
    Bool(bool),
    List(Vec<TypedValue>),
    Map(BTreeMap<String, TypedValue>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IdentifierKind {
    ParameterName,
    MapKey,
}

/// String rendering context retained for compatibility with older
/// `TypedQueryError::ControlCharacter` consumers.
#[deprecated(
    since = "0.9.10",
    note = "string values are escaped now; this context is retained only for API compatibility"
)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValueContext {
    String,
}

#[allow(deprecated)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TypedQueryError {
    InvalidIdentifier {
        kind: IdentifierKind,
        identifier: String,
    },
    /// Legacy error variant retained for API compatibility.
    ///
    /// String control characters are escaped by `render_cypher_value`, so new
    /// code should not construct or match this variant for current rendering.
    #[deprecated(
        since = "0.9.10",
        note = "string control characters are escaped instead of rejected"
    )]
    ControlCharacter {
        #[allow(deprecated)]
        context: ValueContext,
        codepoint: u32,
    },
    NonFiniteFloat {
        value: String,
    },
}

impl TypedQuery {
    pub fn new(cypher: impl Into<String>) -> Self {
        Self {
            cypher: cypher.into(),
            params: HashMap::new(),
        }
    }

    pub fn with_params<I, K>(cypher: impl Into<String>, params: I) -> Result<Self, TypedQueryError>
    where
        I: IntoIterator<Item = (K, TypedValue)>,
        K: Into<String>,
    {
        let mut query = Self::new(cypher);
        for (name, value) in params {
            query.insert_param(name, value)?;
        }
        Ok(query)
    }

    pub fn insert_param(
        &mut self,
        name: impl Into<String>,
        value: TypedValue,
    ) -> Result<(), TypedQueryError> {
        let name = name.into();
        validate_identifier(&name, IdentifierKind::ParameterName)?;
        let rendered = render_cypher_value(&value)?;
        self.params.insert(name, rendered);
        Ok(())
    }
}

pub fn cypher_string_literal(s: &str) -> String {
    format!("'{}'", escape_string_contents(s))
}

pub fn render_cypher_value(value: &TypedValue) -> Result<String, TypedQueryError> {
    match value {
        TypedValue::Null => Ok("null".to_string()),
        TypedValue::String(value) => render_string_literal(value),
        TypedValue::Integer(value) => Ok(value.to_string()),
        TypedValue::Float(value) => render_float(*value),
        TypedValue::Bool(value) => Ok(value.to_string()),
        TypedValue::List(values) => values
            .iter()
            .map(render_cypher_value)
            .collect::<Result<Vec<_>, _>>()
            .map(|values| format!("[{}]", values.join(", "))),
        TypedValue::Map(values) => values
            .iter()
            .map(|(key, value)| {
                validate_identifier(key, IdentifierKind::MapKey)?;
                Ok(format!("{key}: {}", render_cypher_value(value)?))
            })
            .collect::<Result<Vec<_>, _>>()
            .map(|values| format!("{{{}}}", values.join(", "))),
    }
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

pub fn validate_identifier(identifier: &str, kind: IdentifierKind) -> Result<(), TypedQueryError> {
    let mut chars = identifier.chars();
    let Some(first) = chars.next() else {
        return Err(TypedQueryError::InvalidIdentifier {
            kind,
            identifier: identifier.to_string(),
        });
    };

    if !(first == '_' || first.is_ascii_alphabetic())
        || !chars.all(|ch| ch == '_' || ch.is_ascii_alphanumeric())
    {
        return Err(TypedQueryError::InvalidIdentifier {
            kind,
            identifier: identifier.to_string(),
        });
    }

    Ok(())
}

fn render_string_literal(value: &str) -> Result<String, TypedQueryError> {
    Ok(cypher_string_literal(value))
}

fn escape_string_contents(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '\'' => escaped.push_str("\\'"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\u{0008}' => escaped.push_str("\\b"),
            '\u{000C}' => escaped.push_str("\\f"),
            ch if ch.is_control() => escaped.push_str(&format!("\\u{:04X}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    escaped
}

fn render_float(value: f64) -> Result<String, TypedQueryError> {
    if !value.is_finite() {
        return Err(TypedQueryError::NonFiniteFloat {
            value: value.to_string(),
        });
    }

    let mut rendered = value.to_string();
    if !rendered.contains('.') && !rendered.contains('e') && !rendered.contains('E') {
        rendered.push_str(".0");
    }
    Ok(rendered)
}

impl fmt::Display for IdentifierKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ParameterName => f.write_str("parameter name"),
            Self::MapKey => f.write_str("map key"),
        }
    }
}

#[allow(deprecated)]
impl fmt::Display for ValueContext {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::String => f.write_str("string"),
        }
    }
}

#[allow(deprecated)]
impl fmt::Display for TypedQueryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidIdentifier { kind, identifier } => write!(
                f,
                "invalid {kind} `{identifier}`; expected ^[A-Za-z_][A-Za-z0-9_]*$"
            ),
            Self::ControlCharacter { context, codepoint } => write!(
                f,
                "control character U+{codepoint:04X} is not allowed in {context} value"
            ),
            Self::NonFiniteFloat { value } => {
                write!(f, "non-finite float `{value}` is not allowed")
            }
        }
    }
}

impl std::error::Error for TypedQueryError {}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[test]
    fn typed_params_render_nested_safe_cypher_literals() {
        let mut props = BTreeMap::new();
        props.insert("enabled".to_string(), TypedValue::Bool(true));
        props.insert(
            "label".to_string(),
            TypedValue::String("caf\u{00e9} \"quote\" and 'single' \\ slash".to_string()),
        );
        props.insert(
            "nested".to_string(),
            TypedValue::List(vec![
                TypedValue::Integer(1),
                TypedValue::Float(2.25),
                TypedValue::Bool(false),
            ]),
        );

        let query = TypedQuery::with_params(
            "RETURN $name, $count, $ratio, $whole, $enabled, $items, $props",
            [
                (
                    "name",
                    TypedValue::String("O'Reilly \\ path \u{2603}".to_string()),
                ),
                ("count", TypedValue::Integer(42)),
                ("ratio", TypedValue::Float(1.5)),
                ("whole", TypedValue::Float(1.0)),
                ("enabled", TypedValue::Bool(true)),
                (
                    "items",
                    TypedValue::List(vec![
                        TypedValue::String("a".to_string()),
                        TypedValue::Integer(-7),
                        TypedValue::Bool(false),
                    ]),
                ),
                ("props", TypedValue::Map(props)),
            ],
        )
        .expect("valid typed params should render");

        assert_eq!(
            query.cypher,
            "RETURN $name, $count, $ratio, $whole, $enabled, $items, $props"
        );
        assert_eq!(
            query.params.get("name").map(String::as_str),
            Some("'O\\'Reilly \\\\ path \u{2603}'")
        );
        assert_eq!(query.params.get("count").map(String::as_str), Some("42"));
        assert_eq!(query.params.get("ratio").map(String::as_str), Some("1.5"));
        assert_eq!(query.params.get("whole").map(String::as_str), Some("1.0"));
        assert_eq!(
            query.params.get("enabled").map(String::as_str),
            Some("true")
        );
        assert_eq!(
            query.params.get("items").map(String::as_str),
            Some("['a', -7, false]")
        );
        assert_eq!(
            query.params.get("props").map(String::as_str),
            Some(
                "{enabled: true, label: 'caf\u{00e9} \\\"quote\\\" and \\'single\\' \\\\ slash', nested: [1, 2.25, false]}"
            )
        );
    }

    #[test]
    fn string_literals_escape_both_quote_delimiters() {
        let rendered = render_cypher_value(&TypedValue::String("a 'single' and \"double\"".into()))
            .expect("valid string should render");

        assert_eq!(rendered, "'a \\'single\\' and \\\"double\\\"'");
    }

    #[test]
    fn string_literals_escape_control_characters() {
        let rendered = render_cypher_value(&TypedValue::String(
            "line\ncarriage\rtab\tbackspace\u{0008}form\u{000C}escape\u{001B}".into(),
        ))
        .expect("control characters should render as escaped literals");

        assert_eq!(
            rendered,
            "'line\\ncarriage\\rtab\\tbackspace\\bform\\fescape\\u001B'"
        );
    }

    #[test]
    fn nested_string_values_escape_control_characters() {
        let mut props = BTreeMap::new();
        props.insert(
            "items".to_string(),
            TypedValue::List(vec![TypedValue::String("line\nitem".to_string())]),
        );
        props.insert(
            "label".to_string(),
            TypedValue::String("tab\tvalue".to_string()),
        );

        let rendered =
            render_cypher_value(&TypedValue::Map(props)).expect("nested strings should render");

        assert_eq!(rendered, "{items: ['line\\nitem'], label: 'tab\\tvalue'}");
    }

    #[test]
    fn invalid_identifiers_return_typed_errors() {
        let param_error =
            TypedQuery::with_params("RETURN $bad", [("bad-name", TypedValue::Bool(true))])
                .expect_err("invalid parameter name should fail");
        assert_eq!(
            param_error,
            TypedQueryError::InvalidIdentifier {
                kind: IdentifierKind::ParameterName,
                identifier: "bad-name".to_string(),
            }
        );

        let mut props = BTreeMap::new();
        props.insert("bad.key".to_string(), TypedValue::Integer(1));
        let map_error =
            render_cypher_value(&TypedValue::Map(props)).expect_err("invalid map key should fail");
        assert_eq!(
            map_error,
            TypedQueryError::InvalidIdentifier {
                kind: IdentifierKind::MapKey,
                identifier: "bad.key".to_string(),
            }
        );
    }

    #[test]
    fn unsafe_values_return_typed_errors() {
        for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let error = render_cypher_value(&TypedValue::Float(value))
                .expect_err("non-finite float should fail");
            assert!(matches!(error, TypedQueryError::NonFiniteFloat { .. }));
        }
    }
}
