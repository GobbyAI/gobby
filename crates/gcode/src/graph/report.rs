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

pub use generation::{empty_report, generate_report, generate_report_with_options};
pub use types::{
    BridgeEdgeHypothesis, BridgeReportSummary, ConfidenceRange, GraphHotspot, GraphReportHotspots,
    GraphReportSummary, NamedCount, ProjectGraphReport, ProjectGraphReportError,
    ProjectGraphReportOptions, ReportDegradation, TargetFrequency,
};

const RELATES_TO_CODE: &str = "RELATES_TO_CODE";
const DEFAULT_TOP_LIMIT: usize = 10;
