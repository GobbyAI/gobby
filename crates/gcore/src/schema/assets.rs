use sha2::{Digest, Sha256};

pub const RUNNER_PROTOCOL_VERSION: u32 = 1;
pub const BASELINE_VERSION: i32 = 375;
pub const BASELINE_CHECKSUM: &str =
    "ece3754752dbc72aaff4bbd3ebaa91a41305e4899e180012f8429c4f7467b1bf";
pub const BASELINE_SQL: &str = include_str!("../../assets/schema/baseline.sql");
pub const SEED_MANIFEST_JSON: &str = include_str!("../../assets/schema/seed.manifest.json");
pub const CATALOG_MANIFEST_JSON: &str = include_str!("../../assets/schema/catalog.manifest.json");

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
        checksum: "0831490e4169d0e778575761a3463d19bb2ac68b1f73940c00f716447adfed0b",
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
        checksum: "00359659fbe933506babe8f03b643d1558be6ac7dff4c3d5f3d2e62627d534f5",
        sql: include_str!("../../assets/schema/migrations/389_sweep_interactive_orphan_roles.sql"),
    },
    EmbeddedMigration {
        version: 390,
        filename: "390_chat_attachments_deletion_lease.sql",
        checksum: "4d3f2e9652a958f4ad7f0f64410e622e4e4629b8388b1610527170e0bac83598",
        sql: include_str!("../../assets/schema/migrations/390_chat_attachments_deletion_lease.sql"),
    },
];
const _: &str = include_str!("../../assets/schema/migrations/.gitkeep");

/// Receipts written before #20368 edited 377 in place. Live hubs keep those
/// checksums; the improved 377 body is what new applies stamp.
pub(crate) const PRIOR_RECEIPT_CHECKSUMS: &[(i32, &str)] = &[(
    377,
    "43b6c25263c1e510f28c540d9cc24e62ffcad67a3a8dddde3645bdb6a5e92821",
)];

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
