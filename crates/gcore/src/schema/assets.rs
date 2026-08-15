use sha2::{Digest, Sha256};

pub const RUNNER_PROTOCOL_VERSION: u32 = 1;
pub const BASELINE_VERSION: i32 = 375;
pub const BASELINE_CHECKSUM: &str =
    "5d598c3609d0bdbcfd10f1c363c60bd38d5625100c3e8719b3ff42d189047117";
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
        checksum: "43b6c25263c1e510f28c540d9cc24e62ffcad67a3a8dddde3645bdb6a5e92821",
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
];
const _: &str = include_str!("../../assets/schema/migrations/.gitkeep");

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
