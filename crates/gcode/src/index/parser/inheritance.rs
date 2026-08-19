//! Heritage extraction. Row production lands in 1.2; 1.1 only declares the
//! sibling module and returns an empty vec from parse.

use crate::models::InheritanceRelation;

pub(crate) fn extract_inheritance() -> Vec<InheritanceRelation> {
    Vec::new()
}
