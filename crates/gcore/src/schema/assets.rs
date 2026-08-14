use sha2::{Digest, Sha256};

pub const RUNNER_PROTOCOL_VERSION: u32 = 1;
pub const BASELINE_VERSION: i32 = 375;
pub const BASELINE_CHECKSUM: &str =
    "0998a0fd112194440f7f88b42b806486dc63c604ad63c8fc48ddaac16d5bb180";
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

pub(crate) const MIGRATIONS: &[EmbeddedMigration] = &[EmbeddedMigration {
    version: 376,
    filename: "376_copy_agent_definitions.sql",
    checksum: "758b35ad7bc1e2dc4451110b3e392e68c03237ae951ce004fe97b96b618fcc21",
    sql: include_str!("../../assets/schema/migrations/376_copy_agent_definitions.sql"),
}];
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
