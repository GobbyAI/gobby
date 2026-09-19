//! Project-wide file import communities.
//!
//! [`identity`] resolves module names to the visible files that provide them and
//! loads the import rows a partition is built from. Plan sections 2.2, 2.3 and 3.2
//! add the partition, its id remapping, and its persistence beside it.

pub(crate) mod identity;
