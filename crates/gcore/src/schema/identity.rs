use serde::Serialize;

use super::assets::{
    BASELINE_CHECKSUM, BASELINE_VERSION, MIGRATIONS, RUNNER_PROTOCOL_VERSION, root_hash,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct AssetIdentity {
    pub version: i32,
    pub filename: &'static str,
    pub checksum: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SchemaIdentity {
    pub runner_protocol_version: u32,
    pub baseline: AssetIdentity,
    pub latest_asset: AssetIdentity,
    pub root_hash: String,
}

pub fn schema_identity() -> SchemaIdentity {
    let baseline = AssetIdentity {
        version: BASELINE_VERSION,
        filename: "baseline@375",
        checksum: BASELINE_CHECKSUM,
    };
    let latest_asset = MIGRATIONS
        .last()
        .map_or(baseline, |migration| AssetIdentity {
            version: migration.version,
            filename: migration.filename,
            checksum: migration.checksum,
        });
    SchemaIdentity {
        runner_protocol_version: RUNNER_PROTOCOL_VERSION,
        baseline,
        latest_asset,
        root_hash: root_hash(),
    }
}
