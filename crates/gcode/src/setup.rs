mod contracts;
mod ddl;
mod identifiers;
mod postgres;
#[cfg(test)]
mod tests;
mod types;

pub(crate) use contracts::DEFAULT_SCHEMA;
pub use ddl::GcodeStandaloneSetup;
pub use postgres::{run_standalone_setup, validate_standalone_request};
pub use types::{
    StandaloneEmbeddingStatus, StandaloneFailure, StandaloneServicesStatus, StandaloneSetupRequest,
    StandaloneSetupStatus,
};
