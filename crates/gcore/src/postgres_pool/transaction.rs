//! The transaction boundary: one checkout, its commit outcome, and the
//! advisory locks and after-commit callbacks that belong to it.
//!
//! A checkout guard owns the pooled connection. It is armed before `COMMIT`
//! or `ROLLBACK` is awaited and disarmed only when the rollback succeeds, the
//! commit succeeds, or `COMMIT` returns a definite server rejection; dropped
//! armed, it detaches the connection from the pool and closes it. A failed
//! `ROLLBACK` is therefore never logged or returned: its connection is
//! discarded and the caller gets the closure's own error.

use std::sync::Mutex;

use tokio_postgres::Row;
use tokio_postgres::error::SqlState;
use tokio_postgres::types::ToSql;

use super::row::RowError;
use super::{Pool, PoolError};

/// A synchronous after-commit callback. One that needs database work signals
/// an async task (a channel send or `Notify`) instead of blocking a worker.
type Callback = Box<dyn FnOnce() -> anyhow::Result<()> + Send>;

/// An advisory lock a transaction holds until it ends. Family crates define
/// their own; the keys are the Python daemon's strings (for example
/// `task_lifecycle:{task_id}`), so both daemons contend on the same locks.
pub trait LockTarget: Sync {
    /// Nested acquisitions in one transaction must strictly increase this.
    fn priority(&self) -> i32;
    /// Locked in order, each with `pg_advisory_xact_lock(hashtext(key))`.
    fn keys(&self) -> Vec<String>;
}

#[derive(Debug, thiserror::Error)]
pub enum TransactionError {
    #[error(transparent)]
    Pool(#[from] PoolError),
    #[error("PostgreSQL statement failed")]
    Server(#[from] tokio_postgres::Error),
    #[error("the PostgreSQL COMMIT outcome was not observed")]
    IndeterminateCommit(#[source] tokio_postgres::Error),
    #[error("nested lock priority must increase: {held} -> {requested}")]
    LockOrder { held: i32, requested: i32 },
    #[error("a lock target named no keys")]
    EmptyLockTarget,
    #[error(transparent)]
    Row(#[from] RowError),
}

/// One open transaction, lent to the `Pool::transaction` closure.
pub struct Transaction<'c> {
    inner: deadpool_postgres::Transaction<'c>,
    /// `(priority, keys)` of each lock target held, outermost first.
    locks: Mutex<Vec<(i32, Vec<String>)>>,
    callbacks: Mutex<Vec<Callback>>,
}

impl Transaction<'_> {
    pub async fn query(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Vec<Row>, TransactionError> {
        todo!()
    }

    pub async fn query_opt(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Option<Row>, TransactionError> {
        todo!()
    }

    pub async fn query_one(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Row, TransactionError> {
        todo!()
    }

    pub async fn execute(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<u64, TransactionError> {
        todo!()
    }

    pub async fn acquire_lock(&self, target: &dyn LockTarget) -> Result<(), TransactionError> {
        todo!()
    }

    pub fn after_commit(&self, callback: Callback) {
        todo!()
    }
}

impl Pool {
    pub async fn transaction<T, E, F>(&self, lock: Option<&dyn LockTarget>, f: F) -> Result<T, E>
    where
        F: for<'t> AsyncFnOnce(&'t Transaction<'t>) -> Result<T, E>,
        E: From<TransactionError>,
    {
        todo!()
    }
}

fn is_definite_commit_rejection(code: Option<&SqlState>, severity: Option<&str>) -> bool {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commit_rejection_table() {
        let cases = [
            (Some(SqlState::UNIQUE_VIOLATION), Some("ERROR"), true),
            (Some(SqlState::UNIQUE_VIOLATION), Some("FATAL"), true),
            (
                Some(SqlState::T_R_SERIALIZATION_FAILURE),
                Some("ERROR"),
                true,
            ),
            (Some(SqlState::T_R_DEADLOCK_DETECTED), None, true),
            (
                Some(SqlState::T_R_STATEMENT_COMPLETION_UNKNOWN),
                Some("ERROR"),
                false,
            ),
            (Some(SqlState::QUERY_CANCELED), Some("ERROR"), false),
            (Some(SqlState::LOCK_NOT_AVAILABLE), Some("ERROR"), false),
            (Some(SqlState::DIVISION_BY_ZERO), Some("ERROR"), true),
            (Some(SqlState::ADMIN_SHUTDOWN), Some("FATAL"), false),
            (None, None, false),
        ];
        for (code, severity, definite) in cases {
            assert_eq!(
                is_definite_commit_rejection(code.as_ref(), severity),
                definite,
                "{:?} at {severity:?}",
                code.as_ref().map(SqlState::code)
            );
        }
    }
}
