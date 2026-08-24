use sha2::{Digest, Sha256};

pub const RUNNER_PROTOCOL_VERSION: u32 = 1;
pub const BASELINE_VERSION: i32 = 375;
pub const BASELINE_CHECKSUM: &str =
    "ec222a7f8b3c486abfff05eda4ed02995d272a132ad2fdadb1dd90edbccb2ce1";
pub const BASELINE_SQL: &str = include_str!("../../assets/schema/baseline.sql");
pub const SEED_MANIFEST_JSON: &str = include_str!("../../assets/schema/seed.manifest.json");
pub const CATALOG_MANIFEST_JSON: &str = include_str!("../../assets/schema/catalog.manifest.json");
pub(crate) const TOOL_CHAT_OVERLAY_PREDECESSOR_CHECKSUM: &str =
    "d19810005e6c931219781941ab1c63ecc057973dfe60e2d4a8b6a69f460c6dd0";
pub(crate) const WORKTREE_PRE_OVERLAY_BASELINE_CHECKSUM: &str =
    "7477af06f3e54121b97f6af26e68efab79712d187bef7f1773a80e023a4faee6";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EmbeddedMigration {
    pub version: i32,
    pub filename: &'static str,
    pub checksum: &'static str,
    pub sql: &'static str,
}

pub(crate) const MIGRATIONS: &[EmbeddedMigration] = &[
    EmbeddedMigration {
        version: 376,
        filename: "376_copy_agent_definitions.sql",
        checksum: "d736ad1aa1f182a7569fcb7aae129cdf2cdaf89b46e898212a3269251295d4f1",
        sql: include_str!("../../assets/schema/migrations/376_copy_agent_definitions.sql"),
    },
    EmbeddedMigration {
        version: 377,
        filename: "377_copy_agent_step_instances.sql",
        checksum: "bdcd4738d51cf4a099825fa43dcd1509ce6e6be5330abded423b679d1fc3cc14",
        sql: include_str!("../../assets/schema/migrations/377_copy_agent_step_instances.sql"),
    },
    EmbeddedMigration {
        version: 378,
        filename: "378_copy_rule_definitions.sql",
        checksum: "5fe2bcea79afa5876fc12fb1087a764bbe3bb262c23f5abbc3b1b9b83b1ed4e7",
        sql: include_str!("../../assets/schema/migrations/378_copy_rule_definitions.sql"),
    },
    EmbeddedMigration {
        version: 379,
        filename: "379_copy_session_variable_defaults.sql",
        checksum: "ea1ffe7eec95b90901b2818fe734e4128433c8f7e4b05060fee5ac3acbda4896",
        sql: include_str!("../../assets/schema/migrations/379_copy_session_variable_defaults.sql"),
    },
    EmbeddedMigration {
        version: 380,
        filename: "380_copy_pipeline_definitions.sql",
        checksum: "6c4c0b827117c7667b2b1b5b540f56aca81c04304d274bd4d34983947ca9e86c",
        sql: include_str!("../../assets/schema/migrations/380_copy_pipeline_definitions.sql"),
    },
    EmbeddedMigration {
        version: 381,
        filename: "381_drop_legacy_workflow_tables.sql",
        checksum: "029f44aeeaf260d617e981ec77a558f21e2f8cb1af2e49e1d14adbc3458cc2e8",
        sql: include_str!("../../assets/schema/migrations/381_drop_legacy_workflow_tables.sql"),
    },
    EmbeddedMigration {
        version: 382,
        filename: "382_grant_gwiki_tables_to_capability.sql",
        checksum: "658527b69d99dfc0c2de99e0d3c9c47d6b5f1172e784fd7980f5c9f76d7cec4e",
        sql: include_str!(
            "../../assets/schema/migrations/382_grant_gwiki_tables_to_capability.sql"
        ),
    },
    EmbeddedMigration {
        version: 383,
        filename: "383_refresh_reused_interactive_principal.sql",
        checksum: "6a9b479e68847bc240e54ace9d4a4c80e1acf79b1d7568dc8e3eb4fe84477696",
        sql: include_str!(
            "../../assets/schema/migrations/383_refresh_reused_interactive_principal.sql"
        ),
    },
    EmbeddedMigration {
        version: 384,
        filename: "384_grant_projects_liveness_to_capability.sql",
        checksum: "0f7a499e1b7216a7a2426dc5c04064eae97440a1ee1a4d5134a0dd7a8cf6ebef",
        sql: include_str!(
            "../../assets/schema/migrations/384_grant_projects_liveness_to_capability.sql"
        ),
    },
    EmbeddedMigration {
        version: 385,
        filename: "385_issue_maintenance_principal.sql",
        checksum: "2d666ea0917211806be00cde6a854b45c181ea6344b312f729cd8e37a9aa72f6",
        sql: include_str!("../../assets/schema/migrations/385_issue_maintenance_principal.sql"),
    },
    EmbeddedMigration {
        version: 386,
        filename: "386_interactive_principal_role_hash.sql",
        checksum: "8570f08f0f90b1c17bf14f45a4d4cfd4b7b85a56a5aa5ce430442fb03f160799",
        sql: include_str!("../../assets/schema/migrations/386_interactive_principal_role_hash.sql"),
    },
    EmbeddedMigration {
        version: 387,
        filename: "387_interactive_principal_role_helper.sql",
        checksum: "89fbe7978fbd565510b2698e06f15adddad3eec273ee2665e5d17144ef1ebd69",
        sql: include_str!(
            "../../assets/schema/migrations/387_interactive_principal_role_helper.sql"
        ),
    },
    EmbeddedMigration {
        version: 388,
        filename: "388_grant_interactive_role_name.sql",
        checksum: "bb45c887073a2a2a7b77077ff5cc96b30c9476030883111a083e54f1dbc72502",
        sql: include_str!("../../assets/schema/migrations/388_grant_interactive_role_name.sql"),
    },
    EmbeddedMigration {
        version: 389,
        filename: "389_sweep_interactive_orphan_roles.sql",
        checksum: "a86978cd9afd129b3d8cfb4f38d3d9efc096309543c3e08aacc8ea773575283f",
        sql: include_str!("../../assets/schema/migrations/389_sweep_interactive_orphan_roles.sql"),
    },
    EmbeddedMigration {
        version: 390,
        filename: "390_retain_interactive_credential_material.sql",
        checksum: "e2cc73155a4c4d13ae0bb18cf93d95bd745da2cf0ce8dcb423cd40608a61b9a5",
        sql: include_str!(
            "../../assets/schema/migrations/390_retain_interactive_credential_material.sql"
        ),
    },
    EmbeddedMigration {
        version: 391,
        filename: "391_session_last_activity_and_creation_defaults.sql",
        checksum: "775a2f0622a1866984d01a85158c2d85235fd13698728ca23f41d797ad3ae675",
        sql: include_str!(
            "../../assets/schema/migrations/391_session_last_activity_and_creation_defaults.sql"
        ),
    },
    EmbeddedMigration {
        version: 392,
        filename: "392_chat_attachments_deletion_lease.sql",
        checksum: "4d3f2e9652a958f4ad7f0f64410e622e4e4629b8388b1610527170e0bac83598",
        sql: include_str!("../../assets/schema/migrations/392_chat_attachments_deletion_lease.sql"),
    },
    EmbeddedMigration {
        version: 393,
        filename: "393_interactive_principal_hardening.sql",
        checksum: "199d27292b0c3e48c0beb0e5aad2a8748a2a78f82e31f0647b8c0973564633da",
        sql: include_str!("../../assets/schema/migrations/393_interactive_principal_hardening.sql"),
    },
    EmbeddedMigration {
        version: 394,
        filename: "394_sessions_status_last_activity_index.sql",
        checksum: "449a7e2e482c086b063fb066e93019c043703ce970ed1a0c7c30f46e7050d097",
        sql: include_str!(
            "../../assets/schema/migrations/394_sessions_status_last_activity_index.sql"
        ),
    },
    EmbeddedMigration {
        version: 395,
        filename: "395_code_inheritance.sql",
        checksum: "717946c2093e4db2bffa21e6427964fc88ee2e6d90893af7fc2737248205feb0",
        sql: include_str!("../../assets/schema/migrations/395_code_inheritance.sql"),
    },
    EmbeddedMigration {
        version: 396,
        filename: "396_memory_rationale_and_provenance.sql",
        checksum: "67f01619b6ae8f637c868ff9b8e86c8a2f6e7168299bd506551e2b366266c79f",
        sql: include_str!("../../assets/schema/migrations/396_memory_rationale_and_provenance.sql"),
    },
    EmbeddedMigration {
        version: 397,
        filename: "397_memories_source_task_index.sql",
        checksum: "72104d71d3c69a8c277e206cf1d8932570c532899bed45dc006ecec02e4cdddd",
        sql: include_str!("../../assets/schema/migrations/397_memories_source_task_index.sql"),
    },
    EmbeddedMigration {
        version: 398,
        filename: "398_code_indexed_project_states_indexer_version.sql",
        checksum: "1dbe00b375b252d2ab283e1a0c1ef66f5bcd79b1f2a9618a4452f7a1ab153da2",
        sql: include_str!(
            "../../assets/schema/migrations/398_code_indexed_project_states_indexer_version.sql"
        ),
    },
    EmbeddedMigration {
        version: 399,
        filename: "399_drain_orphan_binding_alias.sql",
        checksum: "0043823579b2457d3a7df5d0697dfbe080aaff3aed4fe735166ec14e28acaefe",
        sql: include_str!("../../assets/schema/migrations/399_drain_orphan_binding_alias.sql"),
    },
    EmbeddedMigration {
        version: 400,
        filename: "400_drop_vision_extract_config_rows.sql",
        checksum: "07fac33f96b083fc98efa7f6c41d903b7c65f5362d214851b0bd712a4c36a387",
        sql: include_str!("../../assets/schema/migrations/400_drop_vision_extract_config_rows.sql"),
    },
    EmbeddedMigration {
        version: 401,
        filename: "401_model_metadata_reasoning.sql",
        checksum: "909c433d9398fdadb734c619e81bbb22f17d12329f9f7d060a0dd821142c1a35",
        sql: include_str!("../../assets/schema/migrations/401_model_metadata_reasoning.sql"),
    },
    EmbeddedMigration {
        version: 402,
        filename: "402_task_close_reviews.sql",
        checksum: "10593cb9a01e8cf411e55993bc6cd679a211a3685c2a74f8d89c5f23db02b82f",
        sql: include_str!("../../assets/schema/migrations/402_task_close_reviews.sql"),
    },
    EmbeddedMigration {
        version: 403,
        filename: "403_interactive_overlay_principal.sql",
        checksum: "5970bfeb313dbd58545c3d4ebe824f2de8d6e140ecec4fc6e02f0078856f6231",
        sql: include_str!("../../assets/schema/migrations/403_interactive_overlay_principal.sql"),
    },
    EmbeddedMigration {
        version: 404,
        filename: "404_scope_rotation_sweep_to_managed_principals.sql",
        checksum: "76976c81b1bf18bab99fc158aa9bdc1ad043dea2b53e7d024450b0f6c4e543a3",
        sql: include_str!(
            "../../assets/schema/migrations/404_scope_rotation_sweep_to_managed_principals.sql"
        ),
    },
];
const _: &str = include_str!("../../assets/schema/migrations/.gitkeep");

/// Receipts written before in-place asset edits. Live hubs keep those
/// checksums; the improved bodies are what new applies stamp.
pub(crate) const PRIOR_RECEIPT_CHECKSUMS: &[(i32, &str)] = &[
    (
        375,
        "ece3754752dbc72aaff4bbd3ebaa91a41305e4899e180012f8429c4f7467b1bf",
    ),
    (
        375,
        "8467fc42e29fec1f58986e7ac141c3cdcf8c6a417c61c73ff3cca63241e2a2cf",
    ),
    (
        377,
        "43b6c25263c1e510f28c540d9cc24e62ffcad67a3a8dddde3645bdb6a5e92821",
    ),
    (
        387,
        "0831490e4169d0e778575761a3463d19bb2ac68b1f73940c00f716447adfed0b",
    ),
    (
        389,
        "00359659fbe933506babe8f03b643d1558be6ac7dff4c3d5f3d2e62627d534f5",
    ),
    (
        390,
        "25ff1d544e9ee87e54c4a4ca1330660b29b1c1dd7464eda9aa9985460ec93ad3",
    ),
    (
        391,
        "40fafbe193afe1a097e71f80d236c466cb759533160eb56fe9afd6b8c74321cf",
    ),
    (
        395,
        "a8e488ff1515e14c0544f0f8efbd5f9ce0869a0861ee3d7a7eb6613f41972ab9",
    ),
    (
        396,
        "0696d021aee10934261018742e7d632511591de8c59f20f3203623cbe366cb7d",
    ),
];

pub(crate) fn root_hash() -> String {
    let mut digest = Sha256::new();
    hash_part(&mut digest, "baseline@375", BASELINE_CHECKSUM);
    for migration in MIGRATIONS {
        hash_part(&mut digest, migration.filename, migration.checksum);
    }
    hash_bytes(
        &mut digest,
        "seed.manifest.json",
        SEED_MANIFEST_JSON.as_bytes(),
    );
    hash_bytes(
        &mut digest,
        "catalog.manifest.json",
        CATALOG_MANIFEST_JSON.as_bytes(),
    );
    hex_digest(digest.finalize().as_slice())
}

pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    hex_digest(Sha256::digest(bytes).as_slice())
}

fn hash_part(digest: &mut Sha256, name: &str, checksum: &str) {
    digest.update(name.as_bytes());
    digest.update([0]);
    digest.update(checksum.as_bytes());
    digest.update([0]);
}

fn hash_bytes(digest: &mut Sha256, name: &str, bytes: &[u8]) {
    hash_part(digest, name, &sha256_hex(bytes));
}

fn hex_digest(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    output
}
