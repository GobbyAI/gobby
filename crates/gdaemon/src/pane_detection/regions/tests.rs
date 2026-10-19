use super::casefold;

/// Python's `str.casefold` values: full folding expands sharp s, folds final sigma to sigma,
/// and folds Cherokee lowercase to uppercase. `str::to_lowercase` gets all three wrong.
#[test]
fn casefold_matches_python_full_folding() {
    // "Straße" -> "strasse"
    assert_eq!(casefold("Stra\u{df}e"), "strasse");
    // "ΟΔΟΣ" -> "οδοσ": final sigma folds to sigma, where to_lowercase writes final sigma.
    assert_eq!(
        casefold("\u{39f}\u{394}\u{39f}\u{3a3}"),
        "\u{3bf}\u{3b4}\u{3bf}\u{3c3}"
    );
    // Cherokee small letter A folds to the capital letter.
    assert_eq!(casefold("\u{ab70}"), "\u{13a0}");
    // Capital sharp s folds to "ss".
    assert_eq!(casefold("\u{1e9e}"), "ss");
}
