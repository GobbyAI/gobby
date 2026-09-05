// upstream: herdr v0.8.0 src/ui/sidebar/tokens.rs
//! Token-usage column: compact formatting of Gobby session token stats.
//!
//! herdr's `tokens.rs` resolves configured sidebar row tokens; Gobby keeps
//! the separator rule (`" "` after a glyph, `" · "` elsewhere, see
//! `sidebar_rows`) and adds the compact count column herdr never had.

use crate::ui::text::display_width;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TokenStats {
    pub input: u64,
    pub output: u64,
    pub context_used: u64,
    pub context_limit: u64,
}

/// herdr compact count formatting (`1.2k`, `34k`, `1.5m`).
pub fn format_compact(count: u64) -> String {
    const UNITS: [(u64, &str); 3] = [(1_000_000_000, "g"), (1_000_000, "m"), (1_000, "k")];
    for (base, suffix) in UNITS {
        if count < base {
            continue;
        }
        let whole = count / base;
        if whole >= 10 {
            return format!("{whole}{suffix}");
        }
        let tenths = (count % base) * 10 / base;
        return if tenths == 0 {
            format!("{whole}{suffix}")
        } else {
            format!("{whole}.{tenths}{suffix}")
        };
    }
    count.to_string()
}

/// Column text for a row at `width` columns, or empty when it cannot fit.
///
/// Prefers context pressure (`used/limit`) over raw counts, and drops detail
/// from the right until a form fits.
pub fn token_column(stats: TokenStats, width: u16) -> String {
    let width = usize::from(width);
    let candidates = if stats.context_limit > 0 {
        vec![
            format!(
                "{}/{}",
                format_compact(stats.context_used),
                format_compact(stats.context_limit)
            ),
            format_compact(stats.context_used),
        ]
    } else {
        vec![
            format!(
                "{}↑ {}↓",
                format_compact(stats.input),
                format_compact(stats.output)
            ),
            format_compact(stats.input.saturating_add(stats.output)),
        ]
    };
    candidates
        .into_iter()
        .find(|text| display_width(text) <= width)
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_counts_round_down_to_one_decimal() {
        assert_eq!(format_compact(0), "0");
        assert_eq!(format_compact(999), "999");
        assert_eq!(format_compact(1_000), "1k");
        assert_eq!(format_compact(1_250), "1.2k");
        assert_eq!(format_compact(34_900), "34k");
        assert_eq!(format_compact(1_500_000), "1.5m");
        assert_eq!(format_compact(12_000_000), "12m");
        assert_eq!(format_compact(2_300_000_000), "2.3g");
    }

    #[test]
    fn token_column_prefers_context_pressure_and_shrinks_to_fit() {
        let stats = TokenStats {
            input: 1_200,
            output: 340,
            context_used: 45_000,
            context_limit: 200_000,
        };
        assert_eq!(token_column(stats, 12), "45k/200k");
        assert_eq!(token_column(stats, 5), "45k");
        assert_eq!(token_column(stats, 2), "");
    }

    #[test]
    fn token_column_without_limit_shows_io_counts() {
        let stats = TokenStats {
            input: 1_200,
            output: 340,
            ..TokenStats::default()
        };
        assert_eq!(token_column(stats, 12), "1.2k↑ 340↓");
        assert_eq!(token_column(stats, 5), "1.5k");
    }
}
