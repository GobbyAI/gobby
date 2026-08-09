//! A tiny, deterministic crate used to compare the two CodeWiki engines.

/// Formats a greeting for `name`.
pub fn greeting(name: &str) -> String {
    format!("Hello, {name}!")
}

/// Builds a welcome message by calling [`greeting`].
pub fn welcome(name: &str) -> String {
    format!("{} Welcome to the parity fixture.", greeting(name))
}

/// A documented type that keeps the fixture's symbol topology non-trivial.
pub struct Greeter {
    name: String,
}

impl Greeter {
    /// Creates a greeter for `name`.
    pub fn new(name: impl Into<String>) -> Self {
        Self { name: name.into() }
    }

    /// Returns this greeter's welcome message.
    pub fn welcome(&self) -> String {
        welcome(&self.name)
    }
}
