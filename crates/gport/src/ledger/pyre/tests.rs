//! Selector translation tests: the dash-range and comment-group refusals, and the Python meaning
//! of the dash forms Python accepts.

use super::translate;
use crate::rows::Selectors;

/// A class escape followed by a range dash is a bad character range in Python, whatever ends the
/// range: a literal, an escape, a class, or a `\b`.
#[test]
fn dash_after_a_class_escape_is_refused_as_a_bad_range() {
    for form in [
        r"[\s-a]",
        r"[\s-z]",
        r"[\s-\x41]",
        r"[\s-\]]",
        r"[\s-\s]",
        r"[\s-\d]",
        r"[\s-\n]",
        r"[\s- ]",
        r"[\s-é]",
        r"[\s-A]",
        r"[\s-\x20]",
        r"[\s-\-]",
        r"[\s-.]",
        r"[\s-\\]",
        r"[\s-\.]",
        r"[\s-\b]",
        r"[^\s-a]",
        r"[a\s-c]",
        r"[a-\s]",
        r"[\d-\w]",
    ] {
        assert!(
            translate(form).is_err(),
            "{form} is accepted; Python refuses it"
        );
    }
}

/// Each form is one Python accepts, with the dash read as Python reads it: a trailing dash, a dash
/// before a class, an escaped dash, or a range that starts at `]`. The expected set is the ASCII
/// keys that `re.search` matches, read under `python3.13 -I`; gport must match exactly those keys.
#[test]
fn dash_forms_python_accepts_keep_python_meaning() {
    for (form, expected) in [
        (r"[\s-]", "\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f -"),
        (r"[-\s]", "\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f -"),
        (r"[a-c-\s]", "\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f -abc"),
        (r"[a-z-0]", "-0abcdefghijklmnopqrstuvwxyz"),
        (r"[a-z-]", "-abcdefghijklmnopqrstuvwxyz"),
        (r"[a-]", "-a"),
        (r"[+-]", "+-"),
        (r"[\--]", "-"),
        (r"[a\-z]", "-az"),
        (r"[\s\-]", "\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f -"),
        (r"[a-c\s]", "\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f abc"),
        (r"[]-a]", "]^_`a"),
        (r"[\]-\]]", "]"),
        (r"[\t-\r]", "\x09\x0a\x0b\x0c\x0d"),
        (r"[\101-\x5a]", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        (r"[\x41-\x5a]", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    ] {
        let selectors = Selectors::from_selectors([("c", form)]);
        assert!(
            selectors.bad.is_empty(),
            "{form} is refused: {:?}",
            selectors.bad
        );
        let matched: String = (0u8..128)
            .map(char::from)
            .filter(|key| !selectors.try_owners(&key.to_string()).unwrap().is_empty())
            .collect();
        assert_eq!(matched, expected, "{form}");
    }
}

/// A comment group is refused in every position. Python drops it, so `a*(?#c)?` is a multiple
/// repeat there, and the real map uses no comment group.
#[test]
fn comment_groups_are_refused() {
    for form in ["a*(?#c)?", "a{2}(?#c)?", "(?#c)a", "a(?#c)*", "a(?#c)b"] {
        assert!(translate(form).is_err(), "{form} is accepted");
    }
}

/// Only an `i` in the on list turns ignore-case on. In `(?-i:...)`, `(?s-i:...)`, `(?-im:...)` and
/// `(?u-i:...)` the `i` is turned off, so the group stays case-sensitive and the selector is kept.
/// Verdicts from `python3.13 -I`: `(?-i:é)` matches `é` and not `É`. The on list still sets the
/// flag: `(?i:a)` and `(?i-m:A)`.
#[test]
fn ignore_case_turned_off_is_accepted_and_does_not_set_the_flag() {
    for form in [r"(?-i:é)", r"(?s-i:é)", r"(?-im:é)", r"(?u-i:é)"] {
        let translated =
            translate(form).unwrap_or_else(|error| panic!("{form} is refused: {error}"));
        assert!(!translated.ascii_only, "{form} sets the flag");
    }
    for form in [r"(?i:a)", r"(?i-m:A)"] {
        let translated =
            translate(form).unwrap_or_else(|error| panic!("{form} is refused: {error}"));
        assert!(translated.ascii_only, "{form} does not set the flag");
    }
}

/// A word escape, `\w`, `\W` or `\b` outside a set or `\w` and `\W` inside one, sets the ASCII-only
/// flag, and a selector with one is accepted with a non-ASCII character in it. Inside a set `\b` is
/// backspace, so `[\b]` sets nothing. Verdicts from `python3.13 -I`: `[\b]` matches U+0008 and not
/// `b`.
#[test]
fn word_escapes_set_the_ascii_only_flag_without_refusing_non_ascii_selectors() {
    for form in [
        r"\w", r"\W", r"\b", r"[\w]", r"[\W]", r"[^\w]", r"é\w", r"é\b",
    ] {
        let translated =
            translate(form).unwrap_or_else(|error| panic!("{form} is refused: {error}"));
        assert!(translated.ascii_only, "{form} does not set the flag");
    }
    for form in [r"[\b]", r"é", r"\d", r"\s"] {
        let translated =
            translate(form).unwrap_or_else(|error| panic!("{form} is refused: {error}"));
        assert!(!translated.ascii_only, "{form} sets the flag");
    }
}

/// `\B` is refused in every position it can stand outside a set. Python finds no `\B` in the empty
/// key, and fancy-regex matches one there, so a selector with `\B` would own a row Python does not.
/// Verdicts from `python3.13 -I`: `\B` and `^\B$` find nothing in `""`, and `a\B` finds nothing in
/// `a`. The real map uses no `\B`.
#[test]
fn non_word_boundary_is_refused() {
    for form in [r"\B", r"a\B", r"^\B$", r"(?:\B)", r"a|\B"] {
        assert!(translate(form).is_err(), "{form} is accepted");
    }
}
