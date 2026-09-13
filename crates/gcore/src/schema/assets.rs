use sha2::{Digest, Sha256};

pub const RUNNER_PROTOCOL_VERSION: u32 = 1;
pub const BASELINE_VERSION: i32 = 420;
pub const BASELINE_CHECKSUM: &str =
    "f8e4cea2f63769a2fd2b32a93a56574c4fda3d335a745aa0970cfea6a2596b55";
pub const BASELINE_SQL: &str = include_str!("../../assets/schema/baseline.sql");
pub const SEED_MANIFEST_JSON: &str = include_str!("../../assets/schema/seed.manifest.json");
pub const CATALOG_MANIFEST_JSON: &str = include_str!("../../assets/schema/catalog.manifest.json");

/// Receipt filename of the embedded baseline (`baseline@<BASELINE_VERSION>`).
pub(crate) fn baseline_filename() -> String {
    format!("baseline@{BASELINE_VERSION}")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EmbeddedMigration {
    pub version: i32,
    pub filename: &'static str,
    pub checksum: &'static str,
    pub sql: &'static str,
}

pub(crate) const MIGRATIONS: &[EmbeddedMigration] = &[
    EmbeddedMigration {
        version: 421,
        filename: "421_drop_provider_model_routes.sql",
        checksum: "d3de405d14b6e2d1096f928b905b781c3d4813c764038eb7d4f2e926943f3d90",
        sql: include_str!("../../assets/schema/migrations/421_drop_provider_model_routes.sql"),
    },
    EmbeddedMigration {
        version: 422,
        filename: "422_restore_public_privilege_revokes.sql",
        checksum: "778e4101727b179ffa16a7a51eaa513ed6c52422b62eceb786c150900b2c19ef",
        sql: include_str!(
            "../../assets/schema/migrations/422_restore_public_privilege_revokes.sql"
        ),
    },
    EmbeddedMigration {
        version: 423,
        filename: "423_add_session_handoffs.sql",
        checksum: "fedc1a62b0a5cda9c104c99fc640ac7a8b36c27c65f5d10491541cef429f8cd5",
        sql: include_str!("../../assets/schema/migrations/423_add_session_handoffs.sql"),
    },
    EmbeddedMigration {
        version: 424,
        filename: "424_add_close_review_fingerprint_components.sql",
        checksum: "4ce4e7aeb50cb820173c42fb561c343ca9c80cfeb33ed228b1ba9504e8f2cd33",
        sql: include_str!(
            "../../assets/schema/migrations/424_add_close_review_fingerprint_components.sql"
        ),
    },
    EmbeddedMigration {
        version: 425,
        filename: "425_add_external_pending_close_review_status.sql",
        checksum: "d5bf7a2f1cbe660a2ff50751b79d302f16963e0452c7ae9b6a47718d657b7d1f",
        sql: include_str!(
            "../../assets/schema/migrations/425_add_external_pending_close_review_status.sql"
        ),
    },
    EmbeddedMigration {
        version: 426,
        filename: "426_retire_legacy_wiki.sql",
        checksum: "b390c1b8350fa4ec28fa40204db6ae8fbc25f4ad622595dd06e1b3ef64a5b7f4",
        sql: include_str!("../../assets/schema/migrations/426_retire_legacy_wiki.sql"),
    },
    EmbeddedMigration {
        version: 427,
        filename: "427_remove_session_summary_revisions.sql",
        checksum: "a766908d0dbfac143765e98530a8dd16b25b4060a2b9039d31c309269fb9b140",
        sql: include_str!(
            "../../assets/schema/migrations/427_remove_session_summary_revisions.sql"
        ),
    },
    EmbeddedMigration {
        version: 428,
        filename: "428_add_agent_end_handoff_boundary.sql",
        checksum: "808ebf7df06829502e1334a228341c7ac9a7ae6fa04ec2c0e251eb78967149c4",
        sql: include_str!("../../assets/schema/migrations/428_add_agent_end_handoff_boundary.sql"),
    },
    EmbeddedMigration {
        version: 429,
        filename: "429_add_transcript_processing_failures.sql",
        checksum: "39669edef7481e864653433ccb075dfb6ea21340168b3bf81c8465e314ea23d1",
        sql: include_str!(
            "../../assets/schema/migrations/429_add_transcript_processing_failures.sql"
        ),
    },
    EmbeddedMigration {
        version: 430,
        filename: "430_add_run_evidence_and_reports.sql",
        checksum: "7f8eeeeb60bc13f2a5be9b6fdca99eafcd927c03090ae8f147eba4bf78b8736e",
        sql: include_str!("../../assets/schema/migrations/430_add_run_evidence_and_reports.sql"),
    },
    EmbeddedMigration {
        version: 431,
        filename: "431_add_coordination_waits.sql",
        checksum: "0e3fd049adf275f5b385baadb568d1ea30bfcc45f912b41e56a76c2cbe12b5fe",
        sql: include_str!("../../assets/schema/migrations/431_add_coordination_waits.sql"),
    },
    EmbeddedMigration {
        version: 432,
        filename: "432_bind_tool_chat_overlay_to_session_workspace.sql",
        checksum: "107b5c6cdc766d847be29d76dc026bb0d6ac7674a426ffbc2fd15e5aa3202c31",
        sql: include_str!(
            "../../assets/schema/migrations/432_bind_tool_chat_overlay_to_session_workspace.sql"
        ),
    },
    EmbeddedMigration {
        version: 433,
        filename: "433_flatten_gcode_read_policy_disjunction.sql",
        checksum: "06c32298346c003ae4fba342577666418b662fa17f4c4c8e518a15fd0fcdae35",
        sql: include_str!(
            "../../assets/schema/migrations/433_flatten_gcode_read_policy_disjunction.sql"
        ),
    },
    EmbeddedMigration {
        version: 434,
        filename: "434_canonical_task_config_keys.sql",
        checksum: "d170cbbb75ea311a8f768fb53dffec0b72e8ba71b9b6e88da251b2911f5e0170",
        sql: include_str!("../../assets/schema/migrations/434_canonical_task_config_keys.sql"),
    },
];
// Numbered migrations after canonical baseline@420 land here.
const _: &str = include_str!("../../assets/schema/migrations/.gitkeep");

/// Schema-equivalent receipts written before an identity-only baseline refresh.
pub(crate) const PRIOR_RECEIPT_CHECKSUMS: &[(i32, &str)] = &[(
    419,
    "a361cb10d591e82aeb0e1ce04eb09e64e468ef571dcd3ae492eccb16cbb4ce81",
)];

pub(crate) fn is_prior_baseline_receipt(version: i32, filename: &str, checksum: &str) -> bool {
    version < BASELINE_VERSION
        && filename == format!("baseline@{version}")
        && PRIOR_RECEIPT_CHECKSUMS
            .iter()
            .any(|(prior_version, prior_checksum)| {
                *prior_version == version && *prior_checksum == checksum
            })
}

pub(crate) fn root_hash() -> String {
    let mut digest = Sha256::new();
    hash_part(&mut digest, &baseline_filename(), BASELINE_CHECKSUM);
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
