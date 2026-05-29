use std::fmt;
use std::io::Write;

use clap::ValueEnum;

use crate::CommandResult;

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Format {
    Json,
    Text,
}

#[derive(Debug)]
pub enum OutputError {
    Io(std::io::Error),
    Json(serde_json::Error),
}

impl fmt::Display for OutputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "output write failed: {error}"),
            Self::Json(error) => write!(f, "JSON rendering failed: {error}"),
        }
    }
}

impl std::error::Error for OutputError {}

impl From<std::io::Error> for OutputError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<serde_json::Error> for OutputError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}

pub fn print_result(
    mut writer: impl Write,
    format: Format,
    result: &CommandResult,
) -> Result<(), OutputError> {
    match format {
        Format::Json => print_json(&mut writer, &result.payload),
        Format::Text => print_text(&mut writer, &result.text),
    }
}

pub fn print_json<T: serde::Serialize + ?Sized>(
    writer: &mut impl Write,
    value: &T,
) -> Result<(), OutputError> {
    writeln!(writer, "{}", serde_json::to_string_pretty(value)?)?;
    Ok(())
}

pub fn print_text(writer: &mut impl Write, text: &str) -> Result<(), OutputError> {
    writeln!(writer, "{text}")?;
    Ok(())
}

pub fn print_status(mut writer: impl Write, message: &str) -> std::io::Result<()> {
    writeln!(writer, "gwiki: {message}")
}
