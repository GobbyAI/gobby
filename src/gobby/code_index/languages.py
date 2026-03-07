"""Language registry mapping languages to tree-sitter queries.

Each LanguageSpec defines file extensions, and tree-sitter query strings
for extracting symbols, imports, and calls from a given language.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# Cached parsers (tree-sitter Parser objects are reusable and thread-safe)
_parser_cache: dict[str, Any] = {}


@dataclass
class LanguageSpec:
    """Specification for a single language's tree-sitter queries."""

    extensions: list[str]
    symbol_query: str
    import_query: str = ""
    call_query: str = ""
    docstring_query: str = ""
    # Node types that indicate a class-like container (for parent linking)
    container_types: list[str] = field(default_factory=list)


# ── Query Definitions ────────────────────────────────────────────────────

_PYTHON_SPEC = LanguageSpec(
    extensions=[".py", ".pyi"],
    symbol_query="""
        (function_definition name: (identifier) @name) @definition.function
        (class_definition name: (identifier) @name) @definition.class
    """,
    import_query="""
        (import_statement) @import
        (import_from_statement) @import
    """,
    call_query="""
        (call function: (identifier) @name) @call
        (call function: (attribute attribute: (identifier) @name)) @call
    """,
    container_types=["class_definition"],
)

_JAVASCRIPT_SPEC = LanguageSpec(
    extensions=[".js", ".jsx", ".mjs", ".cjs"],
    symbol_query="""
        (function_declaration name: (identifier) @name) @definition.function
        (class_declaration name: (identifier) @name) @definition.class
        (method_definition name: (property_identifier) @name) @definition.method
        (export_statement declaration: (function_declaration name: (identifier) @name)) @definition.function
        (export_statement declaration: (class_declaration name: (identifier) @name)) @definition.class
        (lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function))) @definition.function
    """,
    import_query="""
        (import_statement) @import
    """,
    call_query="""
        (call_expression function: (identifier) @name) @call
        (call_expression function: (member_expression property: (property_identifier) @name)) @call
    """,
    container_types=["class_declaration", "class"],
)

_TYPESCRIPT_SPEC = LanguageSpec(
    extensions=[".ts", ".tsx"],
    symbol_query="""
        (function_declaration name: (identifier) @name) @definition.function
        (class_declaration name: (type_identifier) @name) @definition.class
        (method_definition name: (property_identifier) @name) @definition.method
        (interface_declaration name: (type_identifier) @name) @definition.type
        (type_alias_declaration name: (type_identifier) @name) @definition.type
        (enum_declaration name: (identifier) @name) @definition.type
        (lexical_declaration (variable_declarator name: (identifier) @name value: (arrow_function))) @definition.function
    """,
    import_query="""
        (import_statement) @import
    """,
    call_query="""
        (call_expression function: (identifier) @name) @call
        (call_expression function: (member_expression property: (property_identifier) @name)) @call
    """,
    container_types=["class_declaration", "interface_declaration"],
)

_GO_SPEC = LanguageSpec(
    extensions=[".go"],
    symbol_query="""
        (function_declaration name: (identifier) @name) @definition.function
        (method_declaration name: (field_identifier) @name) @definition.method
        (type_declaration (type_spec name: (type_identifier) @name)) @definition.type
    """,
    import_query="""
        (import_declaration) @import
    """,
    call_query="""
        (call_expression function: (identifier) @name) @call
        (call_expression function: (selector_expression field: (field_identifier) @name)) @call
    """,
    container_types=[],
)

_RUST_SPEC = LanguageSpec(
    extensions=[".rs"],
    symbol_query="""
        (function_item name: (identifier) @name) @definition.function
        (struct_item name: (type_identifier) @name) @definition.class
        (enum_item name: (type_identifier) @name) @definition.type
        (trait_item name: (type_identifier) @name) @definition.type
        (impl_item type: (type_identifier) @name) @definition.class
        (type_item name: (type_identifier) @name) @definition.type
    """,
    import_query="""
        (use_declaration) @import
    """,
    call_query="""
        (call_expression function: (identifier) @name) @call
        (call_expression function: (field_expression field: (field_identifier) @name)) @call
    """,
    container_types=["impl_item"],
)

_JAVA_SPEC = LanguageSpec(
    extensions=[".java"],
    symbol_query="""
        (method_declaration name: (identifier) @name) @definition.method
        (class_declaration name: (identifier) @name) @definition.class
        (interface_declaration name: (identifier) @name) @definition.type
        (enum_declaration name: (identifier) @name) @definition.type
        (constructor_declaration name: (identifier) @name) @definition.method
    """,
    import_query="""
        (import_declaration) @import
    """,
    call_query="""
        (method_invocation name: (identifier) @name) @call
    """,
    container_types=["class_declaration", "interface_declaration", "enum_declaration"],
)

_PHP_SPEC = LanguageSpec(
    extensions=[".php"],
    symbol_query="""
        (function_definition name: (name) @name) @definition.function
        (class_declaration name: (name) @name) @definition.class
        (method_declaration name: (name) @name) @definition.method
        (interface_declaration name: (name) @name) @definition.type
        (trait_declaration name: (name) @name) @definition.type
    """,
    import_query="""
        (namespace_use_declaration) @import
    """,
    call_query="""
        (function_call_expression function: (name) @name) @call
        (member_call_expression name: (name) @name) @call
    """,
    container_types=["class_declaration", "interface_declaration", "trait_declaration"],
)

_DART_SPEC = LanguageSpec(
    extensions=[".dart"],
    symbol_query="""
        (function_signature name: (identifier) @name) @definition.function
        (class_definition name: (identifier) @name) @definition.class
        (method_signature (function_signature (identifier) @name)) @definition.method
        (enum_declaration name: (identifier) @name) @definition.type
    """,
    import_query="""
        (import_or_export) @import
    """,
    call_query="",
    container_types=["class_definition"],
)

_CSHARP_SPEC = LanguageSpec(
    extensions=[".cs"],
    symbol_query="""
        (method_declaration name: (identifier) @name) @definition.method
        (class_declaration name: (identifier) @name) @definition.class
        (interface_declaration name: (identifier) @name) @definition.type
        (struct_declaration name: (identifier) @name) @definition.type
        (enum_declaration name: (identifier) @name) @definition.type
        (constructor_declaration name: (identifier) @name) @definition.method
    """,
    import_query="""
        (using_directive) @import
    """,
    call_query="""
        (invocation_expression function: (identifier) @name) @call
        (invocation_expression function: (member_access_expression name: (identifier) @name)) @call
    """,
    container_types=["class_declaration", "interface_declaration", "struct_declaration"],
)

_C_SPEC = LanguageSpec(
    extensions=[".c", ".h"],
    symbol_query="""
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @definition.function
        (struct_specifier name: (type_identifier) @name) @definition.type
        (enum_specifier name: (type_identifier) @name) @definition.type
        (type_definition declarator: (type_identifier) @name) @definition.type
    """,
    import_query="""
        (preproc_include) @import
    """,
    call_query="""
        (call_expression function: (identifier) @name) @call
    """,
    container_types=[],
)

_CPP_SPEC = LanguageSpec(
    extensions=[".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".hh"],
    symbol_query="""
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @definition.function
        (function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @name))) @definition.function
        (class_specifier name: (type_identifier) @name) @definition.class
        (struct_specifier name: (type_identifier) @name) @definition.type
    """,
    import_query="""
        (preproc_include) @import
    """,
    call_query="""
        (call_expression function: (identifier) @name) @call
        (call_expression function: (field_expression field: (field_identifier) @name)) @call
    """,
    container_types=["class_specifier"],
)

_ELIXIR_SPEC = LanguageSpec(
    extensions=[".ex", ".exs"],
    symbol_query="""
        (call target: (identifier) @_keyword (#any-of? @_keyword "def" "defp" "defmacro") (arguments (identifier) @name)) @definition.function
        (call target: (identifier) @_keyword (#any-of? @_keyword "defmodule") (arguments (alias) @name)) @definition.class
    """,
    import_query="""
        (call target: (identifier) @_keyword (#any-of? @_keyword "import" "alias" "use" "require")) @import
    """,
    call_query="",
    container_types=[],
)

_RUBY_SPEC = LanguageSpec(
    extensions=[".rb", ".rake", ".gemspec"],
    symbol_query="""
        (method name: (identifier) @name) @definition.function
        (singleton_method name: (identifier) @name) @definition.function
        (class name: (constant) @name) @definition.class
        (module name: (constant) @name) @definition.class
    """,
    import_query="""
        (call method: (identifier) @_m (#any-of? @_m "require" "require_relative" "include")) @import
    """,
    call_query="""
        (call method: (identifier) @name) @call
    """,
    container_types=["class", "module"],
)


# ── Registry ─────────────────────────────────────────────────────────────

LANGUAGE_SPECS: dict[str, LanguageSpec] = {
    "python": _PYTHON_SPEC,
    "javascript": _JAVASCRIPT_SPEC,
    "typescript": _TYPESCRIPT_SPEC,
    "go": _GO_SPEC,
    "rust": _RUST_SPEC,
    "java": _JAVA_SPEC,
    "php": _PHP_SPEC,
    "dart": _DART_SPEC,
    "csharp": _CSHARP_SPEC,
    "c": _C_SPEC,
    "cpp": _CPP_SPEC,
    "elixir": _ELIXIR_SPEC,
    "ruby": _RUBY_SPEC,
}


_EXTENSIONS_MAP: dict[str, str] | None = None


def get_extensions_map() -> dict[str, str]:
    """Return cached map from file extension to language name."""
    global _EXTENSIONS_MAP
    if _EXTENSIONS_MAP is None:
        ext_map: dict[str, str] = {}
        for lang_name, spec in LANGUAGE_SPECS.items():
            for ext in spec.extensions:
                ext_map[ext] = lang_name
        _EXTENSIONS_MAP = ext_map
    return _EXTENSIONS_MAP


def detect_language(file_path: str) -> str | None:
    """Detect language from file extension."""
    suffix = Path(file_path).suffix.lower()
    return get_extensions_map().get(suffix)


def get_parser_for_language(lang: str) -> Any:
    """Get a cached tree-sitter Parser for a language.

    Returns None if tree-sitter-language-pack is not installed
    or the language is not supported.
    """
    if lang in _parser_cache:
        return _parser_cache[lang]

    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(cast(Any, lang))
        _parser_cache[lang] = parser
        return parser
    except ImportError:
        logger.warning("tree-sitter-language-pack not installed")
        return None
    except Exception as e:
        logger.warning(f"Failed to get parser for {lang}: {e}")
        return None


def get_language_obj(lang: str) -> Any:
    """Get tree-sitter Language object for running queries."""
    try:
        from tree_sitter_language_pack import get_language

        return get_language(cast(Any, lang))
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"Failed to get language object for {lang}: {e}")
        return None
