pub(crate) fn split_keyword_dsn_tokens(database_url: &str) -> Vec<&str> {
    let mut tokens = Vec::new();
    let mut start = None;
    let mut in_single_quote = false;
    let mut escaped = false;

    for (index, ch) in database_url.char_indices() {
        if start.is_none() {
            if ch.is_whitespace() {
                continue;
            }
            start = Some(index);
        }

        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '\'' {
            in_single_quote = !in_single_quote;
            continue;
        }
        if ch.is_whitespace()
            && !in_single_quote
            && let Some(token_start) = start.take()
        {
            tokens.push(&database_url[token_start..index]);
        }
    }

    if let Some(token_start) = start {
        tokens.push(&database_url[token_start..]);
    }
    tokens
}
