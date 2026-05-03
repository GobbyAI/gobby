use crate::commands::scope;
use crate::config::Context;
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;

pub fn outline(ctx: &Context, file: &str, format: Format, verbose: bool) -> anyhow::Result<()> {
    let conn = db::open_readwrite(&ctx.db_path)?;
    let file = scope::normalize_file_arg(ctx, file);
    let mut stmt = conn.prepare(
        "SELECT * FROM code_symbols WHERE project_id = ?1 AND file_path = ?2 ORDER BY line_start",
    )?;
    let symbols: Vec<Symbol> = stmt
        .query_map(rusqlite::params![&ctx.project_id, &file], Symbol::from_row)?
        .filter_map(|r| r.ok())
        .collect();

    if symbols.is_empty() && !ctx.quiet {
        eprintln!("{}", outline_missing_diagnostic(&conn, ctx, &file));
    }

    // Record savings: outline bytes vs full file bytes
    let file_path = ctx.project_root.join(&file);
    if let Ok(meta) = file_path.metadata() {
        let file_bytes = meta.len() as usize;
        let outline_bytes: usize = symbols
            .iter()
            .map(|s| {
                // Approximate outline size: name + kind + line numbers + signature
                s.qualified_name.len()
                    + s.kind.len()
                    + s.signature.as_ref().map_or(0, |sig| sig.len())
                    + 20 // line numbers, separators
            })
            .sum();
        if outline_bytes > 0 && file_bytes > outline_bytes {
            savings::print_savings(&format!("outline {file}"), file_bytes, outline_bytes);
            if let Some(url) = savings::resolve_daemon_url(None) {
                savings::report_savings(&url, file_bytes, outline_bytes);
            }
        }
    }

    match format {
        Format::Json => {
            if verbose {
                output::print_json(&symbols)
            } else {
                let slim: Vec<_> = symbols.iter().map(|s| s.to_outline()).collect();
                output::print_json(&slim)
            }
        }
        Format::Text => {
            for s in &symbols {
                let indent = if s.parent_symbol_id.is_some() {
                    "  "
                } else {
                    ""
                };
                println!("{indent}{}", format_outline_text_line(s));
            }
            Ok(())
        }
    }
}

fn outline_missing_diagnostic(conn: &rusqlite::Connection, ctx: &Context, file: &str) -> String {
    if scope::path_exists_in_current_project(ctx, file) {
        if scope::indexed_file_exists(conn, &ctx.project_id, file) {
            return format!("file has no indexed symbols in current project: {file}");
        }
        return format!("file not indexed in current project: {file}");
    }

    if let Some(owner) = scope::other_project_for_path(conn, ctx, file) {
        return format!(
            "path belongs to indexed project {} ({}); use --project {}",
            owner.root_path,
            short_id(&owner.id),
            owner.root_path
        );
    }

    if scope::indexed_file_exists(conn, &ctx.project_id, file)
        || scope::content_chunks_exist(conn, &ctx.project_id, file)
    {
        return format!("indexed path missing from current checkout: {file}; run gcode index");
    }

    format!("file not indexed in current project: {file}")
}

fn format_outline_text_line(symbol: &Symbol) -> String {
    let mut line = format!(
        "{}:{}-{} [{}] {} id={}",
        symbol.file_path,
        symbol.line_start,
        symbol.line_end,
        symbol.kind,
        symbol.qualified_name,
        symbol.id
    );
    if let Some(sig) = symbol.signature.as_deref().filter(|sig| !sig.is_empty()) {
        line.push_str(" sig=");
        line.push_str(sig);
    }
    line
}

fn short_id(id: &str) -> &str {
    id.get(..8).unwrap_or(id)
}

pub fn symbol(ctx: &Context, id: &str, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readwrite(&ctx.db_path)?;
    let sym: Option<Symbol> = conn
        .query_row(
            "SELECT * FROM code_symbols WHERE id = ?1",
            rusqlite::params![id],
            Symbol::from_row,
        )
        .ok();

    match sym {
        Some(s) => {
            let file_path = ctx.project_root.join(&s.file_path);
            if file_path.exists() {
                let source = std::fs::read(&file_path)?;
                let file_bytes = source.len();
                let end = s.byte_end.min(source.len());
                let start = s.byte_start.min(end);
                let symbol_bytes = end - start;
                let snippet = String::from_utf8_lossy(&source[start..end]);

                // Record savings: symbol bytes vs full file bytes
                if symbol_bytes > 0 && file_bytes > symbol_bytes {
                    savings::print_savings(
                        &format!("symbol {}", s.qualified_name),
                        file_bytes,
                        symbol_bytes,
                    );
                    if let Some(url) = savings::resolve_daemon_url(None) {
                        savings::report_savings(&url, file_bytes, symbol_bytes);
                    }
                }

                match format {
                    Format::Json => {
                        let mut result = serde_json::to_value(&s)?;
                        result["source"] = serde_json::Value::String(snippet.to_string());
                        output::print_json(&result)
                    }
                    Format::Text => {
                        println!("{snippet}");
                        Ok(())
                    }
                }
            } else {
                match format {
                    Format::Json => output::print_json(&s),
                    Format::Text => {
                        println!("{}: file not found on disk", s.file_path);
                        Ok(())
                    }
                }
            }
        }
        None => anyhow::bail!("Symbol not found: {id}"),
    }
}

pub fn symbols(ctx: &Context, ids: &[String], format: Format) -> anyhow::Result<()> {
    let conn = db::open_readwrite(&ctx.db_path)?;
    let placeholders: Vec<String> = (1..=ids.len()).map(|i| format!("?{i}")).collect();
    let sql = format!(
        "SELECT * FROM code_symbols WHERE id IN ({})",
        placeholders.join(",")
    );
    let mut stmt = conn.prepare(&sql)?;
    let params: Vec<&dyn rusqlite::types::ToSql> = ids
        .iter()
        .map(|s| s as &dyn rusqlite::types::ToSql)
        .collect();
    let results: Vec<Symbol> = stmt
        .query_map(&*params, Symbol::from_row)?
        .filter_map(|r| r.ok())
        .collect();

    // Aggregate savings across batch
    let mut total_file_bytes = 0usize;
    let mut total_symbol_bytes = 0usize;
    for s in &results {
        let file_path = ctx.project_root.join(&s.file_path);
        if let Ok(meta) = file_path.metadata() {
            total_file_bytes += meta.len() as usize;
            total_symbol_bytes += s.byte_end - s.byte_start;
        }
    }
    if total_symbol_bytes > 0 && total_file_bytes > total_symbol_bytes {
        savings::print_savings(
            &format!("symbols ({})", results.len()),
            total_file_bytes,
            total_symbol_bytes,
        );
        if let Some(url) = savings::resolve_daemon_url(None) {
            savings::report_savings(&url, total_file_bytes, total_symbol_bytes);
        }
    }

    match format {
        Format::Json => output::print_json(&results),
        Format::Text => {
            for s in &results {
                println!(
                    "{}:{} [{}] {}",
                    s.file_path, s.line_start, s.kind, s.qualified_name
                );
            }
            Ok(())
        }
    }
}

pub fn kinds(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let mut stmt =
        conn.prepare("SELECT DISTINCT kind FROM code_symbols WHERE project_id = ?1 ORDER BY kind")?;
    let kinds: Vec<String> = stmt
        .query_map(rusqlite::params![&ctx.project_id], |row| row.get(0))?
        .filter_map(|r| r.ok())
        .collect();

    match format {
        Format::Json => output::print_json(&kinds),
        Format::Text => {
            for k in &kinds {
                println!("{k}");
            }
            Ok(())
        }
    }
}

pub fn tree(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let conn = db::open_readonly(&ctx.db_path)?;
    let mut stmt = conn.prepare(
        "SELECT file_path, language, symbol_count FROM code_indexed_files \
         WHERE project_id = ?1 ORDER BY file_path",
    )?;

    let files: Vec<serde_json::Value> = stmt
        .query_map(rusqlite::params![&ctx.project_id], |row| {
            Ok(serde_json::json!({
                "file_path": row.get::<_, String>(0)?,
                "language": row.get::<_, String>(1)?,
                "symbol_count": row.get::<_, i64>(2)?,
            }))
        })?
        .filter_map(|r| r.ok())
        .collect();

    match format {
        Format::Json => output::print_json(&files),
        Format::Text => {
            for f in &files {
                println!(
                    "{} [{}] ({} symbols)",
                    f["file_path"].as_str().unwrap_or(""),
                    f["language"].as_str().unwrap_or(""),
                    f["symbol_count"].as_i64().unwrap_or(0),
                );
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn context_for(root: &std::path::Path, db_path: std::path::PathBuf) -> Context {
        Context {
            db_path,
            project_root: root.to_path_buf(),
            project_id: "current-project".to_string(),
            quiet: false,
            neo4j: None,
            qdrant: None,
            embedding: None,
            daemon_url: None,
        }
    }

    fn setup_conn() -> rusqlite::Connection {
        let conn = rusqlite::Connection::open_in_memory().expect("open sqlite");
        conn.execute_batch(
            "CREATE TABLE code_indexed_projects (
                id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL
            );
            CREATE TABLE code_indexed_files (
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL
            );
            CREATE TABLE code_content_chunks (
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL
            );",
        )
        .expect("create schema");
        conn
    }

    fn symbol() -> Symbol {
        Symbol {
            id: "12345678-1234-5678-1234-567812345678".to_string(),
            project_id: "current-project".to_string(),
            file_path: "src/commands.rs".to_string(),
            name: "outline".to_string(),
            qualified_name: "outline".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 10,
            line_start: 7,
            line_end: 63,
            signature: Some("pub fn outline() -> anyhow::Result<()> {".to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn outline_text_line_includes_id_range_and_signature() {
        let line = format_outline_text_line(&symbol());

        assert!(line.contains("src/commands.rs:7-63 [function] outline"));
        assert!(line.contains("id=12345678-1234-5678-1234-567812345678"));
        assert!(line.contains("sig=pub fn outline() -> anyhow::Result<()> {"));
    }

    #[test]
    fn outline_diagnostic_reports_unindexed_current_file() {
        let current = tempfile::tempdir().expect("current tempdir");
        let src = current.path().join("src");
        std::fs::create_dir_all(&src).expect("create src");
        std::fs::write(src.join("lib.rs"), "fn main() {}").expect("write file");
        let conn = setup_conn();
        let ctx = context_for(current.path(), current.path().join("index.db"));

        let diagnostic = outline_missing_diagnostic(&conn, &ctx, "src/lib.rs");

        assert_eq!(
            diagnostic,
            "file not indexed in current project: src/lib.rs"
        );
    }

    #[test]
    fn outline_diagnostic_reports_other_project_owner() {
        let conn = setup_conn();
        let current = tempfile::tempdir().expect("current tempdir");
        let other = tempfile::tempdir().expect("other tempdir");
        conn.execute(
            "INSERT INTO code_indexed_projects (id, root_path) VALUES (?1, ?2)",
            rusqlite::params!["other-project", other.path().to_string_lossy().to_string()],
        )
        .expect("insert project");
        conn.execute(
            "INSERT INTO code_indexed_files (project_id, file_path) VALUES ('other-project', 'src/lib.rs')",
            [],
        )
        .expect("insert file");
        let ctx = context_for(current.path(), current.path().join("index.db"));

        let diagnostic = outline_missing_diagnostic(&conn, &ctx, "src/lib.rs");

        assert!(diagnostic.contains("path belongs to indexed project"));
        assert!(diagnostic.contains("(other-pr)"));
    }

    #[test]
    fn outline_diagnostic_reports_stale_current_index() {
        let conn = setup_conn();
        let current = tempfile::tempdir().expect("current tempdir");
        conn.execute(
            "INSERT INTO code_indexed_files (project_id, file_path) VALUES ('current-project', 'src/missing.rs')",
            [],
        )
        .expect("insert file");
        let ctx = context_for(current.path(), current.path().join("index.db"));

        let diagnostic = outline_missing_diagnostic(&conn, &ctx, "src/missing.rs");

        assert_eq!(
            diagnostic,
            "indexed path missing from current checkout: src/missing.rs; run gcode index"
        );
    }
}
