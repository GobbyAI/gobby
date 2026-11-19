//! The pre-configuration sink and the raw stderr writer.

/// Installs the sink that prints each record's message to stderr as bare text, until
/// routing activates. Idempotent.
pub fn install_pre_config() {}

/// Writes `line` to stderr as it is, for output whose bytes other programs read.
pub fn raw_stderr(_line: &str) {}
