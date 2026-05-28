use clap::ValueEnum;
use serde::Serialize;

#[derive(Clone, Copy, Debug, ValueEnum)]
pub enum Format {
    Json,
    Text,
}

/// Print a serializable value to stdout in the requested format.
pub fn print_json<T: Serialize + ?Sized>(value: &T) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

/// Print a serializable value to stdout as compact JSON.
pub fn print_json_compact<T: Serialize + ?Sized>(value: &T) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string(value)?);
    Ok(())
}

/// Print a plain text command result to stdout.
pub fn print_text(text: &str) -> anyhow::Result<()> {
    println!("{text}");
    Ok(())
}
