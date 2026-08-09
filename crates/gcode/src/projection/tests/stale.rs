#[test]
fn projection_fixture_namespace_matches_index_ids() {
    assert_eq!(
        crate::models::CODE_INDEX_UUID_NAMESPACE.to_string(),
        "c0de1de0-0000-4000-8000-000000000000"
    );
}
