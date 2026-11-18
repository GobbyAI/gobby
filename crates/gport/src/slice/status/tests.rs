use super::*;

#[test]
fn blocked_requires_a_non_empty_reason() {
    assert_eq!(State::parse("blocked(waiting)"), Some(State::Blocked));
    assert_eq!(State::parse("blocked()"), None);
    assert_eq!(State::parse("blocked(  )"), None);
    assert_eq!(State::parse("blocked(x"), None);
    assert_eq!(State::parse("finished"), None);
}

#[test]
fn evidence_needs_the_four_keywords_and_a_command() {
    assert_eq!(
        evidence_shas("red aaaaaaa green bbbbbbb test cargo test x"),
        Some(("aaaaaaa", "bbbbbbb"))
    );
    assert_eq!(evidence_shas("red aaaaaaa test cargo test"), None);
    assert_eq!(evidence_shas("red aaaaaaa green bbbbbbb test"), None);
    assert_eq!(evidence_shas("green bbbbbbb red aaaaaaa test x"), None);
}
