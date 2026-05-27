//! PostgreSQL foundation adapter boundary.
//!
//! This module is available with the `postgres` feature. Gobby-owned schemas are
//! externally managed; adapter code must validate required objects without
//! creating, altering, or dropping them.
