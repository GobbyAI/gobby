use super::error::SchemaError;

pub fn split_sql_statements(sql: &str) -> Result<Vec<String>, SchemaError> {
    let bytes = sql.as_bytes();
    let mut statements = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'-' if bytes.get(index + 1) == Some(&b'-') => {
                index = skip_line_comment(bytes, index);
            }
            b'/' if bytes.get(index + 1) == Some(&b'*') => {
                index = skip_block_comment(bytes, index)?;
            }
            b'\'' => {
                let escaped = index > 0
                    && matches!(bytes[index - 1], b'e' | b'E')
                    && (index < 2 || !is_identifier_byte(bytes[index - 2]));
                index = skip_single_quote(bytes, index, escaped)?;
            }
            b'"' => {
                index = skip_double_quote(bytes, index)?;
            }
            b'$' => {
                if let Some(tag) = dollar_tag(bytes, index) {
                    index = skip_dollar_quote(bytes, index, tag)?;
                } else {
                    index += 1;
                }
            }
            b';' => {
                let statement = sql[start..index].trim();
                if !statement.is_empty() {
                    statements.push(statement.to_owned());
                }
                index += 1;
                start = index;
            }
            _ => index += 1,
        }
    }
    let tail = sql[start..].trim();
    if !tail.is_empty() {
        statements.push(tail.to_owned());
    }
    Ok(statements)
}

fn skip_line_comment(bytes: &[u8], start: usize) -> usize {
    bytes[start + 2..]
        .iter()
        .position(|byte| *byte == b'\n')
        .map_or(bytes.len(), |offset| start + 3 + offset)
}

fn skip_block_comment(bytes: &[u8], start: usize) -> Result<usize, SchemaError> {
    let mut depth = 1;
    let mut index = start + 2;
    while index < bytes.len() {
        if bytes.get(index..index + 2) == Some(b"/*") {
            depth += 1;
            index += 2;
        } else if bytes.get(index..index + 2) == Some(b"*/") {
            depth -= 1;
            index += 2;
            if depth == 0 {
                return Ok(index);
            }
        } else {
            index += 1;
        }
    }
    Err(SchemaError::UnterminatedSql("block comment".to_owned()))
}

fn skip_single_quote(
    bytes: &[u8],
    start: usize,
    escape_backslashes: bool,
) -> Result<usize, SchemaError> {
    let mut index = start + 1;
    while index < bytes.len() {
        if escape_backslashes && bytes[index] == b'\\' {
            index += 2;
        } else if bytes[index] == b'\'' {
            if bytes.get(index + 1) == Some(&b'\'') {
                index += 2;
            } else {
                return Ok(index + 1);
            }
        } else {
            index += 1;
        }
    }
    Err(SchemaError::UnterminatedSql(
        "single-quoted string".to_owned(),
    ))
}

fn skip_double_quote(bytes: &[u8], start: usize) -> Result<usize, SchemaError> {
    let mut index = start + 1;
    while index < bytes.len() {
        if bytes[index] == b'"' {
            if bytes.get(index + 1) == Some(&b'"') {
                index += 2;
            } else {
                return Ok(index + 1);
            }
        } else {
            index += 1;
        }
    }
    Err(SchemaError::UnterminatedSql(
        "double-quoted identifier".to_owned(),
    ))
}

fn dollar_tag(bytes: &[u8], start: usize) -> Option<&[u8]> {
    let next = *bytes.get(start + 1)?;
    if next == b'$' {
        return Some(&bytes[start..start + 2]);
    }
    if !next.is_ascii_alphabetic() && next != b'_' {
        return None;
    }
    let mut end = start + 2;
    while let Some(byte) = bytes.get(end) {
        if *byte == b'$' {
            return Some(&bytes[start..=end]);
        }
        if !is_identifier_byte(*byte) {
            return None;
        }
        end += 1;
    }
    None
}

fn skip_dollar_quote(bytes: &[u8], start: usize, tag: &[u8]) -> Result<usize, SchemaError> {
    let search_start = start + tag.len();
    bytes[search_start..]
        .windows(tag.len())
        .position(|window| window == tag)
        .map(|offset| search_start + offset + tag.len())
        .ok_or_else(|| SchemaError::UnterminatedSql(String::from_utf8_lossy(tag).into_owned()))
}

fn is_identifier_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'$'
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitter_rejects_unterminated_constructs() {
        assert!(split_sql_statements("SELECT 'unterminated").is_err());
        assert!(split_sql_statements("/* unterminated").is_err());
        assert!(split_sql_statements("SELECT $body$unterminated").is_err());
    }
}
