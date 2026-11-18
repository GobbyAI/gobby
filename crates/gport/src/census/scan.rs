//! Source walking and the identifier scan shared by the census sections.

use std::collections::HashSet;
use std::path::Path;

use walkdir::{DirEntry, WalkDir};

/// The directory whose top-level packages the census describes.
pub const GOBBY_PREFIX: &str = "src/gobby/";

/// A UTF-8 text file under a census root. `rel` is its path from the root, with `/` separators.
pub struct TextFile {
    pub rel: String,
    pub text: String,
}

/// A file or directory under a census root that the census did not read, and why.
pub struct Skipped {
    /// The path from the census root, with `/` separators.
    pub path: String,
    pub reason: String,
}

impl TextFile {
    /// The path below `src/gobby/`, or the relative path itself for a file outside it.
    pub fn inner(&self) -> &str {
        self.rel.strip_prefix(GOBBY_PREFIX).unwrap_or(&self.rel)
    }

    /// The top-level package: the first directory below `src/gobby/`. `None` for a file directly
    /// in that directory and for a file outside it.
    pub fn package(&self) -> Option<&str> {
        self.rel
            .strip_prefix(GOBBY_PREFIX)?
            .split_once('/')
            .map(|(dir, _)| dir)
    }

    /// Whether the file is a Python module under `src/gobby/`.
    pub fn is_gobby_python(&self) -> bool {
        self.rel.starts_with(GOBBY_PREFIX) && self.rel.ends_with(".py")
    }
}

/// Every UTF-8 text file under `root/sub`, sorted by path. Binary files and `__pycache__`
/// directories are skipped silently. Recorded in `skipped` instead: an entry that cannot be walked
/// or read, a `.py` file that is not UTF-8, and a text file whose path is not UTF-8. A missing `sub`
/// yields no files.
pub fn text_files(root: &Path, sub: &str, skipped: &mut Vec<Skipped>) -> Vec<TextFile> {
    let base = root.join(sub);
    if !base.exists() {
        return Vec::new();
    }
    let mut files = Vec::new();
    let walk = WalkDir::new(&base)
        .sort_by_file_name()
        .into_iter()
        .filter_entry(|entry| !is_pycache(entry));
    for entry in walk {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                let reason = match error.io_error() {
                    Some(io) => format!("cannot walk: {io}"),
                    None => format!("cannot walk: {error}"),
                };
                let path = display_path(root, error.path().unwrap_or(base.as_path()));
                skipped.push(Skipped { path, reason });
                continue;
            }
        };
        if !entry.file_type().is_file() {
            continue;
        }
        let path = entry.path();
        let bytes = match std::fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) => {
                skipped.push(Skipped {
                    path: display_path(root, path),
                    reason: format!("cannot read: {error}"),
                });
                continue;
            }
        };
        let Ok(text) = String::from_utf8(bytes) else {
            if path.extension().is_some_and(|ext| ext == "py") {
                skipped.push(Skipped {
                    path: display_path(root, path),
                    reason: "not UTF-8".to_owned(),
                });
            }
            continue;
        };
        let Some(rel) = relative(root, path) else {
            skipped.push(Skipped {
                path: display_path(root, path),
                reason: "path is not UTF-8".to_owned(),
            });
            continue;
        };
        files.push(TextFile { rel, text });
    }
    files.sort_by(|a, b| a.rel.cmp(&b.rel));
    files
}

fn is_pycache(entry: &DirEntry) -> bool {
    entry.depth() > 0 && entry.file_type().is_dir() && entry.file_name() == "__pycache__"
}

/// `path` below `root`, with each component shown lossily. Used only to name a skipped entry.
fn display_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
}

/// `path` below `root` with `/` separators, or `None` when it is not below `root` or a component
/// is not UTF-8.
fn relative(root: &Path, path: &Path) -> Option<String> {
    let inner = path.strip_prefix(root).ok()?;
    let parts = inner
        .components()
        .map(|part| part.as_os_str().to_str())
        .collect::<Option<Vec<&str>>>()?;
    Some(parts.join("/"))
}

/// The top-level package directories under `src/gobby/`, sorted and without repeats.
pub fn packages(files: &[TextFile]) -> Vec<String> {
    let mut names: Vec<String> = files
        .iter()
        .filter_map(TextFile::package)
        .map(str::to_owned)
        .collect();
    names.sort();
    names.dedup();
    names
}

/// An identifier in source text, the identifier before a `.` that sits directly before it
/// (`functools` in `functools.cache`), and whether the identifier is an assignment target.
pub struct Token<'a> {
    pub ident: &'a str,
    pub qualifier: Option<&'a str>,
    /// True when the identifier is code: outside every string literal and comment.
    pub code: bool,
    /// True when the identifier is the last name of an assignment target, as `Source` reads
    /// targets: a name or a dotted name in a plain (`x = v`), chained (`a = x = v`) or annotated
    /// (`x: T = v`) statement, or in a tuple, list or parenthesised target list. A call, a subscript,
    /// a lambda parameter, a keyword argument, a read or a comparison is not one.
    pub assigned: bool,
}

/// Every identifier in `text`, in order, including those in strings and comments, which have
/// `code` false. A run of word characters that starts with a digit is not an identifier.
pub fn tokens(text: &str) -> Vec<Token<'_>> {
    let source = Source::new(text.as_bytes());
    let bytes = source.bytes;
    let targets = source.assignment_targets();
    let mut found = Vec::new();
    let mut previous: Option<(&str, usize)> = None;
    let mut at = 0;
    while at < bytes.len() {
        if !is_word(bytes[at]) {
            at += 1;
            continue;
        }
        let start = at;
        while at < bytes.len() && is_word(bytes[at]) {
            at += 1;
        }
        if bytes[start].is_ascii_digit() {
            previous = None;
            continue;
        }
        let ident = &text[start..at];
        let qualifier = match previous {
            Some((name, end)) if start > 0 && bytes[start - 1] == b'.' && end == start - 1 => {
                Some(name)
            }
            _ => None,
        };
        found.push(Token {
            ident,
            qualifier,
            code: source.kind_at(start) == Kind::Code,
            assigned: targets.contains(&start),
        });
        previous = Some((ident, at));
    }
    found
}

/// The dotted name of every decorator in `text`: a line whose first non-blank byte is `@`, outside
/// any string, comment or bracket. `functools.lru_cache` is the name in
/// `@functools.lru_cache(maxsize=None)`.
pub fn decorators(text: &str) -> Vec<&str> {
    let source = Source::new(text.as_bytes());
    let bytes = source.bytes;
    let mut names = Vec::new();
    for (at, &byte) in bytes.iter().enumerate() {
        let is_decorator = byte == b'@'
            && source.kind_at(at) == Kind::Code
            && source.depth_at(at) <= 0
            && only_blanks_before(bytes, at);
        if !is_decorator {
            continue;
        }
        let start = skip_blanks(bytes, at + 1);
        if let Some(name) = text
            .get(start..dotted_end(bytes, start))
            .filter(|name| !name.is_empty())
        {
            names.push(name);
        }
    }
    names
}

/// The index just past the dotted name that starts at `start`: identifiers joined by dots.
fn dotted_end(bytes: &[u8], start: usize) -> usize {
    let mut end = start;
    loop {
        let word = word_end(bytes, end);
        if word == end {
            return end;
        }
        end = word;
        let dotted =
            bytes.get(end) == Some(&b'.') && bytes.get(end + 1).is_some_and(|&b| is_word(b));
        if !dotted {
            return end;
        }
        end += 1;
    }
}

/// The index just past the run of word characters that starts at `at`.
fn word_end(bytes: &[u8], mut at: usize) -> usize {
    while bytes.get(at).is_some_and(|&b| is_word(b)) {
        at += 1;
    }
    at
}

/// What a byte of a Python source is.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    /// Code outside any string literal and comment.
    Code,
    /// A string literal, quotes included.
    Text,
    /// A comment, from its `#` up to the line break that ends it.
    Comment,
}

/// The bracket depth before each byte of a Python source, and what each byte is. A bracket inside
/// a string literal or a comment does not count, so the depth stays right past quoted brackets.
struct Source<'a> {
    bytes: &'a [u8],
    depth: Vec<i32>,
    kind: Vec<Kind>,
}

impl<'a> Source<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        let mut source = Self {
            bytes,
            depth: Vec::with_capacity(bytes.len()),
            kind: Vec::with_capacity(bytes.len()),
        };
        let mut level = 0;
        let mut at = 0;
        while at < bytes.len() {
            let (end, kind) = match bytes[at] {
                b'#' => (line_end(bytes, at), Kind::Comment),
                b'"' | b'\'' => (string_end(bytes, at), Kind::Text),
                _ => (at + 1, Kind::Code),
            };
            for &inner in &bytes[at..end] {
                source.depth.push(level);
                source.kind.push(kind);
                if kind == Kind::Code {
                    match inner {
                        b'(' | b'[' | b'{' => level += 1,
                        b')' | b']' | b'}' => level -= 1,
                        _ => {}
                    }
                }
            }
            at = end;
        }
        source
    }

    fn kind_at(&self, at: usize) -> Kind {
        self.kind.get(at).copied().unwrap_or(Kind::Code)
    }

    fn depth_at(&self, at: usize) -> i32 {
        self.depth.get(at).copied().unwrap_or(0)
    }

    /// The start of the last name of every assignment target in code. Each statement runs from its
    /// start to a line break outside brackets, or to a `;` at bracket depth 0. Within it, each single
    /// `=` at depth 0 ends a segment. A segment is read as a target list, and a later segment only
    /// while every earlier one was one, so the first segment that is not, such as the `lambda x` of
    /// `return a, lambda x=1: x`, ends the reading.
    fn assignment_targets(&self) -> HashSet<usize> {
        let mut targets = HashSet::new();
        let mut from = 0;
        while from < self.bytes.len() {
            let to = self.statement_end(from);
            self.statement_targets(from, to, &mut targets);
            from = to + 1;
        }
        targets
    }

    /// The index of the byte that ends the statement that starts at `from`, or the text's length.
    fn statement_end(&self, from: usize) -> usize {
        (from..self.bytes.len())
            .find(|&at| {
                self.kind_at(at) == Kind::Code
                    && self.depth_at(at) <= 0
                    && match self.bytes[at] {
                        b';' => true,
                        b'\n' => {
                            !(at > 0
                                && self.kind_at(at - 1) == Kind::Code
                                && self.bytes[at - 1] == b'\\')
                        }
                        _ => false,
                    }
            })
            .unwrap_or(self.bytes.len())
    }

    /// Adds the targets of the statement `from..to` to `targets`, segment by segment.
    fn statement_targets(&self, from: usize, to: usize, targets: &mut HashSet<usize>) {
        let mut segment = from;
        for at in (from..to).filter(|&at| self.is_plain_equals(at)) {
            let names = if segment == from {
                self.leading_target(from, at)
            } else {
                self.target_list(segment, at, 0)
            };
            let Some(names) = names else {
                break;
            };
            targets.extend(names);
            segment = at + 1;
        }
    }

    /// The targets before a statement's first `=`. An annotation, `name: T`, targets its name alone,
    /// and a compound header such as `if x:` or `else:` leaves its body, after the colon, to be read.
    fn leading_target(&self, from: usize, equals: usize) -> Option<Vec<usize>> {
        let colon = (from..equals).find(|&at| self.is_top_byte(at, b':'));
        let annotated = colon.and_then(|colon| {
            let (a, b) = self.trimmed(from, colon)?;
            let last = self.name_chain(a, b)?;
            (!is_compound_keyword(&self.bytes[a..b])).then_some(vec![last])
        });
        annotated.or_else(|| self.target_list(colon.map_or(from, |colon| colon + 1), equals, 0))
    }

    /// The last name of each element of the target list `from..to` at bracket depth `level`, or
    /// `None` when an element is not a target. Elements split at the commas of that depth, and an
    /// empty one, such as the one after a trailing comma, is skipped.
    fn target_list(&self, from: usize, to: usize, level: i32) -> Option<Vec<usize>> {
        let mut names = Vec::new();
        let mut element = from;
        for at in from..=to {
            let ends = at == to
                || (self.kind_at(at) == Kind::Code
                    && self.bytes[at] == b','
                    && self.depth_at(at) == level);
            if ends {
                if !self.target_element(element, at, level, &mut names) {
                    return None;
                }
                element = at + 1;
            }
        }
        Some(names)
    }

    /// Whether `from..to` is one element of a target list, and if so pushes its last name onto
    /// `names`. An element is a name chain, optionally starred, a parenthesised or bracketed target
    /// list, or a subscript, call or attribute chain with no blank or colon at its own level. A
    /// `lambda` never starts one.
    fn target_element(&self, from: usize, to: usize, level: i32, names: &mut Vec<usize>) -> bool {
        let Some((mut a, b)) = self.trimmed(from, to) else {
            return true;
        };
        if self.bytes[a] == b'*' {
            let Some((next, _)) = self.trimmed(a + 1, b) else {
                return false;
            };
            a = next;
        }
        if starts_with_word(&self.bytes[a..b], b"lambda") {
            return false;
        }
        if self.is_wrapped(a, b, level) {
            let Some(inner) = self.target_list(a + 1, b - 1, level + 1) else {
                return false;
            };
            names.extend(inner);
            true
        } else if let Some(last) = self.name_chain(a, b) {
            names.push(last);
            true
        } else {
            self.is_opaque(a, b, level)
        }
    }

    /// Whether `a..b` is a parenthesised or bracketed group around its whole element: the opener and
    /// closer sit at depth `level`, and everything between them is nested deeper.
    fn is_wrapped(&self, a: usize, b: usize, level: i32) -> bool {
        let close = b - 1;
        self.kind_at(a) == Kind::Code
            && matches!(self.bytes[a], b'(' | b'[')
            && self.kind_at(close) == Kind::Code
            && matches!(self.bytes[close], b')' | b']')
            && self.depth_at(close) == level + 1
            && (a + 1..close).all(|at| self.depth_at(at) > level)
    }

    /// Whether `a..b` is a subscript, call or attribute chain such as `rows[i]`: nothing at its own
    /// level is a blank, a colon or a string, so it is not a lambda or a keyword form.
    fn is_opaque(&self, a: usize, b: usize, level: i32) -> bool {
        (a..b).all(|at| {
            self.depth_at(at) > level
                || (self.kind_at(at) == Kind::Code && !self.is_blank(at) && self.bytes[at] != b':')
        })
    }

    /// The start of the last name of the dotted name chain that fills `from..to`, as `_a_cache` or
    /// `self._a_cache` does.
    fn name_chain(&self, from: usize, to: usize) -> Option<usize> {
        let mut at = from;
        loop {
            let start = at;
            let begins = start < to
                && self.kind_at(start) == Kind::Code
                && (self.bytes[start].is_ascii_alphabetic() || self.bytes[start] == b'_');
            if !begins {
                return None;
            }
            while at < to && is_word(self.bytes[at]) {
                at += 1;
            }
            if at == to {
                return Some(start);
            }
            if self.bytes[at] != b'.' {
                return None;
            }
            at += 1;
        }
    }

    /// Whether `at` is blank inside a target: a space, tab, carriage return or line break in code, a
    /// backslash that continues the line, or any byte of a comment.
    fn is_blank(&self, at: usize) -> bool {
        match self.kind_at(at) {
            Kind::Comment => true,
            Kind::Text => false,
            Kind::Code => {
                let byte = self.bytes[at];
                matches!(byte, b' ' | b'\t' | b'\r' | b'\n')
                    || (byte == b'\\' && self.bytes.get(at + 1) == Some(&b'\n'))
            }
        }
    }

    /// The bounds of `from..to` without blanks at either end, or `None` when none is left.
    fn trimmed(&self, from: usize, to: usize) -> Option<(usize, usize)> {
        let start = (from..to).find(|&at| !self.is_blank(at)).unwrap_or(to);
        let end = (start..to)
            .rev()
            .find(|&at| !self.is_blank(at))
            .map_or(start, |at| at + 1);
        (start < end).then_some((start, end))
    }

    /// Whether the byte at `at` is `byte` in code, at bracket depth 0.
    fn is_top_byte(&self, at: usize, byte: u8) -> bool {
        self.kind_at(at) == Kind::Code && self.depth_at(at) <= 0 && self.bytes[at] == byte
    }

    /// Whether the `=` at `at` is a plain assignment's: at depth 0 in code, and not a plain `=`
    /// inside `==`, `!=`, `<=`, `>=`, `:=` or an augmented assignment such as `+=`.
    fn is_plain_equals(&self, at: usize) -> bool {
        self.is_top_byte(at, b'=') && self.is_single_equals(at)
    }

    /// Whether the `=` at `at` is a single one: not the first byte of `==`, and not the last byte of
    /// `!=`, `<=`, `>=`, `:=` or an augmented assignment such as `+=`.
    fn is_single_equals(&self, at: usize) -> bool {
        let before = at.checked_sub(1).and_then(|prev| self.bytes.get(prev));
        let doubled = self.bytes.get(at + 1) == Some(&b'=');
        let compared = matches!(
            before,
            Some(
                b'=' | b'!'
                    | b'<'
                    | b'>'
                    | b':'
                    | b'+'
                    | b'-'
                    | b'*'
                    | b'/'
                    | b'%'
                    | b'&'
                    | b'|'
                    | b'^'
                    | b'@'
            )
        );
        !doubled && !compared
    }
}

/// Whether `text` begins with the word `word`, with no identifier byte after it.
fn starts_with_word(text: &[u8], word: &[u8]) -> bool {
    text.starts_with(word) && !text.get(word.len()).is_some_and(|&byte| is_word(byte))
}

/// Whether `text` is a compound statement keyword that takes no expression before its colon.
fn is_compound_keyword(text: &[u8]) -> bool {
    matches!(text, b"else" | b"try" | b"finally")
}

/// The index of the line break that ends the comment that starts at `at`, or the end of the text.
fn line_end(bytes: &[u8], at: usize) -> usize {
    bytes
        .get(at..)
        .unwrap_or_default()
        .iter()
        .position(|&b| b == b'\n')
        .map_or(bytes.len(), |offset| at + offset)
}

/// The index just past the string literal that opens at `start`, quotes included. A triple-quoted
/// literal ends at its closing triple quote. Any other literal ends at its closing quote, or at the
/// line break that leaves it unclosed. A backslash skips the byte after it.
fn string_end(bytes: &[u8], start: usize) -> usize {
    let quote = bytes.get(start).copied().unwrap_or(b'"');
    let triple_quote = [quote; 3];
    let triple = bytes.get(start..start + 3) == Some(&triple_quote[..]);
    let mut at = start + if triple { 3 } else { 1 };
    while let Some(&byte) = bytes.get(at) {
        if byte == b'\\' {
            at += 2;
            continue;
        }
        if triple {
            if bytes.get(at..at + 3) == Some(&triple_quote[..]) {
                return at + 3;
            }
        } else if byte == quote {
            return at + 1;
        } else if byte == b'\n' {
            return at;
        }
        at += 1;
    }
    bytes.len()
}

/// The first index at or after `at` that is not a space or tab.
fn skip_blanks(bytes: &[u8], mut at: usize) -> usize {
    while matches!(bytes.get(at), Some(b' ' | b'\t')) {
        at += 1;
    }
    at
}

/// Whether only spaces and tabs sit between the start of `at`'s line and `at`.
fn only_blanks_before(bytes: &[u8], at: usize) -> bool {
    bytes
        .get(..at)
        .unwrap_or_default()
        .iter()
        .rev()
        .take_while(|&&b| b != b'\n')
        .all(|&b| b == b' ' || b == b'\t')
}

fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use super::{decorators, relative, tokens};

    /// The identifiers that `tokens` marks as assignment targets in `text`, in order.
    fn targets(text: &str) -> Vec<&str> {
        tokens(text)
            .into_iter()
            .filter(|token| token.assigned)
            .map(|token| token.ident)
            .collect()
    }

    #[test]
    fn tuple_target_list_marks_every_name_before_a_single_equals() {
        assert_eq!(
            targets("self._pa_cache, self._pb_cache = {}, {}\n"),
            ["_pa_cache", "_pb_cache"]
        );
    }

    #[test]
    fn keyword_argument_and_call_arguments_are_not_targets() {
        assert_eq!(
            targets("offload(self._args_cache, retries=2)\n"),
            Vec::<&str>::new()
        );
    }

    #[test]
    fn annotation_spanning_lines_is_read_through_to_its_equals() {
        assert_eq!(
            targets("self._ml_cache: dict[\n    str, int] = {}\n"),
            ["_ml_cache"]
        );
    }

    #[test]
    fn annotation_holding_a_string_is_read_through_to_its_equals() {
        assert_eq!(
            targets("self._s_cache: \"dict[str, int]\" = {}\n"),
            ["_s_cache"]
        );
    }

    #[test]
    fn dict_keys_comments_and_f_string_debug_forms_are_not_targets() {
        let text = "lookup = {\n    self._k2_cache: make(size=3),\n}\n\
                    label = f\"{self._dbg2_cache=}\"\n\
                    # self._c_cache = {}\n\
                    self._a_cache: int  # note = 1\n";
        assert_eq!(targets(text), ["lookup", "label"]);
    }

    #[test]
    fn comparisons_are_not_targets_and_chained_assignments_are() {
        assert_eq!(
            targets("self._a_cache = self._b_cache = {}\n"),
            ["_a_cache", "_b_cache"]
        );
        assert_eq!(
            targets("return self._a_cache, self._b_cache == 1\n"),
            Vec::<&str>::new()
        );
        assert_eq!(targets("if self._c_cache == x:\n"), Vec::<&str>::new());
    }

    #[test]
    fn a_string_ends_a_tuple_target_list_without_an_equals() {
        assert_eq!(
            targets("assert self._a_cache, \"m=1\"\n"),
            Vec::<&str>::new()
        );
    }

    #[test]
    fn an_escaped_quote_does_not_end_a_string_and_a_comment_bracket_does_not_count() {
        assert_eq!(targets("label = 'don\\'t self._q_cache = 1'\n"), ["label"]);
        assert_eq!(
            targets("x = 1  # see (the docs\nself._a_cache = {}\n"),
            ["x", "_a_cache"]
        );
    }

    #[test]
    fn decorators_name_each_decorator_line_outside_strings_and_brackets() {
        let text = "@cachetools.func.ttl_cache(maxsize=4)\ndef a():\n    pass\n\n\
                    class B:\n    @ cache\n    def b(self):\n        s = '''\n@not_one\n'''\n\
                    \x20       return (\n@not_two)\n";
        assert_eq!(decorators(text), ["cachetools.func.ttl_cache", "cache"]);
    }

    #[test]
    fn bracketed_target_lists_mark_each_name_in_them() {
        assert_eq!(
            targets("(self._a_cache, self._b_cache) = {}, {}\n"),
            ["_a_cache", "_b_cache"]
        );
        assert_eq!(
            targets("[self._a_cache, self._b_cache] = {}, {}\n"),
            ["_a_cache", "_b_cache"]
        );
    }

    #[test]
    fn subscripts_and_call_argument_tuples_are_not_target_names() {
        assert_eq!(targets("x[self._a_cache] = 1\n"), Vec::<&str>::new());
        assert_eq!(
            targets("rows[self._a_cache], self._b_cache = 1, 2\n"),
            ["_b_cache"]
        );
        assert_eq!(
            targets("offload((self._a_cache, self._b_cache), key)\n"),
            Vec::<&str>::new()
        );
    }

    #[test]
    fn lambda_parameters_are_not_target_names() {
        assert_eq!(
            targets("return self._a_cache, lambda x=1: x\n"),
            Vec::<&str>::new()
        );
        assert_eq!(targets("g = lambda x_cache, y=1: 0\n"), ["g"]);
        assert_eq!(targets("g = lambda y=1, x_cache=2: 0\n"), ["g"]);
    }

    #[test]
    fn bare_names_are_targets_when_they_begin_a_statement() {
        assert_eq!(targets("x_cache: dict[str, int] = {}\n"), ["x_cache"]);
        assert_eq!(targets("x_cache: dict[str, int]\n"), Vec::<&str>::new());
        assert_eq!(
            targets("a_cache, b_cache, c_cache = 1, 2, 3\n"),
            ["a_cache", "b_cache", "c_cache"]
        );
        assert_eq!(targets("a = b_cache = {}\n"), ["a", "b_cache"]);
        assert_eq!(targets("y: x_cache = 1\n"), ["y"]);
        assert_eq!(targets("x = 1; y_cache = 2\n"), ["x", "y_cache"]);
        assert_eq!(targets("if y: z_cache = 1\n"), ["z_cache"]);
        assert_eq!(targets("else: w_cache = 1\n"), ["w_cache"]);
    }

    #[test]
    fn relative_joins_the_components_below_the_root_with_slashes() {
        let path = Path::new("/root/src/gobby/a.py");
        assert_eq!(
            relative(Path::new("/root"), path),
            Some("src/gobby/a.py".to_owned())
        );
    }

    #[test]
    #[cfg(unix)]
    fn relative_is_none_when_a_component_is_not_utf8() {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;

        let path = PathBuf::from(OsStr::from_bytes(b"/root/src/odd\xff.txt"));
        assert_eq!(relative(Path::new("/root"), &path), None);
    }
}
