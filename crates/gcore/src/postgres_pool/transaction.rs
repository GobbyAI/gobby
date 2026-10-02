//! The transaction boundary: one checkout, its commit outcome, and the
//! advisory locks and after-commit callbacks that belong to it.
//!
//! A checkout guard owns the pooled connection. It is armed before `COMMIT`
//! or `ROLLBACK` is awaited and disarmed only when the rollback succeeds, the
//! commit succeeds, or `COMMIT` returns a definite server rejection; dropped
//! armed, it detaches the connection from the pool and closes it. A failed
//! `ROLLBACK` is therefore never logged or returned: its connection is
//! discarded and the caller gets the closure's own error.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};

use tokio_postgres::Row;
use tokio_postgres::error::{DbError, SqlState};
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
    #[error("a statement failed inside the transaction, so it was rolled back")]
    Aborted,
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
    /// Set when a statement fails. PostgreSQL has then aborted the
    /// transaction, and the seam has no savepoint to recover it, so a closure
    /// that swallows the error and returns `Ok` gets `Aborted` instead.
    failed: AtomicBool,
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
        self.record(self.inner.query(statement, params).await)
    }

    pub async fn query_opt(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Option<Row>, TransactionError> {
        self.record(self.inner.query_opt(statement, params).await)
    }

    pub async fn query_one(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Row, TransactionError> {
        self.record(self.inner.query_one(statement, params).await)
    }

    pub async fn execute(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<u64, TransactionError> {
        self.record(self.inner.execute(statement, params).await)
    }

    fn record<T>(&self, result: Result<T, tokio_postgres::Error>) -> Result<T, TransactionError> {
        if result.is_err() {
            self.failed.store(true, Ordering::Relaxed);
        }
        Ok(result?)
    }

    /// Take `target`'s locks until the transaction ends. A target already
    /// held is a no-op; any other must outrank every target held, and both
    /// refusals are decided before any SQL is sent.
    pub async fn acquire_lock(&self, target: &dyn LockTarget) -> Result<(), TransactionError> {
        let identity = (target.priority(), target.keys());
        {
            let locks = locked(&self.locks);
            if locks.contains(&identity) {
                return Ok(());
            }
            if identity.1.is_empty() {
                return Err(TransactionError::EmptyLockTarget);
            }
            if let Some(&(held, _)) = locks.last()
                && identity.0 <= held
            {
                return Err(TransactionError::LockOrder {
                    held,
                    requested: identity.0,
                });
            }
        }
        for key in &identity.1 {
            self.execute("SELECT pg_advisory_xact_lock(hashtext($1))", &[key])
                .await?;
        }
        locked(&self.locks).push(identity);
        Ok(())
    }

    /// Run `callback` after a successful `COMMIT`, once the checkout is back
    /// in the pool, in registration order. Its error is discarded: it cannot
    /// change the committed result, and the remaining callbacks still run.
    pub fn after_commit(&self, callback: Callback) {
        locked(&self.callbacks).push(callback);
    }
}

/// Owns the pooled connection while a transaction is open.
struct Checkout {
    object: Option<deadpool_postgres::Client>,
    /// Set while `COMMIT` or `ROLLBACK` is unresolved, when the session's
    /// transaction state is unknown.
    armed: bool,
}

impl Drop for Checkout {
    fn drop(&mut self) {
        if self.armed
            && let Some(object) = self.object.take()
        {
            drop(deadpool_postgres::Object::take(object));
        }
    }
}

impl Pool {
    /// Check out a connection, `BEGIN`, take `lock`, and run `f`; commit on
    /// `Ok` and run the after-commit callbacks, roll back on `Err`. An `Ok`
    /// after any statement failed rolls back too and returns `Aborted`.
    ///
    /// The two lifetimes in `f`'s argument are independent: tying them
    /// (`&'t Transaction<'t>`) fails the `Send` check of a spawned call.
    pub async fn transaction<T, E, F>(&self, lock: Option<&dyn LockTarget>, f: F) -> Result<T, E>
    where
        F: AsyncFnOnce(&Transaction<'_>) -> Result<T, E>,
        E: From<TransactionError>,
    {
        // The checkout drops at the end of this block, after everything that
        // borrows it, so the callbacks run with the connection back in the pool.
        let (value, callbacks) = {
            let mut checkout = Checkout {
                object: None,
                armed: false,
            };
            let object = checkout
                .object
                .insert(self.get().await.map_err(TransactionError::from)?);
            let transaction = Transaction {
                inner: object.transaction().await.map_err(TransactionError::from)?,
                failed: AtomicBool::default(),
                locks: Mutex::default(),
                callbacks: Mutex::default(),
            };
            let lock_outcome = match lock {
                Some(target) => transaction.acquire_lock(target).await,
                None => Ok(()),
            };
            let outcome = match lock_outcome {
                Ok(()) => match f(&transaction).await {
                    Ok(_) if transaction.failed.load(Ordering::Relaxed) => {
                        Err(TransactionError::Aborted.into())
                    }
                    outcome => outcome,
                },
                Err(error) => Err(error.into()),
            };
            let Transaction {
                inner, callbacks, ..
            } = transaction;

            checkout.armed = true;
            let value = match outcome {
                Ok(value) => value,
                Err(error) => {
                    #[cfg(test)]
                    self.pause_at_rollback_gate().await;
                    if inner.rollback().await.is_ok() {
                        checkout.armed = false;
                    }
                    return Err(error);
                }
            };
            if let Err(error) = inner.commit().await {
                let severity = error
                    .as_db_error()
                    .and_then(DbError::parsed_severity)
                    .map(|severity| severity.to_string());
                if is_definite_commit_rejection(error.code(), severity.as_deref()) {
                    checkout.armed = false;
                    return Err(TransactionError::Server(error).into());
                }
                return Err(TransactionError::IndeterminateCommit(error).into());
            }
            checkout.armed = false;
            (value, callbacks)
        };
        let callbacks = callbacks
            .into_inner()
            .unwrap_or_else(PoisonError::into_inner);
        for callback in callbacks {
            let _ = callback();
        }
        Ok(value)
    }

    /// Test seam: signal the armed rollback once, then suspend before it.
    #[cfg(test)]
    async fn pause_at_rollback_gate(&self) {
        let gate = locked(&self.rollback_gate).take();
        if let Some(gate) = gate {
            let _ = gate.send(());
            std::future::pending::<()>().await;
        }
    }
}

/// No critical section here can leave its data half-updated, so a poisoned
/// lock is still sound to use.
fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Whether a failed `COMMIT` definitely rolled back: an integrity (23) or
/// transaction-rollback (40) class, or any other ERROR-severity response.
/// Cancellation, lock timeout, and statement-completion-unknown leave the
/// outcome unknown, as does a lost connection.
fn is_definite_commit_rejection(code: Option<&SqlState>, severity: Option<&str>) -> bool {
    let Some(code) = code else {
        return false;
    };
    if [
        SqlState::QUERY_CANCELED,
        SqlState::LOCK_NOT_AVAILABLE,
        SqlState::T_R_STATEMENT_COMPLETION_UNKNOWN,
    ]
    .contains(code)
    {
        return false;
    }
    code.code().starts_with("23") || code.code().starts_with("40") || severity == Some("ERROR")
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
