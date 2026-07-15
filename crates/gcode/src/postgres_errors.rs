pub(crate) fn message(error: &postgres::Error) -> String {
    if let Some(db_error) = error.as_db_error() {
        let mut parts = vec![db_error.message().to_string()];
        if let Some(detail) = db_error.detail()
            && !detail.is_empty()
        {
            parts.push(detail.to_string());
        }
        if let Some(hint) = db_error.hint()
            && !hint.is_empty()
        {
            parts.push(hint.to_string());
        }
        return parts.join(": ");
    }
    error.to_string()
}
