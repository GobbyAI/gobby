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
    transcribe_via_daemon, write_codewiki_via_daemon,
};
pub use types::{
    CodeWikiWriterOptions, CodeWikiWriterResult, DaemonEmbeddingResult, DaemonTranscriptionOptions,
};
