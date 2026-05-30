pub(crate) fn database_url_from_env() -> Option<String> {
    [
        "GWIKI_DATABASE_URL",
        "GOBBY_POSTGRES_DSN",
        "GCODE_DATABASE_URL",
    ]
    .into_iter()
    .find_map(|name| {
        std::env::var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    })
}
