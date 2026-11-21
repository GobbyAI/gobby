//! The text and JSON layouts of a routed line, as Python's formatters write them.

use std::fmt::Write as _;

use gobby_core::json::{DumpError, Json, PYTHON, dumps};

use super::Level;

/// One record, as the layouts see it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Record {
    /// Local time as `YYYY-MM-DDTHH:MM:SS`; the text layout writes a space for the `T`.
    pub time: String,
    pub level: Level,
    /// The full target, such as `gobby.hooks`.
    pub target: String,
    /// The stem of the source file that emitted the record.
    pub module: String,
    pub message: String,
    /// The record's other fields, in the order they were recorded.
    pub extras: Vec<(String, String)>,
}

/// The text line: `time - LEVEL - short name - message`, then ` | k=v` per extra, with
/// C0 controls, DEL, CR and LF escaped so one record is one line.
pub(crate) fn format_text(record: &Record) -> String {
    let time = record.time.replacen('T', " ", 1);
    let short_name = record
        .target
        .strip_prefix("gobby.")
        .unwrap_or(&record.target);
    let mut line = format!(
        "{time} - {:<8} - {} - {}",
        record.level.name(),
        escape(short_name),
        escape(&record.message)
    );
    for (key, value) in &record.extras {
        let _ = write!(line, " | {}={}", escape(key), escape(value));
    }
    line
}

/// The JSON line: `time`, `level`, `name`, `module`, `message`, then the extras, encoded
/// as Python's `json.dumps` does. An extra named like a fixed key replaces its value in
/// place, as a later assignment to a Python dict does.
pub(crate) fn format_json(record: &Record) -> Result<String, DumpError> {
    let mut members: Vec<(String, Json)> = [
        ("time", record.time.as_str()),
        ("level", record.level.name()),
        ("name", record.target.as_str()),
        ("module", record.module.as_str()),
        ("message", record.message.as_str()),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), Json::Str(value.to_owned())))
    .collect();
    for (key, value) in &record.extras {
        let value = Json::Str(value.clone());
        match members.iter_mut().find(|(existing, _)| existing == key) {
            Some((_, slot)) => *slot = value,
            None => members.push((key.clone(), value)),
        }
    }
    dumps(&Json::Object(members), PYTHON)
}

/// `text` with every control character written as an escape, so a value cannot start a
/// new physical line or carry terminal control bytes.
fn escape(text: &str) -> String {
    if !text.chars().any(char::is_control) {
        return text.to_owned();
    }
    let mut escaped = String::with_capacity(text.len() + 8);
    for character in text.chars() {
        match character {
            '\r' => escaped.push_str(r"\r"),
            '\n' => escaped.push_str(r"\n"),
            '\t' => escaped.push_str(r"\t"),
            control if control.is_control() => {
                let _ = write!(escaped, "\\x{:02x}", u32::from(control));
            }
            other => escaped.push(other),
        }
    }
    escaped
}
