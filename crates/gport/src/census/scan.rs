//! Source walking and the identifier scan shared by the census sections.

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
    /// True when the identifier is an assignment target: the name of a plain assignment,
    /// `name = value` (also a chained one, `name = other = value`), of an annotated one,
    /// `name: T = value`, whose dotted name starts its line, or one name of a tuple target list,
    /// `name, other = value`. A call, a method reference, a read, a comparison, a keyword argument
    /// or an augmented assignment is not.
    pub assigned: bool,
}

/// Every identifier in `text`, in order. A run of word characters that starts with a digit is not
/// an identifier.
pub fn tokens(text: &str) -> Vec<Token<'_>> {
    let source = Source::new(text.as_bytes());
    let bytes = source.bytes;
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
        let assigned = source.is_target(start, at);
        found.push(Token {
            ident,
            qualifier,
            assigned,
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

    /// Whether the identifier at `start..end` is an assignment target. It must be code outside
    /// every bracket, so a keyword argument, `f(name=value)`, and a dict key, `{name: value}`, are
    /// not one. A plain target, `name = value`, must not be followed by `=` of `==`, or by `}` as in
    /// the debug form of an f-string. An annotated target, `name: T = value`, must begin its line,
    /// so `if key in self._cache:` and `batch[self._cache:]` are not one.
    fn is_target(&self, start: usize, end: usize) -> bool {
        if self.kind_at(start) != Kind::Code {
            return false;
        }
        let base = self.depth_at(start);
        if base > 0 {
            return false;
        }
        let after = skip_blanks(self.bytes, end);
        match self.bytes.get(after) {
            Some(b'=') => !matches!(self.bytes.get(after + 1), Some(b'=' | b'}')),
            Some(b',') => self.assigns_after(after + 1, base, true),
            Some(b':') => {
                begins_line(self.bytes, start) && self.assigns_after(after + 1, base, false)
            }
            _ => false,
        }
    }

    /// Whether the statement that continues at `from` assigns to a target at bracket depth `base`.
    /// The first `=` at that depth decides it: a single one is an assignment. At that depth a
    /// comment, a line break, a `;` or a closing bracket ends the search without one. A tuple
    /// target list (`tuple`) also ends it at the first byte that cannot sit in a target list, and
    /// an annotation ends it at a `,` or a `:`. Strings and brackets are skipped over, so an
    /// annotation that spans lines is read through.
    fn assigns_after(&self, from: usize, base: i32, tuple: bool) -> bool {
        let mut at = from;
        while let Some(&byte) = self.bytes.get(at) {
            let top = self.depth_at(at) <= base;
            match self.kind_at(at) {
                Kind::Text if top && tuple => return false,
                Kind::Comment if top => return false,
                Kind::Code if top => {
                    if byte == b'\\' && self.bytes.get(at + 1) == Some(&b'\n') {
                        at += 2;
                        continue;
                    }
                    match byte {
                        b'=' => return self.is_single_equals(at),
                        b'\n' | b';' | b':' | b')' | b']' | b'}' => return false,
                        b',' if !tuple => return false,
                        _ if tuple && !is_target_byte(byte) => return false,
                        _ => {}
                    }
                }
                _ => {}
            }
            at += 1;
        }
        false
    }

    /// Whether the `=` at `at` is a plain one: not the first byte of `==`, and not the last byte of
    /// `!=`, `<=`, `>=` or `:=`.
    fn is_single_equals(&self, at: usize) -> bool {
        let before = at.checked_sub(1).and_then(|prev| self.bytes.get(prev));
        let doubled = self.bytes.get(at + 1) == Some(&b'=');
        let compared = matches!(before, Some(b'=' | b'!' | b'<' | b'>' | b':'));
        !doubled && !compared
    }
}

/// Whether a byte can sit in a tuple target list at the top level: a name, a dot, a comma, a star,
/// a blank or an opening bracket.
fn is_target_byte(byte: u8) -> bool {
    is_word(byte) || matches!(byte, b'.' | b',' | b'*' | b' ' | b'\t' | b'(' | b'[' | b'{')
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

/// Whether the dotted name, whose last identifier starts at `start`, begins its line.
fn begins_line(bytes: &[u8], start: usize) -> bool {
    let mut begin = start;
    while begin > 0
        && bytes
            .get(begin - 1)
            .is_some_and(|&b| is_word(b) || b == b'.')
    {
        begin -= 1;
    }
    only_blanks_before(bytes, begin)
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
