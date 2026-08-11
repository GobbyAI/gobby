use crate::config::is_machine_config_key;

#[test]
fn rejects_unregistered_machine_key() {
    assert!(is_machine_config_key("ai.embeddings.model"));
    assert!(!is_machine_config_key("unknown.machine.key"));
}

#[test]
fn secret_bearing_keys_are_not_machine_exportable() {
    assert!(!is_machine_config_key("ai.embeddings.api_key"));
    assert!(!is_machine_config_key("databases.falkordb.password"));
    assert!(!is_machine_config_key("databases.qdrant.api_key"));
}

#[test]
fn secret_reference_keys_follow_the_contract() {
    use crate::config::is_secret_reference_key;

    // Exact keys with reference secrecy.
    assert!(is_secret_reference_key("ai.embeddings.api_key"));
    assert!(is_secret_reference_key("databases.falkordb.password"));
    assert!(is_secret_reference_key("databases.qdrant.api_key"));

    // Pattern keys: only the {field} segments with reference secrecy qualify.
    assert!(is_secret_reference_key(
        "ai.generation.endpoints.mine.api_key"
    ));
    assert!(!is_secret_reference_key(
        "ai.generation.endpoints.mine.api_base"
    ));

    // Non-secret and unregistered keys never qualify.
    assert!(!is_secret_reference_key("ai.embeddings.model"));
    assert!(!is_secret_reference_key("databases.falkordb.host"));
    assert!(!is_secret_reference_key("unknown.machine.key"));
}
