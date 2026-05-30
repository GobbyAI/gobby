pub(crate) fn collect_timestamp() -> String {
    let millis = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(duration) => duration.as_millis(),
        Err(error) => {
            eprintln!("gwiki: failed to read system clock: {error}");
            0
        }
    };
    format!("unix-ms:{millis}")
}
