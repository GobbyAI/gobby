//! The text and JSON layouts of a routed line, as Python's formatters write them.

use gobby_core::json::DumpError;

use super::Level;

/// One record, as the layouts see it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
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
pub fn format_text(_record: &Record) -> String {
    String::new()
}

/// The JSON line: `time`, `level`, `name`, `module`, `message`, then the extras, encoded
/// as Python's `json.dumps` does.
pub fn format_json(_record: &Record) -> Result<String, DumpError> {
    Ok(String::new())
}
