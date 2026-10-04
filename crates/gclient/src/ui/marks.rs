// upstream: none (native Gobby mark parser and renderer)

use crate::theme::{Palette, ThemeKind};
use ratatui::style::Color;
use ratatui::Frame;
use std::fmt;
use std::sync::OnceLock;

const GOBLIN_LARGE: &str = include_str!("../../assets/marks/goblin-41x18.grid");
const WORDMARK: &str = include_str!("../../assets/marks/wordmark-braille-54x8.txt");
const WORDMARK_SHADOW: &str = include_str!("../../assets/marks/wordmark-shadow-49x9.grid");

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MarkKind {
    Halfblock,
    Braille,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Role {
    Accent,
    Overlay1,
    Ink,
    Glint,
    Dim,
    Node,
    Edge,
    Transparent,
}

impl Role {
    fn parse(letter: char, line: usize) -> Result<Self, MarkError> {
        match letter {
            'a' => Ok(Self::Accent),
            'o' => Ok(Self::Overlay1),
            'i' => Ok(Self::Ink),
            'g' => Ok(Self::Glint),
            'd' => Ok(Self::Dim),
            'n' => Ok(Self::Node),
            'e' => Ok(Self::Edge),
            '.' => Ok(Self::Transparent),
            _ => Err(MarkError::new(
                line,
                format!("invalid halfblock role {letter:?}"),
            )),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Cell {
    Halfblock { upper: Role, lower: Role },
    Braille(char),
}

#[derive(Debug)]
pub struct Mark {
    pub kind: MarkKind,
    pub cols: u16,
    pub rows: u16,
    cells: Vec<Cell>,
}

#[derive(Debug)]
pub struct MarkError {
    line: usize,
    reason: String,
}

impl MarkError {
    fn new(line: usize, reason: impl Into<String>) -> Self {
        Self {
            line,
            reason: reason.into(),
        }
    }
}

impl fmt::Display for MarkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}: {}", self.line, self.reason)
    }
}

impl std::error::Error for MarkError {}

pub fn parse(text: &str) -> Result<Mark, MarkError> {
    if !text.ends_with('\n') {
        return Err(MarkError::new(
            text.split('\n').count(),
            "mark must end with LF",
        ));
    }
    let mut lines = text.split_terminator('\n').enumerate();
    let header = lines
        .next()
        .map(|(_, line)| line)
        .ok_or_else(|| MarkError::new(1, "missing header"))?;
    let (kind, cols, rows) = parse_header(header)?;
    let mut cells = Vec::new();
    let mut data_rows = 0usize;

    for (index, line) in lines {
        if line.starts_with('#') {
            continue;
        }
        let line_number = index + 1;
        if data_rows == usize::from(rows) {
            return Err(MarkError::new(line_number, "too many data rows"));
        }
        let width = match kind {
            MarkKind::Halfblock => usize::from(cols) * 2,
            MarkKind::Braille => usize::from(cols),
        };
        if line.chars().count() != width {
            return Err(MarkError::new(
                line_number,
                format!("data width must be {width} characters"),
            ));
        }
        match kind {
            MarkKind::Halfblock => {
                let mut letters = line.chars();
                while let (Some(upper), Some(lower)) = (letters.next(), letters.next()) {
                    cells.push(Cell::Halfblock {
                        upper: Role::parse(upper, line_number)?,
                        lower: Role::parse(lower, line_number)?,
                    });
                }
            }
            MarkKind::Braille => {
                for glyph in line.chars() {
                    if !('\u{2800}'..='\u{28ff}').contains(&glyph) {
                        return Err(MarkError::new(
                            line_number,
                            format!("invalid braille glyph {glyph:?}"),
                        ));
                    }
                    cells.push(Cell::Braille(glyph));
                }
            }
        }
        data_rows += 1;
    }
    if data_rows != usize::from(rows) {
        return Err(MarkError::new(
            text.split_terminator('\n').count() + 1,
            format!("expected {rows} data rows, found {data_rows}"),
        ));
    }
    Ok(Mark {
        kind,
        cols,
        rows,
        cells,
    })
}

fn parse_header(header: &str) -> Result<(MarkKind, u16, u16), MarkError> {
    let (kind, size) = header
        .strip_prefix("# ")
        .and_then(|rest| rest.split_once(' '))
        .ok_or_else(|| MarkError::new(1, "invalid header"))?;
    let kind = match kind {
        "halfblock" => MarkKind::Halfblock,
        "braille" => MarkKind::Braille,
        _ => return Err(MarkError::new(1, "invalid header kind")),
    };
    if size.chars().any(char::is_whitespace) {
        return Err(MarkError::new(1, "invalid header size"));
    }
    let (cols, rows) = size
        .split_once('x')
        .ok_or_else(|| MarkError::new(1, "invalid header size"))?;
    let cols = cols
        .parse::<u16>()
        .map_err(|_| MarkError::new(1, "invalid header columns"))?;
    let rows = rows
        .parse::<u16>()
        .map_err(|_| MarkError::new(1, "invalid header rows"))?;
    if cols == 0 || rows == 0 {
        return Err(MarkError::new(1, "header dimensions must be positive"));
    }
    Ok((kind, cols, rows))
}

pub fn goblin_large() -> &'static Mark {
    static MARK: OnceLock<Mark> = OnceLock::new();
    // The committed asset format and size are checked by the marks integration test.
    MARK.get_or_init(|| parse(GOBLIN_LARGE).expect("valid large goblin asset"))
}

pub fn wordmark() -> &'static Mark {
    static MARK: OnceLock<Mark> = OnceLock::new();
    MARK.get_or_init(|| parse(WORDMARK).expect("valid wordmark asset"))
}

pub fn wordmark_shadow() -> &'static Mark {
    static MARK: OnceLock<Mark> = OnceLock::new();
    MARK.get_or_init(|| parse(WORDMARK_SHADOW).expect("valid wordmark shadow asset"))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MarkPalette {
    pub accent: Option<Color>,
    pub overlay1: Option<Color>,
    pub ink: Option<Color>,
    pub glint: Option<Color>,
    pub dim: Option<Color>,
    /// The halo's network nodes and the edges between them.
    pub node: Option<Color>,
    pub edge: Option<Color>,
    pub braille: Color,
}

impl MarkPalette {
    /// Halo nodes are info blue and edges `overlay0`; in monochrome, where
    /// info is only a grey, nodes take `dim` and edges `surface1`.
    pub fn normal(palette: &Palette, monochrome: bool) -> Self {
        let (node, edge) = if monochrome {
            (palette.dim, palette.surface1)
        } else {
            (palette.blue, palette.overlay0)
        };
        Self {
            accent: Some(palette.accent),
            overlay1: Some(palette.overlay1),
            ink: Some(palette.ink),
            glint: Some(palette.glint),
            dim: Some(palette.dim),
            node: Some(node),
            edge: Some(edge),
            braille: palette.wordmark,
        }
    }

    pub fn shadow(palette: &Palette, monochrome: bool) -> Self {
        Self {
            braille: palette.dim,
            ..Self::normal(palette, monochrome)
        }
    }

    /// Every role keeps its own colour, and the eye glint stays the
    /// splash's white, so the dimmed goblin still has both eyes (#23280).
    /// The halo's nodes take the dimmed fill and its edges the dimmed tablet.
    pub fn dimmed(palette: &Palette, kind: ThemeKind) -> Self {
        let (accent, overlay1, ink, dim) = match kind {
            ThemeKind::Dark => (
                palette.overlay0,
                palette.dim,
                palette.panel_bg,
                palette.panel_bg,
            ),
            ThemeKind::Light => (
                palette.surface1,
                palette.overlay0,
                palette.subtext0,
                palette.overlay0,
            ),
        };
        Self {
            accent: Some(accent),
            overlay1: Some(overlay1),
            ink: Some(ink),
            glint: Some(palette.glint),
            dim: Some(dim),
            node: Some(accent),
            edge: Some(overlay1),
            braille: palette.wordmark,
        }
    }

    fn color(self, role: Role) -> Option<Color> {
        match role {
            Role::Accent => self.accent,
            Role::Overlay1 => self.overlay1,
            Role::Ink => self.ink,
            Role::Glint => self.glint,
            Role::Dim => self.dim,
            Role::Node => self.node,
            Role::Edge => self.edge,
            Role::Transparent => None,
        }
    }
}

pub fn render_mark(frame: &mut Frame, origin: (u16, u16), mark: &Mark, palette: &MarkPalette) {
    let area = frame.area();
    for row in 0..mark.rows {
        for col in 0..mark.cols {
            let x = u32::from(origin.0) + u32::from(col);
            let y = u32::from(origin.1) + u32::from(row);
            if x < u32::from(area.x)
                || y < u32::from(area.y)
                || x >= u32::from(area.right())
                || y >= u32::from(area.bottom())
            {
                continue;
            }
            let Some(cell) = frame.buffer_mut().cell_mut((x as u16, y as u16)) else {
                continue;
            };
            let index = usize::from(row) * usize::from(mark.cols) + usize::from(col);
            let Some(mark_cell) = mark.cells.get(index) else {
                continue;
            };
            match *mark_cell {
                Cell::Halfblock { upper, lower } => {
                    match (palette.color(upper), palette.color(lower)) {
                        (None, None) => {}
                        (Some(upper), None) => {
                            cell.set_symbol("▀").set_fg(upper);
                        }
                        (None, Some(lower)) => {
                            cell.set_symbol("▄").set_fg(lower);
                        }
                        (Some(upper), Some(lower)) => {
                            cell.set_symbol("▀").set_fg(upper).set_bg(lower);
                        }
                    }
                }
                Cell::Braille('\u{2800}') => {}
                Cell::Braille(glyph) => {
                    let mut encoded = [0; 4];
                    cell.set_symbol(glyph.encode_utf8(&mut encoded))
                        .set_fg(palette.braille);
                }
            }
        }
    }
}
