use chrono::{DateTime, FixedOffset, Local, NaiveDate, Offset, Utc};

use crate::WikiError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ParsedTimestamp {
    Instant(DateTime<Utc>),
    DateOnly(NaiveDate),
    DatePrefix { date: NaiveDate, original: String },
    Unparseable(String),
}

pub(crate) fn parse_timestamp(value: &str) -> ParsedTimestamp {
    let trimmed = value.trim();
    if let Some(millis) = trimmed.strip_prefix("unix-ms:") {
        return millis
            .parse::<i64>()
            .ok()
            .and_then(DateTime::<Utc>::from_timestamp_millis)
            .map(ParsedTimestamp::Instant)
            .unwrap_or_else(|| ParsedTimestamp::Unparseable(value.to_string()));
    }
    if let Ok(parsed) = DateTime::parse_from_rfc3339(trimmed) {
        return ParsedTimestamp::Instant(parsed.with_timezone(&Utc));
    }
    if let Ok(date) = NaiveDate::parse_from_str(trimmed, "%Y-%m-%d") {
        return ParsedTimestamp::DateOnly(date);
    }
    if let Some(prefix) = trimmed.get(..10)
        && let Ok(date) = NaiveDate::parse_from_str(prefix, "%Y-%m-%d")
    {
        return ParsedTimestamp::DatePrefix {
            date,
            original: value.to_string(),
        };
    }
    ParsedTimestamp::Unparseable(value.to_string())
}

pub(crate) fn format_timestamp(value: &ParsedTimestamp, offset: FixedOffset) -> String {
    match value {
        ParsedTimestamp::Instant(instant) => format!(
            "{} (unix-ms:{})",
            instant.with_timezone(&offset).format("%Y-%m-%d %H:%M %:z"),
            instant.timestamp_millis()
        ),
        ParsedTimestamp::DateOnly(date) => date.format("%Y-%m-%d").to_string(),
        ParsedTimestamp::DatePrefix { original, .. } | ParsedTimestamp::Unparseable(original) => {
            original.clone()
        }
    }
}

pub(crate) fn local_offset_for(value: &ParsedTimestamp) -> FixedOffset {
    match value {
        ParsedTimestamp::Instant(instant) => instant.with_timezone(&Local).offset().fix(),
        ParsedTimestamp::DateOnly(_)
        | ParsedTimestamp::DatePrefix { .. }
        | ParsedTimestamp::Unparseable(_) => Local::now().offset().fix(),
    }
}

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
        detail: "system timestamp milliseconds exceed u64 range".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use chrono::FixedOffset;

    use super::*;

    #[test]
    fn parsed_timestamps_format_injected_offsets_and_preserve_precision() {
        let negative = FixedOffset::west_opt(7 * 60 * 60).expect("valid negative offset");
        let positive = FixedOffset::east_opt(5 * 60 * 60 + 30 * 60).expect("valid positive offset");
        let instant = parse_timestamp("2026-07-05T01:30:00Z");

        assert_eq!(
            format_timestamp(&instant, negative),
            "2026-07-04 18:30 -07:00 (unix-ms:1783215000000)"
        );
        assert_eq!(
            format_timestamp(&instant, positive),
            "2026-07-05 07:00 +05:30 (unix-ms:1783215000000)"
        );
        assert_eq!(
            format_timestamp(&parse_timestamp("2026-07-05"), negative),
            "2026-07-05"
        );
        assert_eq!(
            format_timestamp(&parse_timestamp("2026-07-05 (approximate)"), negative),
            "2026-07-05 (approximate)"
        );
        assert_eq!(
            format_timestamp(&parse_timestamp("last tuesday"), negative),
            "last tuesday"
        );
    }

    #[test]
    fn unix_timestamp_ms_returns_epoch_milliseconds() {
        let timestamp = unix_timestamp_ms().expect("timestamp");
        let earliest_expected = 1_704_067_200_000;
        let now = u64::try_from(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("system clock after Unix epoch")
                .as_millis(),
        )
        .expect("current timestamp fits u64");

        assert!(
            (earliest_expected..=now).contains(&timestamp),
            "timestamp {timestamp} was outside expected range {earliest_expected}..={now}"
        );
    }
}
