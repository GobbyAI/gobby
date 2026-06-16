//! Language registry with tree-sitter query definitions.
//! Ports 16 language specs from src/gobby/code_index/languages.py.

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
    extensions: &[".js", ".jsx", ".cjs", ".mjs"],
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
        (object_creation_expression type: (type_identifier) @name) @call
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
        (function_call_expression function: (qualified_name) @name) @call
        (scoped_call_expression scope: [(name) (qualified_name)] name: (name) @name) @call
        (member_call_expression name: (name) @name) @call
        (object_creation_expression [(name) (qualified_name)] @name) @call
    "#,
};

const DART: LanguageSpec = LanguageSpec {
    extensions: &[".dart"],
    symbol_query: r#"
        (function_signature name: (identifier) @name) @definition.function
        (class_declaration name: (identifier) @name) @definition.class
        (method_signature (function_signature (identifier) @name)) @definition.method
        (enum_declaration name: (identifier) @name) @definition.type
    "#,
    import_query: r#"
        (import_or_export) @import
    "#,
    // Dart calls are extracted by parser.rs because this grammar models calls as
    // selector chains rather than a stable call-expression node.
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
    extensions: &[".c"],
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

const OBJC: LanguageSpec = LanguageSpec {
    // Headers are extension-ambiguous, but Objective-C's grammar inherits the C
    // grammar and exposes @interface declarations that plain C cannot see.
    // Obj-C++ `.mm` also uses this grammar so message sends are indexed.
    extensions: &[".m", ".mm", ".h"],
    symbol_query: r#"
        (class_interface "@interface" . (identifier) @name) @definition.class
        (class_implementation "@implementation" . (identifier) @name) @definition.class
        (protocol_declaration "@protocol" . (identifier) @name) @definition.type
        (method_declaration (identifier) @name) @definition.method
        (method_declaration (method_identifier (identifier) @name)) @definition.method
        (method_definition (identifier) @name) @definition.method
        (method_definition (method_identifier (identifier) @name)) @definition.method
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @definition.function
        (declaration declarator: (function_declarator declarator: (identifier) @name)) @definition.function
        (struct_specifier name: (type_identifier) @name) @definition.type
        (enum_specifier name: (type_identifier) @name) @definition.type
        (type_definition declarator: (type_identifier) @name) @definition.type
    "#,
    import_query: r#"
        (preproc_include) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (message_expression receiver: (_) @receiver method: (identifier) @name) @call
    "#,
};

const ELIXIR: LanguageSpec = LanguageSpec {
    extensions: &[".ex", ".exs"],
    symbol_query: r#"
        (call
            target: (identifier) @_keyword
            (#any-of? @_keyword "def" "defp" "defmacro")
            (arguments [
                (identifier) @name
                (call target: (identifier) @name)
                (binary_operator
                    left: (call target: (identifier) @name)
                    operator: "when")
            ])) @definition.function
        (call target: (identifier) @_keyword (#any-of? @_keyword "defmodule") (arguments (alias) @name)) @definition.class
    "#,
    import_query: r#"
        (call target: (identifier) @_keyword (#any-of? @_keyword "import" "alias" "use" "require")) @import
    "#,
    call_query: r#"
        (call target: (identifier) @name) @call
        (call target: (dot right: (identifier) @name)) @call
    "#,
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
        (call method: (identifier) @_m (#any-of? @_m "require" "require_relative" "load" "include" "extend" "prepend")) @import
    "#,
    call_query: r#"
        (call method: (identifier) @name) @call
    "#,
};

const KOTLIN: LanguageSpec = LanguageSpec {
    extensions: &[".kt", ".kts"],
    symbol_query: r#"
        (function_declaration name: (identifier) @name) @definition.function
        (class_declaration name: (identifier) @name) @definition.class
        (object_declaration name: (identifier) @name) @definition.class
    "#,
    import_query: r#"
        (import) @import
    "#,
    call_query: r#"
        (call_expression (identifier) @name) @call
        (call_expression (navigation_expression (identifier) (identifier) @name)) @call
    "#,
};

const SCALA: LanguageSpec = LanguageSpec {
    extensions: &[".scala", ".sc"],
    symbol_query: r#"
        (class_definition name: [(identifier) (operator_identifier)] @name) @definition.class
        (object_definition name: [(identifier) (operator_identifier)] @name) @definition.class
        (trait_definition name: [(identifier) (operator_identifier)] @name) @definition.type
        (function_definition name: [(identifier) (operator_identifier)] @name) @definition.function
    "#,
    import_query: r#"
        (import_declaration) @import
    "#,
    call_query: r#"
        (call_expression function: (identifier) @name) @call
        (call_expression function: (field_expression field: (identifier) @name)) @call
        (call_expression function: (generic_function function: (identifier) @name)) @call
        (call_expression function: (generic_function function: (field_expression field: (identifier) @name))) @call
        (instance_expression (type_identifier) @name) @call
        (instance_expression (generic_type type: (type_identifier) @name)) @call
"#,
};

const LUA: LanguageSpec = LanguageSpec {
    extensions: &[".lua"],
    symbol_query: r#"
(function_declaration
  name: [
    (identifier) @name
    (dot_index_expression field: (identifier) @name)
  ]) @definition.function
(function_declaration
  name: (method_index_expression method: (identifier) @name)) @definition.method
(assignment_statement
  (variable_list
    .
    name: [
      (identifier) @name
      (dot_index_expression field: (identifier) @name)
    ])
  (expression_list
    .
    value: (function_definition))) @definition.function
(table_constructor
  (field
    name: (identifier) @name
    value: (function_definition))) @definition.function
"#,
    import_query: r#"
(assignment_statement
  (expression_list
    (function_call name: (identifier) @_require (#eq? @_require "require")))) @import
(assignment_statement
  (expression_list
    (dot_index_expression
      table: (function_call name: (identifier) @_require (#eq? @_require "require"))))) @import
"#,
    call_query: r#"
(function_call
  name: [
    (identifier) @name
    (dot_index_expression field: (identifier) @name)
    (method_index_expression method: (identifier) @name)
  ]) @call
"#,
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

const SWIFT: LanguageSpec = LanguageSpec {
    extensions: &[".swift"],
    symbol_query: r#"
        (function_declaration name: (simple_identifier) @name) @definition.function
        (class_declaration declaration_kind: "class" name: (type_identifier) @name) @definition.class
        (class_declaration declaration_kind: "actor" name: (type_identifier) @name) @definition.class
        (protocol_declaration name: (type_identifier) @name) @definition.type
        (class_declaration declaration_kind: "struct" name: (type_identifier) @name) @definition.type
        (class_declaration declaration_kind: "enum" name: (type_identifier) @name) @definition.type
    "#,
    import_query: r#"
        (import_declaration) @import
    "#,
    call_query: r#"
        (call_expression (simple_identifier) @name) @call
        (call_expression (navigation_expression suffix: (navigation_suffix suffix: (simple_identifier) @name))) @call
    "#,
};

const BASH: LanguageSpec = LanguageSpec {
    extensions: &[".sh", ".bash"],
    symbol_query: r#"
        (function_definition name: (word) @name) @definition.function
    "#,
    import_query: r#"
        (command
            name: (command_name) @_cmd
            (#any-of? @_cmd "source" ".")) @import
    "#,
    call_query: r#"
        (command name: (command_name) @name) @call
    "#,
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
    ("objc", &OBJC),
    ("c", &C_LANG),
    ("cpp", &CPP),
    ("elixir", &ELIXIR),
    ("ruby", &RUBY),
    ("kotlin", &KOTLIN),
    ("scala", &SCALA),
    ("lua", &LUA),
    ("swift", &SWIFT),
    ("bash", &BASH),
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

/// Returns `true` for "data" languages: specs whose `import_query` and
/// `call_query` are both empty (JSON and YAML).
///
/// Such files carry no import/call-graph structure, only per-key `property`
/// symbols, so an oversized one can be indexed content-only without losing any
/// graph signal. Driven by the spec rather than a hard-coded extension list, so
/// future data languages are covered automatically (gobby-cli #678).
pub fn is_data_language(lang: &str) -> bool {
    get_spec(lang)
        .map(|spec| spec.import_query.is_empty() && spec.call_query.is_empty())
        .unwrap_or(false)
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
        "objc" => tree_sitter_objc::LANGUAGE,
        "c" => tree_sitter_c::LANGUAGE,
        "cpp" => tree_sitter_cpp::LANGUAGE,
        "csharp" => tree_sitter_c_sharp::LANGUAGE,
        "ruby" => tree_sitter_ruby::LANGUAGE,
        "php" => tree_sitter_php::LANGUAGE_PHP,
        "swift" => tree_sitter_swift::LANGUAGE,
        "kotlin" => tree_sitter_kotlin_ng::LANGUAGE,
        "scala" => tree_sitter_scala::LANGUAGE,
        "lua" => tree_sitter_lua::LANGUAGE,
        "dart" => tree_sitter_dart::LANGUAGE,
        "elixir" => tree_sitter_elixir::LANGUAGE,
        "bash" => tree_sitter_bash::LANGUAGE,
        "json" => tree_sitter_json::LANGUAGE,
        "yaml" => tree_sitter_yaml::LANGUAGE,
        _ => return None,
    };
    Some(lang_fn.into())
}

/// Get the parser grammar for a language and concrete file path.
pub fn get_ts_language_for_path(lang: &str, file_path: &str) -> Option<Language> {
    if lang == "typescript"
        && std::path::Path::new(file_path)
            .extension()
            .map(|ext| ext.to_string_lossy().eq_ignore_ascii_case("tsx"))
            .unwrap_or(false)
    {
        return Some(tree_sitter_typescript::LANGUAGE_TSX.into());
    }

    get_ts_language(lang)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markdown_extensions_are_not_detected() {
        // Markdown is intentionally handled as content-only text, not AST.
        assert_eq!(detect_language("README.md"), None);
        assert_eq!(detect_language("docs/guide.markdown"), None);
    }

    #[test]
    fn javascript_extensions_still_detect() {
        assert_eq!(detect_language("src/app.js"), Some("javascript"));
        assert_eq!(detect_language("src/app.jsx"), Some("javascript"));
        assert_eq!(detect_language("src/app.cjs"), Some("javascript"));
        assert_eq!(detect_language("src/generated.mjs"), Some("javascript"));
    }

    #[test]
    fn typescript_extensions_still_detect() {
        assert_eq!(detect_language("src/app.ts"), Some("typescript"));
        assert_eq!(detect_language("src/app.tsx"), Some("typescript"));
    }

    #[test]
    fn bash_extensions_detect() {
        assert_eq!(detect_language("scripts/deploy.sh"), Some("bash"));
        assert_eq!(detect_language("scripts/env.bash"), Some("bash"));
    }

    #[test]
    fn scala_extensions_detect() {
        assert_eq!(detect_language("src/main/scala/App.scala"), Some("scala"));
        assert_eq!(detect_language("scripts/build.sc"), Some("scala"));
    }

    #[test]
    fn lua_extensions_detect() {
        assert_eq!(detect_language("lua/app/init.lua"), Some("lua"));
    }

    #[test]
    fn objc_extensions_detect() {
        assert_eq!(detect_language("Sources/App/Widget.m"), Some("objc"));
        assert_eq!(detect_language("Sources/App/Widget.mm"), Some("objc"));
        assert_eq!(detect_language("Sources/App/Widget.h"), Some("objc"));
    }

    #[test]
    fn objcxx_paths_use_objc_grammar() {
        let language = get_ts_language_for_path("objc", "Sources/App/Widget.mm").unwrap();
        assert!(parses_without_error(
            language,
            r#"
@interface Widget
- (void)render;
@end

@implementation Widget
- (void)render { helper(); }
@end
"#,
        ));
    }

    #[test]
    fn tsx_paths_use_tsx_grammar() {
        let language = get_ts_language_for_path("typescript", "src/app.tsx").unwrap();
        assert!(parses_without_error(
            language,
            "export const View = () => <section data-id=\"x\" />;",
        ));
    }

    #[test]
    fn ts_paths_keep_typescript_grammar() {
        let language = get_ts_language_for_path("typescript", "src/app.ts").unwrap();
        assert!(parses_with_error(
            language,
            "export const View = () => <section />;"
        ));
    }

    fn parses_without_error(language: Language, source: &str) -> bool {
        let mut parser = tree_sitter::Parser::new();
        parser.set_language(&language).unwrap();
        let tree = parser.parse(source, None).unwrap();
        !tree.root_node().has_error()
    }

    fn parses_with_error(language: Language, source: &str) -> bool {
        let mut parser = tree_sitter::Parser::new();
        parser.set_language(&language).unwrap();
        let tree = parser.parse(source, None).unwrap();
        tree.root_node().has_error()
    }

    #[test]
    fn is_data_language_matches_only_json_and_yaml() {
        assert!(is_data_language("json"));
        assert!(is_data_language("yaml"));
        // AST languages carry import/call queries and must not be treated as data.
        assert!(!is_data_language("rust"));
        assert!(!is_data_language("python"));
        // Dart has an empty call_query but a non-empty import_query, so it is excluded.
        assert!(!is_data_language("dart"));
        // Unknown languages are not data languages.
        assert!(!is_data_language("not_a_language"));
    }
}
