// A 500 MB ceiling keeps video/audio/PDF inbox imports usable while preventing
// accidental multi-GB reads from exhausting memory before media-specific
// ingestion can stream or degrade the file.
const DEFAULT_MAX_INBOX_ITEM_BYTES: u64 = 500_000_000;

pub(crate) fn database_url_from_env() -> Option<String> {
    ["GWIKI_DATABASE_URL", "GOBBY_POSTGRES_DSN"]
        .into_iter()
        .find_map(|name| {
            std::env::var(name)
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
}

pub(crate) fn max_inbox_item_bytes_from_env() -> u64 {
    match std::env::var("GWIKI_MAX_INBOX_ITEM_BYTES") {
        Ok(raw) => parse_positive_u64(&raw).unwrap_or_else(|| {
            eprintln!("warning: ignoring invalid GWIKI_MAX_INBOX_ITEM_BYTES={raw}");
            DEFAULT_MAX_INBOX_ITEM_BYTES
        }),
        Err(_) => DEFAULT_MAX_INBOX_ITEM_BYTES,
    }
}

fn parse_positive_u64(raw: &str) -> Option<u64> {
    raw.trim().parse::<u64>().ok().filter(|value| *value > 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn positive_u64_env_parser_rejects_invalid_values() {
        assert_eq!(parse_positive_u64("42"), Some(42));
        assert_eq!(parse_positive_u64(" 7 "), Some(7));
        assert_eq!(parse_positive_u64("0"), None);
        assert_eq!(parse_positive_u64("-1"), None);
        assert_eq!(parse_positive_u64("nope"), None);
    }
}
