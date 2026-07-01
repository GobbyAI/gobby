mod operations;
mod request;
mod response;
mod transport;
mod types;

#[cfg(test)]
mod tests;

pub use operations::{
    describe_image_via_daemon, embed_via_daemon, generate_via_daemon,
    generate_via_daemon_with_candidates, generate_via_daemon_with_max_tokens,
    transcribe_via_daemon,
};
pub use types::{DaemonEmbeddingResult, DaemonTranscriptionOptions};

// Shared daemon HTTP/auth primitives reused by the generation-layer
// `DaemonChatTransport` (Lane B) without duplicating token/url resolution.
pub(crate) use transport::{daemon_client, daemon_url, read_local_cli_token, with_local_token};
