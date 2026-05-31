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
    std::env::var("GWIKI_MAX_INBOX_ITEM_BYTES")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_MAX_INBOX_ITEM_BYTES)
}
