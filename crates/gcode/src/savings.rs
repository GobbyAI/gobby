//! Direct DB savings tracking for gcode.
//!
//! Records token savings to the `savings_ledger` table when gcode returns
//! compact symbol/outline data instead of full file contents.
//!
//! Display output follows the gsqz pattern: stderr prefix showing savings.
//!
//! Source: src/gobby/savings/tracker.py (schema, CHARS_PER_TOKEN)

use rusqlite::Connection;

/// Empirical chars-per-token for code-heavy content.
/// Matches Python: CHARS_PER_TOKEN in savings/tracker.py
const CHARS_PER_TOKEN: f64 = 3.7;

/// Calculate savings percentage.
pub fn savings_pct(original_chars: usize, actual_chars: usize) -> f64 {
    if original_chars == 0 {
        return 0.0;
    }
    (1.0 - actual_chars as f64 / original_chars as f64) * 100.0
}

/// Record a savings event to the savings_ledger table.
///
/// Best-effort: returns Ok(()) even if the table doesn't exist or the write fails,
/// matching the Python pattern of never letting savings tracking break functionality.
pub fn record_savings(
    conn: &Connection,
    category: &str,
    original_chars: usize,
    actual_chars: usize,
    project_id: Option<&str>,
    metadata: Option<&str>,
) -> anyhow::Result<()> {
    let original_tokens = (original_chars as f64 / CHARS_PER_TOKEN) as i64;
    let actual_tokens = (actual_chars as f64 / CHARS_PER_TOKEN) as i64;
    let tokens_saved = (original_tokens - actual_tokens).max(0);

    let result = conn.execute(
        "INSERT INTO savings_ledger \
         (session_id, project_id, category, original_tokens, actual_tokens, \
          tokens_saved, cost_saved_usd, model, metadata) \
         VALUES (NULL, ?1, ?2, ?3, ?4, ?5, 0.0, NULL, ?6)",
        rusqlite::params![
            project_id,
            category,
            original_tokens,
            actual_tokens,
            tokens_saved,
            metadata,
        ],
    );

    // Best-effort: don't fail if table missing or write error
    if let Err(e) = result {
        eprintln!("Warning: failed to record savings: {e}");
    }

    Ok(())
}

/// Print savings info to stderr in gsqz-style format.
pub fn print_savings(label: &str, original_chars: usize, actual_chars: usize) {
    if original_chars <= actual_chars || original_chars == 0 {
        return;
    }
    let pct = savings_pct(original_chars, actual_chars);
    eprintln!("[gcode \u{2014} {label}, saved {pct:.0}% ({actual_chars}B vs {original_chars}B)]");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_chars_to_tokens() {
        // 3700 chars / 3.7 = 1000 tokens
        let tokens = (3700.0 / CHARS_PER_TOKEN) as i64;
        assert_eq!(tokens, 1000);
    }

    #[test]
    fn test_savings_pct_basic() {
        let pct = savings_pct(1000, 200);
        assert!((pct - 80.0).abs() < 0.01);
    }

    #[test]
    fn test_savings_pct_zero_original() {
        assert_eq!(savings_pct(0, 0), 0.0);
    }

    #[test]
    fn test_savings_pct_no_savings() {
        assert!((savings_pct(100, 100)).abs() < 0.01);
    }

    #[test]
    fn test_record_savings_insert() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE savings_ledger (
                session_id TEXT,
                project_id TEXT,
                category TEXT NOT NULL,
                original_tokens INTEGER NOT NULL,
                actual_tokens INTEGER NOT NULL,
                tokens_saved INTEGER NOT NULL,
                cost_saved_usd REAL NOT NULL,
                model TEXT,
                metadata TEXT
            )",
        )
        .unwrap();

        record_savings(
            &conn,
            "code_index",
            3700,
            370,
            Some("test-project"),
            Some(r#"{"symbol":"foo"}"#),
        )
        .unwrap();

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM savings_ledger", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);

        let (original, actual, saved): (i64, i64, i64) = conn
            .query_row(
                "SELECT original_tokens, actual_tokens, tokens_saved FROM savings_ledger",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(original, 1000);
        assert_eq!(actual, 100);
        assert_eq!(saved, 900);
    }
}
