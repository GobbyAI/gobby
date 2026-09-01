use serde::{Deserialize, Serialize};

use super::assets::{
    BASELINE_CHECKSUM, BASELINE_VERSION, MIGRATIONS, RUNNER_PROTOCOL_VERSION, baseline_filename,
    root_hash,
};

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct AssetIdentity {
    pub version: i32,
    pub filename: String,
    pub checksum: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SchemaIdentity {
    pub runner_protocol_version: u32,
    pub baseline: AssetIdentity,
    pub latest_asset: AssetIdentity,
    pub root_hash: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SchemaIdentityContract {
    pub runner_protocol: u32,
    pub baseline_version: i32,
    pub baseline_checksum: String,
    pub latest_version: i32,
    pub latest_checksum: String,
    pub assets_root_hash: String,
}

impl SchemaIdentityContract {
    pub fn embedded() -> Self {
        let identity = schema_identity();
        Self {
            runner_protocol: identity.runner_protocol_version,
            baseline_version: identity.baseline.version,
            baseline_checksum: identity.baseline.checksum.to_owned(),
            latest_version: identity.latest_asset.version,
            latest_checksum: identity.latest_asset.checksum.to_owned(),
            assets_root_hash: identity.root_hash,
        }
    }
}

pub fn schema_identity() -> SchemaIdentity {
    let baseline = AssetIdentity {
        version: BASELINE_VERSION,
        filename: baseline_filename(),
        checksum: BASELINE_CHECKSUM,
    };
    let latest_asset = MIGRATIONS.last().map_or_else(
        || baseline.clone(),
        |migration| AssetIdentity {
            version: migration.version,
            filename: migration.filename.to_owned(),
            checksum: migration.checksum,
        },
    );
    SchemaIdentity {
        runner_protocol_version: RUNNER_PROTOCOL_VERSION,
        baseline,
        latest_asset,
        root_hash: root_hash(),
    }
}
