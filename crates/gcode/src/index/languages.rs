//! Language registry with tree-sitter query definitions.
//! Ports 16 language specs from src/gobby/code_index/languages.py.

use std::collections::HashSet;
use tree_sitter::Language;

/// Specification for a single language's tree-sitter queries.
pub struct LanguageSpec {
    pub extensions: &'static [&'static str],
    pub symbol_query: &'static str,
    pub import_query: &'static str,
    pub call_query: &'static str,
}

// ── Query Definitions ──────────────────────────────────────────────────

const PYTHON: LanguageSpec = LanguageSpec {
    extensions: &[".py", ".pyi"],
    symbol_query: r#"
        (function_definition name: (identifier) @name) @definition.function
        (class_definition name: (identifier) @name) @definition.class
    "#,
    import_query: r#"
        (import_statement) @import
        (import_from_statement) @import
    "#,
    call_query: r#"
        (call function: (identifier) @name) @call
        (call function: (attribute attribute: (identifier) @name)) @call
    "#,
};

const JAVASCRIPT: LanguageSpec = LanguageSpec {
    extensions: &[".js", ".jsx", ".mjs", ".cjs"],
    symbol_query: r#"
        (function_declaration name: (identifier) @name) @definition.function
        (class_declaration name: (identifier) @name) @definition.class
        (method_definition name: (property_identifier) @name) @definition.method
        (export_statement declaration: (function_declaration name: (identifier) @name)) @definition.function
        (export_statement declaration: (class_declaration name: (identifier) @name)) @definition.class
        (lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function))) @definition.function
    "#,
    import_query: r#"
        (import_statement) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (call_expression function: (member_expression property: (property_identifier) @name)) @call
    "#,
};

const TYPESCRIPT: LanguageSpec = LanguageSpec {
    extensions: &[".ts", ".tsx"],
    symbol_query: r#"
        (function_declaration name: (identifier) @name) @definition.function
        (class_declaration name: (type_identifier) @name) @definition.class
        (method_definition name: (property_identifier) @name) @definition.method
        (interface_declaration name: (type_identifier) @name) @definition.type
        (type_alias_declaration name: (type_identifier) @name) @definition.type
        (enum_declaration name: (identifier) @name) @definition.type
        (lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function))) @definition.function
        (export_statement declaration: (function_declaration name: (identifier) @name)) @definition.function
        (export_statement declaration: (class_declaration name: (type_identifier) @name)) @definition.class
        (export_statement declaration: (interface_declaration name: (type_identifier) @name)) @definition.type
        (export_statement declaration: (type_alias_declaration name: (type_identifier) @name)) @definition.type
        (export_statement declaration: (enum_declaration name: (identifier) @name)) @definition.type
        (export_statement declaration: (lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function)))) @definition.function
    "#,
    import_query: r#"
        (import_statement) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (call_expression function: (member_expression property: (property_identifier) @name)) @call
    "#,
};

const GO: LanguageSpec = LanguageSpec {
    extensions: &[".go"],
    symbol_query: r#"
        (function_declaration name: (identifier) @name) @definition.function
        (method_declaration name: (field_identifier) @name) @definition.method
        (type_declaration (type_spec name: (type_identifier) @name)) @definition.type
    "#,
    import_query: r#"
        (import_declaration) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (call_expression function: (selector_expression field: (field_identifier) @name)) @call
    "#,
};

const RUST: LanguageSpec = LanguageSpec {
    extensions: &[".rs"],
    symbol_query: r#"
        (function_item name: (identifier) @name) @definition.function
        (struct_item name: (type_identifier) @name) @definition.class
        (enum_item name: (type_identifier) @name) @definition.type
        (trait_item name: (type_identifier) @name) @definition.type
        (impl_item type: (type_identifier) @name) @definition.class
        (type_item name: (type_identifier) @name) @definition.type
    "#,
    import_query: r#"
        (use_declaration) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (call_expression function: (scoped_identifier name: (identifier) @name)) @call
        (call_expression function: (field_expression field: (field_identifier) @name)) @call
    "#,
};

const JAVA: LanguageSpec = LanguageSpec {
    extensions: &[".java"],
    symbol_query: r#"
        (method_declaration name: (identifier) @name) @definition.method
        (class_declaration name: (identifier) @name) @definition.class
        (interface_declaration name: (identifier) @name) @definition.type
        (enum_declaration name: (identifier) @name) @definition.type
        (constructor_declaration name: (identifier) @name) @definition.method
    "#,
    import_query: r#"
        (import_declaration) @import
    "#,
    call_query: r#"
        (method_invocation name: (identifier) @name) @call
    "#,
};

const PHP: LanguageSpec = LanguageSpec {
    extensions: &[".php"],
    symbol_query: r#"
        (function_definition name: (name) @name) @definition.function
        (class_declaration name: (name) @name) @definition.class
        (method_declaration name: (name) @name) @definition.method
        (interface_declaration name: (name) @name) @definition.type
        (trait_declaration name: (name) @name) @definition.type
    "#,
    import_query: r#"
        (namespace_use_declaration) @import
    "#,
    call_query: r#"
        (function_call_expression function: (name) @name) @call
        (member_call_expression name: (name) @name) @call
    "#,
};

const DART: LanguageSpec = LanguageSpec {
    extensions: &[".dart"],
    symbol_query: r#"
        (function_signature name: (identifier) @name) @definition.function
        (class_definition name: (identifier) @name) @definition.class
        (method_signature (function_signature (identifier) @name)) @definition.method
        (enum_declaration name: (identifier) @name) @definition.type
    "#,
    import_query: r#"
        (import_or_export) @import
    "#,
    call_query: "",
};

const CSHARP: LanguageSpec = LanguageSpec {
    extensions: &[".cs"],
    symbol_query: r#"
        (method_declaration name: (identifier) @name) @definition.method
        (class_declaration name: (identifier) @name) @definition.class
        (interface_declaration name: (identifier) @name) @definition.type
        (struct_declaration name: (identifier) @name) @definition.type
        (enum_declaration name: (identifier) @name) @definition.type
        (constructor_declaration name: (identifier) @name) @definition.method
    "#,
    import_query: r#"
        (using_directive) @import
    "#,
    call_query: r#"
        (invocation_expression function: (identifier) @name) @call
        (invocation_expression function: (member_access_expression name: (identifier) @name)) @call
    "#,
};

const C_LANG: LanguageSpec = LanguageSpec {
    extensions: &[".c", ".h"],
    symbol_query: r#"
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @definition.function
        (struct_specifier name: (type_identifier) @name) @definition.type
        (enum_specifier name: (type_identifier) @name) @definition.type
        (type_definition declarator: (type_identifier) @name) @definition.type
    "#,
    import_query: r#"
        (preproc_include) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
    "#,
};

const CPP: LanguageSpec = LanguageSpec {
    extensions: &[".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".hh"],
    symbol_query: r#"
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @definition.function
        (function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @name))) @definition.function
        (class_specifier name: (type_identifier) @name) @definition.class
        (struct_specifier name: (type_identifier) @name) @definition.type
    "#,
    import_query: r#"
        (preproc_include) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (call_expression function: (field_expression field: (field_identifier) @name)) @call
    "#,
};

const ELIXIR: LanguageSpec = LanguageSpec {
    extensions: &[".ex", ".exs"],
    symbol_query: r#"
        (call target: (identifier) @_keyword (#any-of? @_keyword "def" "defp" "defmacro") (arguments (identifier) @name)) @definition.function
        (call target: (identifier) @_keyword (#any-of? @_keyword "defmodule") (arguments (alias) @name)) @definition.class
    "#,
    import_query: r#"
        (call target: (identifier) @_keyword (#any-of? @_keyword "import" "alias" "use" "require")) @import
    "#,
    call_query: "",
};

const RUBY: LanguageSpec = LanguageSpec {
    extensions: &[".rb", ".rake", ".gemspec"],
    symbol_query: r#"
        (method name: (identifier) @name) @definition.function
        (singleton_method name: (identifier) @name) @definition.function
        (class name: (constant) @name) @definition.class
        (module name: (constant) @name) @definition.class
    "#,
    import_query: r#"
        (call method: (identifier) @_m (#any-of? @_m "require" "require_relative" "include")) @import
    "#,
    call_query: r#"
        (call method: (identifier) @name) @call
    "#,
};

const MARKDOWN: LanguageSpec = LanguageSpec {
    extensions: &[".md", ".markdown"],
    symbol_query: r#"
        (atx_heading heading_content: (_) @name) @definition.section
        (setext_heading heading_content: (_) @name) @definition.section
    "#,
    import_query: "",
    call_query: "",
};

const YAML: LanguageSpec = LanguageSpec {
    extensions: &[".yaml", ".yml"],
    symbol_query: r#"
        (block_mapping_pair key: (_) @name) @definition.property
    "#,
    import_query: "",
    call_query: "",
};

const JSON_LANG: LanguageSpec = LanguageSpec {
    extensions: &[".json", ".jsonc"],
    symbol_query: r#"
        (pair key: (string (string_content) @name)) @definition.property
    "#,
    import_query: "",
    call_query: "",
};

// ── Registry ───────────────────────────────────────────────────────────

/// All supported languages and their specs.
const SPECS: &[(&str, &LanguageSpec)] = &[
    ("python", &PYTHON),
    ("javascript", &JAVASCRIPT),
    ("typescript", &TYPESCRIPT),
    ("go", &GO),
    ("rust", &RUST),
    ("java", &JAVA),
    ("php", &PHP),
    ("dart", &DART),
    ("csharp", &CSHARP),
    ("c", &C_LANG),
    ("cpp", &CPP),
    ("elixir", &ELIXIR),
    ("ruby", &RUBY),
    ("markdown", &MARKDOWN),
    ("yaml", &YAML),
    ("json", &JSON_LANG),
];

/// Detect language name from file extension.
pub fn detect_language(file_path: &str) -> Option<&'static str> {
    let path = std::path::Path::new(file_path);
    let ext = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))?;

    for (name, spec) in SPECS {
        if spec.extensions.contains(&ext.as_str()) {
            return Some(name);
        }
    }
    None
}

/// Get the language spec for a given language name.
pub fn get_spec(lang: &str) -> Option<&'static LanguageSpec> {
    SPECS
        .iter()
        .find(|(name, _)| *name == lang)
        .map(|(_, s)| *s)
}

/// Get the tree-sitter Language object for a given language name.
pub fn get_ts_language(lang: &str) -> Option<Language> {
    let lang_fn = match lang {
        "python" => tree_sitter_python::LANGUAGE,
        "javascript" => tree_sitter_javascript::LANGUAGE,
        "typescript" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT,
        "go" => tree_sitter_go::LANGUAGE,
        "rust" => tree_sitter_rust::LANGUAGE,
        "java" => tree_sitter_java::LANGUAGE,
        "c" => tree_sitter_c::LANGUAGE,
        "cpp" => tree_sitter_cpp::LANGUAGE,
        "csharp" => tree_sitter_c_sharp::LANGUAGE,
        "ruby" => tree_sitter_ruby::LANGUAGE,
        "php" => tree_sitter_php::LANGUAGE_PHP,
        "swift" => tree_sitter_swift::LANGUAGE,
        // kotlin 0.3 is built against tree-sitter 0.20, incompatible with 0.24
        // TODO: update when tree-sitter-kotlin publishes a 0.24-compatible release
        "kotlin" => return None,
        "dart" => tree_sitter_dart::LANGUAGE,
        "elixir" => tree_sitter_elixir::LANGUAGE,
        "json" => tree_sitter_json::LANGUAGE,
        "yaml" => tree_sitter_yaml::LANGUAGE,
        "markdown" => tree_sitter_md::LANGUAGE,
        _ => return None,
    };
    Some(lang_fn.into())
}

/// Set of all file extensions supported for AST parsing.
pub fn supported_extensions() -> HashSet<&'static str> {
    let mut exts = HashSet::new();
    for (_, spec) in SPECS {
        for ext in spec.extensions {
            exts.insert(*ext);
        }
    }
    exts
}
