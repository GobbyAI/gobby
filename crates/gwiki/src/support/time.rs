pub(crate) fn collect_timestamp() -> Result<String, std::time::SystemTimeError> {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_millis();
    Ok(format!("unix-ms:{millis}"))
}
