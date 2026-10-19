use super::RunDetectState;

const LOOP_PANE: &str = "Potential loop detected\nContinue? (y/n)\n";
const LOOP_PANE_RETYPED: &str = "Potential loop detected\nType y to continue\n";

#[test]
fn loop_prompt_dismissal_is_per_pane_and_per_run() {
    let mut state = RunDetectState::new();
    assert!(!state.was_loop_prompt_dismissed(LOOP_PANE));
    state.mark_loop_prompt_dismissed(LOOP_PANE);
    assert!(state.was_loop_prompt_dismissed(LOOP_PANE));
    assert!(!state.was_loop_prompt_dismissed(LOOP_PANE_RETYPED));
    assert!(!RunDetectState::new().was_loop_prompt_dismissed(LOOP_PANE));
}

#[test]
fn trust_dismissal_reads_back_for_its_run() {
    let mut state = RunDetectState::new();
    assert!(!state.was_trust_dismissed());
    state.mark_trust_dismissed();
    assert!(state.was_trust_dismissed());
    assert!(!RunDetectState::new().was_trust_dismissed());
}

#[test]
fn approval_dismissal_reads_back_for_its_pane() {
    let mut state = RunDetectState::new();
    state.mark_approval_prompt_dismissed(LOOP_PANE);
    assert!(state.was_approval_prompt_dismissed(LOOP_PANE));
    assert!(!state.was_approval_prompt_dismissed(LOOP_PANE_RETYPED));
    assert!(!RunDetectState::new().was_approval_prompt_dismissed(LOOP_PANE));
}
