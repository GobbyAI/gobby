pub(crate) mod contracts;
mod ddl;
mod identifiers;
mod postgres;
#[cfg(test)]
mod tests;
mod types;

pub(crate) use contracts::DEFAULT_SCHEMA;
pub use postgres::{run_standalone_setup, validate_standalone_request};
pub use types::{StandaloneEmbeddingStatus, StandaloneServicesStatus, StandaloneSetupRequest};

#[cfg(test)]
use ddl::GcodeStandaloneSetup;
#[cfg(test)]
use types::{StandaloneFailure, StandaloneSetupStatus};
