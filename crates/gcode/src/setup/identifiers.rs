use gobby_core::setup::SetupError;

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
    Ok(format!("\"{}\"", trimmed.replace('"', "\"\"")))
}
