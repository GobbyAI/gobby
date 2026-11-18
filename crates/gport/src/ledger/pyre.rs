//! Python `re` selectors, read by fancy-regex.
//!
//! scripts/port/check_coverage.py compiles each selector with Python's `re`, and fancy-regex
//! reads some constructs differently. `translate` rewrites a Python pattern into fancy-regex
//! syntax: a letter escape or punctuation becomes a code point, `\Z` becomes `\z`, and braces
//! follow Python's quantifier rule. A construct whose meaning differs and cannot be rewritten is
//! refused, so the selector is reported BAD rather than run with another meaning. Where Python also
//! refuses the construct, the message is Python's wording.
//!
//! The refused set is deliberate, and it covers selectors Python would accept. Each construct
//! below is one fancy-regex matches differently from Python, and gport refuses it rather than chase
//! parity by reimplementing it, so a selector that uses one is a BAD line:
//!
//! - any backreference: `\1` to `\9` read as a group reference, and `(?P=name)`. That includes a
//!   reference to a group still open, as in `(a\1)`, and a case-insensitive reference.
//! - any named group, `(?P<name>...)`, whatever the name and however often it repeats.
//! - the verbose flag `x` in any inline-flag form: `(?x)`, `(?ix)`, `(?x:...)` and `(?-x:...)`.
//!   Python ignores the whitespace in verbose mode, and fancy-regex does not.
//! - the ignore-case flag `i` with a non-ASCII character in the selector, written as a character
//!   or as a code point such as `ı`.

/// Whitespace as Python's `\s` reads it: the Rust class plus U+001C to U+001F.
const SPACE: &str = r"[\s\x{1c}-\x{1f}]";
const NOT_SPACE: &str = r"[^\s\x{1c}-\x{1f}]";
/// The same members, for use inside an enclosing set.
const SET_SPACE: &str = r"\s\x{1c}-\x{1f}";

/// The fancy-regex form of the Python pattern `selector`, or the reason it is refused.
pub(crate) fn translate(selector: &str) -> Result<String, String> {
    let text: Vec<char> = selector.chars().collect();
    let mut scanner = Scanner {
        text: &text,
        at: 0,
        out: String::with_capacity(selector.len() + 8),
        prev: Prev::Empty,
        frames: Vec::new(),
        ignore_case: false,
    };
    while let Some(c) = scanner.peek() {
        match c {
            '\\' => scanner.escape()?,
            '[' => scanner.set()?,
            '(' => scanner.group()?,
            '{' => scanner.brace()?,
            '*' | '+' | '?' => scanner.quantifier()?,
            ')' => scanner.close_group()?,
            // An alternation makes a look-behind variable in width; it also leaves nothing for a
            // quantifier to repeat.
            '|' => {
                scanner.variable_width();
                scanner.at += 1;
                scanner.out.push('|');
                scanner.prev = Prev::Empty;
            }
            // An anchor leaves nothing for a quantifier to repeat.
            '^' | '$' => {
                scanner.at += 1;
                scanner.out.push(c);
                scanner.prev = Prev::Empty;
            }
            // Outside a set, Python reads a bare `]` or `}` as a literal.
            ']' | '}' => {
                scanner.at += 1;
                scanner.code(c);
                scanner.prev = Prev::Atom;
            }
            _ => {
                scanner.at += 1;
                scanner.out.push(c);
                scanner.prev = Prev::Atom;
            }
        }
    }
    if scanner.ignore_case && (!selector.is_ascii() || names_non_ascii(&scanner.out)) {
        return Err("the ignore-case flag with a non-ASCII character is refused".to_owned());
    }
    Ok(scanner.out)
}

/// Whether the fancy-regex text names a non-ASCII character: a raw character, or a `\x{..}` code
/// point above U+007F, which is how `translate` writes each character an escape stands for.
fn names_non_ascii(text: &str) -> bool {
    !text.is_ascii()
        || text.split("\\x{").skip(1).any(|rest| {
            rest.split('}')
                .next()
                .and_then(|hex| u32::from_str_radix(hex, 16).ok())
                .is_some_and(|value| value > 0x7f)
        })
}

/// What the last token was, which decides whether a quantifier has something to repeat.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Prev {
    /// Nothing yet, after `(` or `|`, or after an anchor.
    Empty,
    /// A character, class, escape or group: a quantifier may follow.
    Atom,
    /// A quantifier: only a lazy `?` may follow.
    Repeat,
    /// A lazy quantifier: nothing may follow.
    Lazy,
}

/// An open group: whether it is a look-behind, whether a quantifier or alternation has made its
/// text variable in width, and where it opens.
struct Frame {
    lookbehind: bool,
    variable: bool,
    at: usize,
}

/// The Python pattern being read, with the fancy-regex text written so far. `ignore_case` records
/// that an inline flag group turned `i` on or off, which `translate` checks at the end.
struct Scanner<'a> {
    text: &'a [char],
    at: usize,
    out: String,
    prev: Prev,
    frames: Vec<Frame>,
    ignore_case: bool,
}

impl Scanner<'_> {
    fn peek(&self) -> Option<char> {
        self.text.get(self.at).copied()
    }

    /// Open a group at `at`. A look-behind is checked for fixed width when it closes.
    fn open(&mut self, lookbehind: bool, at: usize) {
        self.frames.push(Frame {
            lookbehind,
            variable: false,
            at,
        });
    }

    /// A `)`: close the innermost group. Python requires a look-behind to have a fixed width, so a
    /// look-behind that a quantifier or alternation made variable is refused.
    fn close_group(&mut self) -> Result<(), String> {
        if let Some(frame) = self.frames.pop()
            && frame.lookbehind
            && frame.variable
        {
            return Err(format!(
                "look-behind requires fixed-width pattern at position {}",
                frame.at
            ));
        }
        self.at += 1;
        self.out.push(')');
        self.prev = Prev::Atom;
        Ok(())
    }

    /// A quantifier or alternation: every open look-behind is marked variable in width. This also
    /// refuses some fixed-width bodies that Python accepts, such as `(?<=a|b)`; refusing them fails
    /// safe, where accepting a variable-width look-behind would change a verdict.
    fn variable_width(&mut self) {
        for frame in self.frames.iter_mut().filter(|frame| frame.lookbehind) {
            frame.variable = true;
        }
    }

    fn bump(&mut self) -> Option<char> {
        let c = self.peek();
        if c.is_some() {
            self.at += 1;
        }
        c
    }

    /// Write `c` as a literal code point.
    fn code(&mut self, c: char) {
        self.out.push_str(&code_point(c as u32));
    }

    /// An escape outside a set. The backslash is at `self.at`.
    fn escape(&mut self) -> Result<(), String> {
        let start = self.at;
        self.at += 1;
        let Some(c) = self.bump() else {
            return Err(format!("bad escape (end of pattern) at position {start}"));
        };
        match c {
            'A' | 'b' | 'B' | 'd' | 'D' | 'w' | 'W' => self.out.push_str(&format!("\\{c}")),
            'Z' => self.out.push_str(r"\z"),
            's' => self.out.push_str(SPACE),
            'S' => self.out.push_str(NOT_SPACE),
            '0' => {
                let value = self.octal_value(c, start)?;
                self.out.push_str(&code_point(value));
            }
            '1'..='9' => self.group_or_octal(c, start)?,
            _ => self.literal_escape(c, start, false)?,
        }
        self.prev = match c {
            'A' | 'b' | 'B' | 'Z' => Prev::Empty,
            _ => Prev::Atom,
        };
        Ok(())
    }

    /// A quantifier `*`, `+` or `?` at `self.at`. After a quantifier, `?` makes it lazy; a `+`
    /// would make it possessive, which fancy-regex cannot express, so it is refused.
    fn quantifier(&mut self) -> Result<(), String> {
        let position = self.at;
        self.variable_width();
        match (self.prev, self.peek()) {
            (Prev::Repeat, Some('?')) => self.prev = Prev::Lazy,
            (Prev::Repeat, Some('+')) => {
                return Err(format!(
                    "possessive quantifier at position {position} is not supported"
                ));
            }
            _ => {
                self.repeatable(position)?;
                self.prev = Prev::Repeat;
            }
        }
        if let Some(c) = self.bump() {
            self.out.push(c);
        }
        Ok(())
    }

    /// Whether a quantifier at `position` has an atom to repeat.
    fn repeatable(&self, position: usize) -> Result<(), String> {
        match self.prev {
            Prev::Atom => Ok(()),
            Prev::Empty => Err(format!("nothing to repeat at position {position}")),
            Prev::Repeat | Prev::Lazy => Err(format!("multiple repeat at position {position}")),
        }
    }

    /// A `\1`..`\9` escape outside a set: an octal escape when three octal digits follow, as Python
    /// reads it. Otherwise it is a backreference, which is refused.
    fn group_or_octal(&mut self, first: char, start: usize) -> Result<(), String> {
        let mut digits = String::from(first);
        if let Some(second) = self.peek().filter(char::is_ascii_digit) {
            self.at += 1;
            digits.push(second);
            if is_octal(first)
                && is_octal(second)
                && let Some(third) = self.peek().filter(|c| is_octal(*c))
            {
                self.at += 1;
                digits.push(third);
                let value = u32::from_str_radix(&digits, 8).unwrap_or(u32::MAX);
                if value > 0o377 {
                    return Err(format!(
                        "octal escape \\{digits} outside of range 0-0o377 at position {start}"
                    ));
                }
                self.out.push_str(&code_point(value));
                return Ok(());
            }
        }
        Err(format!(
            "backreference \\{digits} at position {start} is refused"
        ))
    }

    /// The value of an octal escape whose first digit is `first`: up to two more octal digits.
    fn octal_value(&mut self, first: char, start: usize) -> Result<u32, String> {
        let mut value = first.to_digit(8).unwrap_or_default();
        for _ in 0..2 {
            match self.peek().and_then(|c| c.to_digit(8)) {
                Some(digit) => {
                    self.at += 1;
                    value = value * 8 + digit;
                }
                None => break,
            }
        }
        if value > 0o377 {
            return Err(format!(
                "octal escape value outside of range 0-0o377 at position {start}"
            ));
        }
        Ok(value)
    }

    /// The escapes that stand for one character: `\a \f \n \r \t \v`, `\x`, `\u`, `\U`, a
    /// punctuation or non-ASCII character, and `\b` inside a set (backspace). Any other ASCII
    /// letter is a bad escape, as Python reports it.
    fn literal_escape(&mut self, c: char, start: usize, in_set: bool) -> Result<(), String> {
        let value = match c {
            'a' => 0x07,
            'f' => 0x0c,
            'n' => 0x0a,
            'r' => 0x0d,
            't' => 0x09,
            'v' => 0x0b,
            'b' if in_set => 0x08,
            'x' => self.hex(2, start, c)?,
            'u' => self.hex(4, start, c)?,
            'U' => self.hex(8, start, c)?,
            'N' => return Err(format!("unsupported escape \\N at position {start}")),
            _ if c.is_ascii_alphabetic() => {
                return Err(format!("bad escape \\{c} at position {start}"));
            }
            _ => c as u32,
        };
        self.out.push_str(&code_point(value));
        Ok(())
    }

    /// The value of `\x`, `\u` or `\U`: exactly `count` hex digits.
    fn hex(&mut self, count: usize, start: usize, letter: char) -> Result<u32, String> {
        let mut digits = String::new();
        while digits.len() < count {
            match self.peek().filter(char::is_ascii_hexdigit) {
                Some(digit) => {
                    self.at += 1;
                    digits.push(digit);
                }
                None => break,
            }
        }
        if digits.len() != count {
            return Err(format!(
                "incomplete escape \\{letter}{digits} at position {start}"
            ));
        }
        u32::from_str_radix(&digits, 16).map_err(|error| error.to_string())
    }

    /// A set, from the `[` at `self.at` to its `]`. Python reads a nested `[` and the set operators
    /// `&&`, `||`, `~~` and `--` as plain characters; fancy-regex reads them as set syntax, so they
    /// are refused. A `]` first in the set (after an optional `^`) is a member.
    fn set(&mut self) -> Result<(), String> {
        let start = self.at;
        self.at += 1;
        self.out.push('[');
        if self.peek() == Some('^') {
            self.at += 1;
            self.out.push('^');
        }
        if self.peek() == Some(']') {
            self.at += 1;
            self.code(']');
        }
        loop {
            let at = self.at;
            let Some(c) = self.bump() else {
                return Err(format!("unterminated character set at position {start}"));
            };
            match c {
                ']' => {
                    self.out.push(']');
                    self.prev = Prev::Atom;
                    return Ok(());
                }
                '[' => return Err(format!("nested set at position {at}")),
                '\\' => self.set_escape(at)?,
                '&' | '|' | '~' if self.peek() == Some(c) => {
                    return Err(format!("set operator {c}{c} at position {at}"));
                }
                '-' if self.peek() == Some('-') => {
                    return Err(format!("set difference -- at position {at}"));
                }
                '&' | '|' | '~' => self.code(c),
                _ => self.out.push(c),
            }
        }
    }

    /// An escape inside a set. The backslash, at `start`, is already read.
    fn set_escape(&mut self, start: usize) -> Result<(), String> {
        let Some(c) = self.bump() else {
            return Err(format!("bad escape (end of pattern) at position {start}"));
        };
        match c {
            'd' | 'D' | 'w' | 'W' => self.out.push_str(&format!("\\{c}")),
            's' => self.out.push_str(SET_SPACE),
            'S' => return Err(format!("\\S inside a set at position {start}")),
            '0'..='7' => {
                let value = self.octal_value(c, start)?;
                self.out.push_str(&code_point(value));
            }
            '8' | '9' => return Err(format!("bad escape \\{c} at position {start}")),
            _ => self.literal_escape(c, start, true)?,
        }
        Ok(())
    }

    /// A group from the `(` at `self.at`. Python's `(?:`, `(?=`, `(?!`, look-behinds, `(?#...)`
    /// comments and inline flags are rewritten. A named group, `(?P<name>` or `(?P=name)`, is
    /// refused, and so is every other `(?` form, including the ones only fancy-regex has.
    fn group(&mut self) -> Result<(), String> {
        let start = self.at;
        let before = self.prev;
        self.at += 1;
        self.prev = Prev::Empty;
        if self.peek() != Some('?') {
            self.out.push('(');
            self.open(false, start);
            return Ok(());
        }
        self.at += 1;
        let Some(c) = self.bump() else {
            return Err(format!("unexpected end of pattern at position {start}"));
        };
        match c {
            ':' | '=' | '!' => {
                self.out.push_str(&format!("(?{c}"));
                self.open(false, start);
            }
            '<' => match self.bump() {
                Some(next @ ('=' | '!')) => {
                    self.out.push_str(&format!("(?<{next}"));
                    self.open(true, start);
                }
                other => {
                    let next = other.unwrap_or(' ');
                    return Err(unknown_extension(&format!("?<{next}"), start));
                }
            },
            'P' => self.named(start)?,
            '#' => {
                // A comment runs to the first `)` and is dropped; it leaves the previous token.
                self.until(')', start)?;
                self.prev = before;
            }
            _ if is_flag(c) || c == '-' => self.flags(c, start)?,
            _ => return Err(unknown_extension(&format!("?{c}"), start)),
        }
        Ok(())
    }

    /// `(?P<name>` or `(?P=name)`, with the `P` already read. Both are refused: a named group, and
    /// a backreference by name. Any other letter after `P` is an unknown extension, as Python reads
    /// it.
    fn named(&self, start: usize) -> Result<(), String> {
        match self.peek() {
            Some('<') => Err(format!("named group at position {start} is refused")),
            Some('=') => Err(format!(
                "named backreference at position {start} is refused"
            )),
            other => {
                let next = other.unwrap_or(' ');
                Err(unknown_extension(&format!("?P{next}"), start))
            }
        }
    }

    /// Inline flags, with the first character `first` already read: `(?on)`, `(?on:` or
    /// `(?on-off:`, read as Python's `_parse_flags` reads them. A `-` needs at least one flag after
    /// it and then a `:`, so a global `(?on)` takes no `-`. A flag may not be turned on and off in
    /// one group. `a` and `L` change meanings fancy-regex does not share, so they are refused; the
    /// default `u` is dropped when turned on and refused when turned off. `x` is refused in either
    /// form, and `i` is recorded for the non-ASCII check in `translate`.
    fn flags(&mut self, first: char, start: usize) -> Result<(), String> {
        let mut on = String::new();
        let mut off = String::new();
        let mut current = Some(first);
        while let Some(letter) = current.filter(|c| !matches!(c, '-' | ':' | ')')) {
            push_flag(&mut on, letter, start)?;
            current = self.bump();
        }
        if current == Some('-') {
            let mut turned_off = 0;
            current = self.bump();
            while let Some(letter) = current.filter(|c| !matches!(c, ':' | ')')) {
                if matches!(letter, 'a' | 'L' | 'u') {
                    return Err(format!(
                        "bad inline flags: cannot turn off flags 'a', 'u' and 'L' at position {start}"
                    ));
                }
                push_flag(&mut off, letter, start)?;
                turned_off += 1;
                current = self.bump();
            }
            if turned_off == 0 {
                return Err(format!("missing flag at position {start}"));
            }
            if current != Some(':') {
                return Err(format!("missing : at position {start}"));
            }
            if let Some(letter) = on.chars().find(|letter| off.contains(*letter)) {
                return Err(format!(
                    "bad inline flags: flag {letter} turned on and off at position {start}"
                ));
            }
        }
        if on.contains('x') || off.contains('x') {
            return Err(format!("verbose flag x is refused at position {start}"));
        }
        if on.contains('i') || off.contains('i') {
            self.ignore_case = true;
        }
        match current {
            Some(':') => {
                let mut prefix = format!("(?{on}");
                if !off.is_empty() {
                    prefix.push('-');
                    prefix.push_str(&off);
                }
                prefix.push(':');
                self.out.push_str(&prefix);
                self.open(false, start);
            }
            Some(')') => {
                // Python accepts global flags only at the start of the pattern.
                if start != 0 {
                    return Err(format!(
                        "global flags not at the start of the expression at position {start}"
                    ));
                }
                if !on.is_empty() {
                    self.out.push_str(&format!("(?{on})"));
                }
            }
            _ => return Err(format!("missing -, : or ) at position {start}")),
        }
        Ok(())
    }

    /// The text up to `end`, consumed with it.
    fn until(&mut self, end: char, start: usize) -> Result<String, String> {
        let mut text = String::new();
        loop {
            match self.bump() {
                Some(c) if c == end => return Ok(text),
                Some(c) => text.push(c),
                None => return Err(format!("missing {end} at position {start}")),
            }
        }
    }

    /// A `{` from `self.at`: a quantifier when Python reads one, otherwise a literal brace.
    /// Python reads `{m}`, `{m,}`, `{,n}`, `{m,n}` and `{,}`; `{}` is literal.
    fn brace(&mut self) -> Result<(), String> {
        let start = self.at;
        let (low, after_low) = self.digits_at(start + 1);
        let (comma, high, end) = if self.text.get(after_low) == Some(&',') {
            let (high, after_high) = self.digits_at(after_low + 1);
            (true, high, after_high)
        } else {
            (false, String::new(), after_low)
        };
        let closed = self.text.get(end) == Some(&'}');
        if !closed || (low.is_empty() && !comma) {
            self.at += 1;
            self.code('{');
            self.prev = Prev::Atom;
            return Ok(());
        }
        self.repeatable(start)?;
        self.variable_width();
        self.prev = Prev::Repeat;
        self.at = end + 1;
        let min = if low.is_empty() {
            0
        } else {
            parse_count(&low, start)?
        };
        if !comma {
            self.out.push_str(&format!("{{{min}}}"));
        } else if high.is_empty() {
            self.out.push_str(&format!("{{{min},}}"));
        } else {
            let max = parse_count(&high, start)?;
            if min > max {
                return Err(format!(
                    "min repeat greater than max repeat at position {start}"
                ));
            }
            self.out.push_str(&format!("{{{min},{max}}}"));
        }
        Ok(())
    }

    /// The ASCII digits from index `at`, and the index after them. Nothing is consumed.
    fn digits_at(&self, mut at: usize) -> (String, usize) {
        let mut digits = String::new();
        while let Some(c) = self.text.get(at).copied().filter(char::is_ascii_digit) {
            digits.push(c);
            at += 1;
        }
        (digits, at)
    }
}

fn code_point(value: u32) -> String {
    format!("\\x{{{value:x}}}")
}

fn is_octal(c: char) -> bool {
    c.is_digit(8)
}

fn is_flag(c: char) -> bool {
    matches!(c, 'a' | 'i' | 'L' | 'm' | 's' | 'u' | 'x')
}

/// Add one flag letter, turned on or off, to `flags`. `a` and `L` are refused, `u` is the default
/// and adds nothing, and a letter outside Python's flag set is unknown.
fn push_flag(flags: &mut String, letter: char, start: usize) -> Result<(), String> {
    match letter {
        'a' | 'L' => Err(format!(
            "unsupported inline flag {letter} at position {start}"
        )),
        'u' => Ok(()),
        'i' | 'm' | 's' | 'x' => {
            flags.push(letter);
            Ok(())
        }
        _ => Err(format!("unknown flag {letter} at position {start}")),
    }
}

fn parse_count(digits: &str, start: usize) -> Result<u32, String> {
    digits
        .parse::<u32>()
        .map_err(|_| format!("the repetition number is too large at position {start}"))
}

fn unknown_extension(extension: &str, start: usize) -> String {
    format!("unknown extension {extension} at position {start}")
}
