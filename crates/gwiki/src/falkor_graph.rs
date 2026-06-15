mod boost;
mod code_edges;
mod query;
mod sync;
#[cfg(test)]
mod tests;
mod wiki_facts;

pub(crate) use boost::load_graph_boost_data;
pub(crate) use code_edges::load_code_graph_edges;
pub(crate) use sync::sync_scope_from_postgres;
pub(crate) use wiki_facts::load_wiki_graph_facts;

use std::collections::BTreeSet;

use gobby_core::degradation::DegradationKind;

use crate::graph::WikiGraphCodeEdge;

pub const FALKORDB_GRAPH_NAME: &str = "gobby_wiki";
pub(crate) const SHARED_CODE_GRAPH_TRUNCATED_SOURCE: &str = "shared_code_graph_truncated";
const CODE_GRAPH_PROVENANCE: &str = "shared_code_graph";
const CODE_CALL_EDGE_TRUNCATION_COMPONENT: &str = "code_call_edges";
const CODE_IMPORT_EDGE_TRUNCATION_COMPONENT: &str = "code_import_edges";
const CODE_TOTAL_EDGE_TRUNCATION_COMPONENT: &str = "code_edges";
/// Hard cap for shared code graph edges loaded into one wiki graph context pack.
const MAX_TOTAL_CODE_EDGES: usize = 1000;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct SharedCodeGraphTruncation {
    pub(crate) components: Vec<String>,
}

impl SharedCodeGraphTruncation {
    fn from_components(components: BTreeSet<String>) -> Self {
        Self {
            components: components.into_iter().collect(),
        }
    }

    pub(crate) fn is_truncated(&self) -> bool {
        !self.components.is_empty()
    }
}

#[derive(Clone, Debug, Default)]
pub(crate) struct SharedCodeGraphEdges {
    pub(crate) edges: Vec<WikiGraphCodeEdge>,
    pub(crate) truncation: SharedCodeGraphTruncation,
}

pub(crate) struct GraphBoostData {
    pub documents: Vec<crate::search::graph_boost::GraphBoostDocument>,
    pub links: Vec<crate::search::graph_boost::GraphBoostLink>,
    pub degradation: Option<DegradationKind>,
}
