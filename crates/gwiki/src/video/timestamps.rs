use crate::transcribe::{TranscriptSegment, TranscriptionRange};

pub(super) fn transcript_start_seconds(segment: &TranscriptSegment) -> u32 {
    let seconds = segment.start_ms / 1_000;
    seconds.min(u64::from(u32::MAX)) as u32
}

pub(super) fn parse_timestamp_seconds(value: &str) -> Option<u32> {
    let parts = value
        .split(':')
        .map(parse_timestamp_part)
        .collect::<Option<Vec<_>>>()?;

    match parts.as_slice() {
        [seconds] => Some(*seconds),
        [minutes, seconds] => minutes.checked_mul(60)?.checked_add(*seconds),
        [hours, minutes, seconds] => hours
            .checked_mul(3600)?
            .checked_add(minutes.checked_mul(60)?)?
            .checked_add(*seconds),
        _ => None,
    }
}

fn parse_timestamp_part(value: &str) -> Option<u32> {
    value
        .split_once('.')
        .map_or(value, |(seconds, _)| seconds)
        .trim()
        .parse()
        .ok()
}

pub(super) fn format_timestamp(seconds: u32) -> String {
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let seconds = seconds % 60;
    format!("{hours:02}:{minutes:02}:{seconds:02}")
}

pub(super) fn format_ranges_ms(ranges: &[TranscriptionRange]) -> String {
    ranges
        .iter()
        .map(|range| format!("{}-{}", range.start_ms, range.end_ms))
        .collect::<Vec<_>>()
        .join(",")
}
