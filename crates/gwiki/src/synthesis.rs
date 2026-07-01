mod generate;
mod paths;
mod render;
mod types;
mod write;

pub use generate::{synthesize_article, synthesize_source_pages};
pub use paths::{
    ensure_synthesized_path_inside_vault, relative_path, slugify, slugify_unique, wiki_link,
};
pub use types::{
    ArticleKind, PageWriteKind, PageWriteOutcome, SynthesisInput, SynthesisPrompt, SynthesisSource,
    SynthesizedPage, WritePolicy,
};
pub use write::{ensure_page_write_allowed, write_synthesized_page};

#[cfg(test)]
mod tests;
