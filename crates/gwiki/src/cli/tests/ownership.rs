use gobby_wiki::{Command, ScopeSelection};

use crate::cli::mapping::command_is_mutating;

#[test]
fn mutating_commands_are_classified_after_parse() {
    let init = Command::Init {
        scope: ScopeSelection::topic("rust"),
    };
    let search = Command::Search {
        query: "q".into(),
        scope: ScopeSelection::topic("rust"),
        limit: 5,
        include_semantic: true,
        token_budget: None,
        include_candidates: false,
    };
    assert!(command_is_mutating(&init));
    assert!(!command_is_mutating(&search));
    assert!(command_is_mutating(&Command::Health {
        scope: ScopeSelection::topic("rust"),
    }));
    assert!(!command_is_mutating(&Command::Status {
        scope: ScopeSelection::topic("rust"),
    }));
}
