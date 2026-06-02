use gobby_core::setup::SetupError;

const POSTGRES_IDENTIFIER_MAX_BYTES: usize = 63;

pub(super) fn qualified_relation(
    schema: &str,
    relation: &str,
    label: &str,
) -> Result<String, SetupError> {
    Ok(format!(
        "{}.{}",
        quote_identifier(schema, "schema")?,
        quote_identifier(relation, label)?
    ))
}

pub(super) fn quote_identifier(value: &str, label: &str) -> Result<String, SetupError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!("{label} identifier must not be empty"),
        });
    }
    if trimmed.contains('\0') {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!("{label} identifier must not contain NUL bytes"),
        });
    }
    let escaped = trimmed.replace('"', "\"\"");
    if escaped.len() > POSTGRES_IDENTIFIER_MAX_BYTES {
        return Err(SetupError::CreationFailed {
            object: label.to_string(),
            message: format!(
                "{label} identifier must be at most {POSTGRES_IDENTIFIER_MAX_BYTES} bytes"
            ),
        });
    }
    Ok(format!("\"{escaped}\""))
}
