//! Project-wide file import communities.
//!
//! [`identity`] resolves module names to the visible files that provide them and
//! loads the import rows a partition is built from. [`partition`] turns those
//! rows into communities and [`labels`] names them. Plan sections 2.3 and 3.2
//! add the id remapping and the persistence beside them.

pub(crate) mod identity;
pub(crate) mod labels;
pub(crate) mod partition;
