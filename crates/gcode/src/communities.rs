//! Project-wide file import communities.
//!
//! [`identity`] resolves module names to the visible files that provide them and
//! loads the import rows a partition is built from. [`partition`] turns those
//! rows into communities and [`labels`] names them. Plan sections 2.3 and 3.2
//! add the id remapping and the persistence beside them.

#[allow(dead_code)] // consumed by the 3.2 persistence stage
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum LabelSource {
    Deterministic,
    Model,
}

#[allow(dead_code)] // consumed by the 3.2 persistence stage
impl LabelSource {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Deterministic => "deterministic",
            Self::Model => "model",
        }
    }

    pub(crate) fn parse(value: &str) -> Option<Self> {
        match value {
            "deterministic" => Some(Self::Deterministic),
            "model" => Some(Self::Model),
            _ => None,
        }
    }
}

pub(crate) mod identity;
pub(crate) mod labels;
pub(crate) mod partition;
pub(crate) mod remap;
