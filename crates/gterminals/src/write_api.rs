//! The loopback write route's admission: the current backend child's token.

use std::sync::{Arc, PoisonError, RwLock};

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum WriteRefused {
    #[error("no supervised backend is attached")]
    NoBackend,
    #[error("the request carries no write token")]
    MissingToken,
    #[error("the write token is not the current backend's")]
    WrongToken,
}

/// The token gdaemon minted for the backend child it supervises now.
#[derive(Clone, Default)]
pub struct TokenSlot {
    current: Arc<RwLock<Option<Arc<str>>>>,
}

/// Holds one start's token in its slot; dropping it retires that token only.
pub struct TokenLease {
    slot: TokenSlot,
    token: Arc<str>,
}

impl TokenSlot {
    pub fn install(&self, token: String) -> TokenLease {
        let token: Arc<str> = token.into();
        *self.current.write().unwrap_or_else(PoisonError::into_inner) = Some(token.clone());
        TokenLease {
            slot: self.clone(),
            token,
        }
    }

    pub fn authorize(&self, presented: Option<&str>) -> Result<(), WriteRefused> {
        let current = self.current.read().unwrap_or_else(PoisonError::into_inner);
        let current = current.as_deref().ok_or(WriteRefused::NoBackend)?;
        match presented {
            None | Some("") => Err(WriteRefused::MissingToken),
            Some(presented) if constant_time_eq(presented.as_bytes(), current.as_bytes()) => Ok(()),
            Some(_) => Err(WriteRefused::WrongToken),
        }
    }
}

impl Drop for TokenLease {
    fn drop(&mut self) {
        let mut current = self
            .slot
            .current
            .write()
            .unwrap_or_else(PoisonError::into_inner);
        if current
            .as_ref()
            .is_some_and(|c| Arc::ptr_eq(c, &self.token))
        {
            *current = None;
        }
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .zip(right)
            .fold(0_u8, |acc, (a, b)| acc | (a ^ b))
            == 0
}
