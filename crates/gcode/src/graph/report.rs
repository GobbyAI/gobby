mod generation;
mod loading;
mod queries;
mod render;
mod rows;
mod summary;
#[cfg(test)]
mod tests;
mod time;
mod types;

pub use generation::generate_report_with_options;
pub use types::{ProjectGraphReport, ProjectGraphReportOptions};

#[cfg(test)]
pub(crate) use generation::{empty_report, generate_report};
#[cfg(test)]
pub(crate) use types::{BridgeEdgeHypothesis, ProjectGraphReportError, TargetFrequency};

const RELATES_TO_CODE: &str = "RELATES_TO_CODE";
const DEFAULT_TOP_LIMIT: usize = 10;
