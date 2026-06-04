use crate::WikiError;

pub(crate) fn collect_timestamp() -> Result<String, WikiError> {
    let millis = unix_timestamp_ms()?;
    Ok(format!("unix-ms:{millis}"))
}

pub(crate) fn unix_timestamp_ms() -> Result<u64, WikiError> {
    let duration = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|error| WikiError::Config {
            detail: format!("system clock is before Unix epoch: {error}"),
        })?;
    u64::try_from(duration.as_millis()).map_err(|_| WikiError::Config {
        detail: "system timestamp exceeds u64 milliseconds".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unix_timestamp_ms_returns_epoch_milliseconds() {
        assert!(unix_timestamp_ms().expect("timestamp") > 0);
    }
}
