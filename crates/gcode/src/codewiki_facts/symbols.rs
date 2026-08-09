use crate::models::Symbol;
use crate::visibility;

use super::{CodewikiFacts, FileId};

/// Owned symbol record safe to pass outside gcode.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SymbolFact {
    pub id: String,
    pub file: FileId,
    pub file_path: String,
    pub name: String,
    pub qualified_name: String,
    pub kind: String,
    pub language: String,
    pub byte_start: usize,
    pub byte_end: usize,
    pub line_start: usize,
    pub line_end: usize,
    pub signature: Option<String>,
    pub docstring: Option<String>,
    pub parent_symbol_id: Option<String>,
    pub file_content_hash: String,
    pub content_hash: String,
    pub summary: Option<String>,
}

impl From<Symbol> for SymbolFact {
    fn from(symbol: Symbol) -> Self {
        Self {
            id: symbol.id,
            file: FileId::new(symbol.file_path.clone()),
            file_path: symbol.file_path,
            name: symbol.name,
            qualified_name: symbol.qualified_name,
            kind: symbol.kind,
            language: symbol.language,
            byte_start: symbol.byte_start,
            byte_end: symbol.byte_end,
            line_start: symbol.line_start,
            line_end: symbol.line_end,
            signature: symbol.signature,
            docstring: symbol.docstring,
            parent_symbol_id: symbol.parent_symbol_id,
            file_content_hash: symbol.file_content_hash,
            content_hash: symbol.content_hash,
            summary: symbol.summary,
        }
    }
}

impl CodewikiFacts {
    pub fn symbols_in(&self, files: &[FileId]) -> anyhow::Result<Vec<SymbolFact>> {
        let paths = files
            .iter()
            .map(|file| file.as_str().to_string())
            .collect::<Vec<_>>();
        let mut conn = self.read_connection()?;
        Ok(
            visibility::visible_symbols_for_files(&mut conn, self.context(), &paths)?
                .into_iter()
                .map(SymbolFact::from)
                .collect(),
        )
    }

    pub fn symbols_for_file(&self, file: &FileId) -> anyhow::Result<Vec<SymbolFact>> {
        self.symbols_in(std::slice::from_ref(file))
    }

    pub fn symbol_by_id(&self, id: &str) -> anyhow::Result<Option<SymbolFact>> {
        let mut conn = self.read_connection()?;
        Ok(visibility::visible_symbol_by_id(&mut conn, self.context(), id)?.map(SymbolFact::from))
    }
}
