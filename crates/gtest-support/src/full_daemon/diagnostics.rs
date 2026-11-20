//! What a failed readiness wait reports: bounded tails of the daemon's logs.

use std::fs;
use std::path::Path;

/// The most lines a readiness failure shows from each log.
const TAIL_LINES: usize = 40;

/// The diagnostics a readiness failure carries for a daemon whose home is `home`.
///
/// This stub shows only the captured stderr, as before routed logging.
pub(super) fn ready_diagnostics(home: &Path) -> String {
    log_tail(&home.join("logs/daemon_error.log"))
}

fn log_tail(path: &Path) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(TAIL_LINES)..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Writes `count` numbered lines, `<prefix> 0` to `<prefix> count-1`, to `path`.
    fn write_numbered(path: &Path, prefix: &str, count: usize) {
        let text: String = (0..count)
            .map(|index| format!("{prefix} {index}\n"))
            .collect();
        fs::write(path, text).expect("write the log fixture");
    }

    #[test]
    fn readiness_diagnostics_carry_bounded_tails_of_the_routed_error_log_and_stderr() {
        let home = tempfile::tempdir().expect("temporary home");
        let logs = home.path().join("logs");
        fs::create_dir_all(&logs).expect("logs directory");
        write_numbered(&logs.join("daemon_error.log"), "stderr line", 100);
        write_numbered(&logs.join("errors.log"), "routed line", 100);

        let report = ready_diagnostics(home.path());
        let lines: Vec<&str> = report.lines().collect();

        assert!(
            lines.contains(&"routed line 99"),
            "the routed error log's tail is missing:\n{report}"
        );
        assert!(
            lines.contains(&"stderr line 99"),
            "the stderr tail is missing:\n{report}"
        );
        assert!(
            !lines.contains(&"routed line 0") && !lines.contains(&"stderr line 0"),
            "a tail is not bounded:\n{report}"
        );
        let routed = lines
            .iter()
            .filter(|line| line.starts_with("routed line "))
            .count();
        let stderr = lines
            .iter()
            .filter(|line| line.starts_with("stderr line "))
            .count();
        assert_eq!((routed, stderr), (TAIL_LINES, TAIL_LINES), "{report}");
    }
}
