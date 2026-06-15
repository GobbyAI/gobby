/// How a file should be indexed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileClassification {
    Ast,
    ContentOnly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiscoveryOptions {
    pub respect_gitignore: bool,
}

impl Default for DiscoveryOptions {
    fn default() -> Self {
        Self {
            respect_gitignore: true,
        }
    }
}
