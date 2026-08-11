use crate::config::is_machine_config_key;

#[test]
fn rejects_unregistered_machine_key() {
    assert!(is_machine_config_key("ai.embeddings.model"));
    assert!(!is_machine_config_key("unknown.machine.key"));
}
