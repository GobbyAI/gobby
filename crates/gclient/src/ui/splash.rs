// upstream: none (native Gobby connection splash)
//! The connection splash: the goblin beside the wordmark, alone on the
//! ground until startup draws its first frame.

use ratatui::layout::Rect;
use ratatui::Frame;

use super::marks::{self, MarkPalette};
use super::Chrome;

/// The wordmark starts this many columns right of the goblin's origin: the
/// haloed goblin's forty-one, then a gap of four.
const WORDMARK_X: u16 = 45;
/// The wordmark sits this many rows below the goblin's origin, on the
/// middle of the goblin under its halo.
const WORDMARK_Y: u16 = 6;

/// Draw the goblin and the wordmark centred in `area`; too narrow for both,
/// the wordmark alone, then the goblin alone, then nothing. Nothing else is
/// painted, so the ground shows through around them.
pub fn render_splash(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    let goblin = marks::goblin_large();
    let wordmark = marks::wordmark();
    let palette = MarkPalette::normal(&chrome.palette, chrome.prefs.monochrome);
    let centred = |cols: u16, rows: u16| {
        (area.width >= cols && area.height >= rows).then(|| {
            (
                area.x + (area.width - cols) / 2,
                area.y + (area.height - rows) / 2,
            )
        })
    };
    let both = (
        WORDMARK_X + wordmark.cols,
        goblin.rows.max(WORDMARK_Y + wordmark.rows),
    );
    if let Some((x, y)) = centred(both.0, both.1) {
        marks::render_mark(frame, (x, y), goblin, &palette);
        marks::render_mark(frame, (x + WORDMARK_X, y + WORDMARK_Y), wordmark, &palette);
    } else if let Some(origin) = centred(wordmark.cols, wordmark.rows) {
        marks::render_mark(frame, origin, wordmark, &palette);
    } else if let Some(origin) = centred(goblin.cols, goblin.rows) {
        marks::render_mark(frame, origin, goblin, &palette);
    }
}
