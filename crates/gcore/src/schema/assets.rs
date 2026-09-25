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
    EmbeddedMigration {
        version: 435,
        filename: "435_bind_tool_grants_to_requested_checkout.sql",
        checksum: "f636cf590a56f7ea7e31f07d4eb2dd6bc4379a6c39e23f018dcd42f702cc3e0e",
        sql: include_str!(
            "../../assets/schema/migrations/435_bind_tool_grants_to_requested_checkout.sql"
        ),
    },
    EmbeddedMigration {
        version: 436,
        filename: "436_add_session_heuristic_title_and_reasoning_effort.sql",
        checksum: "6fa7aef5d57f478717db6d50c601ef205dc72c04d9d6fdeaf2b1c2fe83bd8453",
        sql: include_str!(
            "../../assets/schema/migrations/436_add_session_heuristic_title_and_reasoning_effort.sql"
        ),
    },
    EmbeddedMigration {
        version: 437,
        filename: "437_add_ask_artifacts.sql",
        checksum: "760f6ff692279b5f5130c5c5a24e06ac10899685ebea90a73313f1af0b5cc88f",
        sql: include_str!("../../assets/schema/migrations/437_add_ask_artifacts.sql"),
    },
    EmbeddedMigration {
        version: 438,
        filename: "438_rotate_expired_principal.sql",
        checksum: "630000397ba6e8aef4494d73bfa1b325a671c1e6a954c39e5db23b9a22215467",
        sql: include_str!("../../assets/schema/migrations/438_rotate_expired_principal.sql"),
    },
    EmbeddedMigration {
        version: 439,
        filename: "439_retire_linear_github_issue_bridge.sql",
        checksum: "1595df4c23e3052a3848c06c0a812f866fabdbc18d6fbf9bacca7c634ff7adf6",
        sql: include_str!(
            "../../assets/schema/migrations/439_retire_linear_github_issue_bridge.sql"
        ),
    },
    EmbeddedMigration {
        version: 440,
        filename: "440_add_workspaces.sql",
        checksum: "2a9410cd8cb7dcec08f429fdbc1b1f7043cc3f97efc7a3affdbcfb21c166fd52",
        sql: include_str!("../../assets/schema/migrations/440_add_workspaces.sql"),
    },
    EmbeddedMigration {
        version: 441,
        filename: "441_add_coordination_reply_waits.sql",
        checksum: "c0a85147b36e2be3dd401ee4812fb3050863cff96860e1d78fb35610ebc4f44f",
        sql: include_str!("../../assets/schema/migrations/441_add_coordination_reply_waits.sql"),
    },
    EmbeddedMigration {
        version: 442,
        filename: "442_zero_based_refs.sql",
        checksum: "a9751a8ab49b65c1e0ad4c4a540a12f1aa0824cb582bd77c6f474c35d2315697",
        sql: include_str!("../../assets/schema/migrations/442_zero_based_refs.sql"),
    },
    EmbeddedMigration {
        version: 443,
        filename: "443_add_code_communities.sql",
        checksum: "ff143a0c2d040ebd44b5138e717608705e81d42176dece35561802fcd1e06229",
        sql: include_str!("../../assets/schema/migrations/443_add_code_communities.sql"),
    },
    EmbeddedMigration {
        version: 444,
        filename: "444_add_queued_close_reviews.sql",
        checksum: "c58e6345ec07b37f79c423f8dcbeb1a72ce924145abf9880c3cfe58319d0db03",
        sql: include_str!("../../assets/schema/migrations/444_add_queued_close_reviews.sql"),
    },
    EmbeddedMigration {
        version: 445,
        filename: "445_drop_task_validation_system_prompt.sql",
        checksum: "55650f9d4147ec2c37f76512f88bcc5252a5f8247a35b4795b3e49e2b3dca03a",
        sql: include_str!(
            "../../assets/schema/migrations/445_drop_task_validation_system_prompt.sql"
        ),
    },
    EmbeddedMigration {
        version: 446,
        filename: "446_grant_agent_project_resolution.sql",
        checksum: "92af848e93ef46536f40fdb0084dcf5f0fa4bc3b5b7eaf9788ee020c977d3b31",
        sql: include_str!("../../assets/schema/migrations/446_grant_agent_project_resolution.sql"),
    },
    EmbeddedMigration {
        version: 447,
        filename: "447_coordination_wait_live_identity.sql",
        checksum: "7de2a20e9697002df4ca27ca8a85c3a043266a5a622449419e26a73cfdfb11e8",
        sql: include_str!("../../assets/schema/migrations/447_coordination_wait_live_identity.sql"),
    },
    EmbeddedMigration {
        version: 448,
        filename: "448_add_task_delegation.sql",
        checksum: "51c14abba79016cc9c5ff0c045650315fc213420e8694a5713d50d215c49e67b",
        sql: include_str!("../../assets/schema/migrations/448_add_task_delegation.sql"),
    },
    EmbeddedMigration {
        version: 449,
        filename: "449_workspace_default_project.sql",
        checksum: "41cfe81ece42cc02275d859df92ec978a0b768128ea9e8c419560b17d36f016f",
        sql: include_str!("../../assets/schema/migrations/449_workspace_default_project.sql"),
    },
    EmbeddedMigration {
        version: 450,
        filename: "450_drop_session_heuristic_title.sql",
        checksum: "449ae04f4d41057f071eaaae16df9365d6b94f03b725e92202b239369f730cf6",
        sql: include_str!("../../assets/schema/migrations/450_drop_session_heuristic_title.sql"),
    },
    EmbeddedMigration {
        version: 451,
        filename: "451_drop_comms_routing_rules.sql",
        checksum: "96644fa5866262c425fa3a8f43cf74110b4d94a8ebe8511112e5bf48a416b941",
        sql: include_str!("../../assets/schema/migrations/451_drop_comms_routing_rules.sql"),
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
