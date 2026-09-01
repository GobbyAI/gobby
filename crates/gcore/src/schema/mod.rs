//! PostgreSQL schema authority for Gobby.
//!
//! This module owns schema DDL, migration application, and read-only schema
//! verification. Consumer crates retain ownership of CRUD and domain behavior.

mod assets;
mod attached;
mod error;
mod external;
mod gate;
mod identity;
mod runner;
mod sql_splitter;
mod sweep;
mod verify;

#[cfg(test)]
mod runner_tests;

pub use assets::{
    BASELINE_CHECKSUM, BASELINE_SQL, BASELINE_VERSION, CATALOG_MANIFEST_JSON,
    RUNNER_PROTOCOL_VERSION, SEED_MANIFEST_JSON,
};
pub use attached::{
    AttachedValidator, RequiredObject, StoreKind, ValidationContext, ValidationReport,
};
pub use error::SchemaError;
pub use external::{
    ExternalPostgresObject, ExternalPostgresObjectKind, ExternalSchemaError,
    gcode_postgres_objects, gwiki_postgres_objects,
};
pub use gate::{
    ArtifactRecord, BackupGateContext, BackupManifestError, HubBackupManifest, SourceIdentity,
    StoreRecord, VerificationState, VerifiedBackupManifest, parse_backup_manifest,
};
pub use identity::{AssetIdentity, SchemaIdentity, SchemaIdentityContract, schema_identity};
pub use runner::{ApplyReport, SchemaRunner};
pub use sql_splitter::split_sql_statements;
pub use sweep::sweep_test_schemas;
pub use verify::{
    CatalogEntry, CatalogManifest, VerificationReport, catalog_manifest, render_catalog_manifest,
};
