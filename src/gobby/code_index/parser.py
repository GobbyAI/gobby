"""Tree-sitter AST parser for code symbol extraction.

Parses source files into Symbol, ImportRelation, and CallRelation objects
using tree-sitter queries defined in the language registry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    from tree_sitter import Query, QueryCursor
except ImportError:
    Query = None  # type: ignore[assignment,misc]
    QueryCursor = None  # type: ignore[assignment,misc]

from gobby.code_index.hasher import file_content_hash, symbol_content_hash
from gobby.code_index.languages import (
    LANGUAGE_SPECS,
    detect_language,
    get_language_obj,
    get_parser_for_language,
)
from gobby.code_index.models import (
    CallRelation,
    ImportRelation,
    ParseResult,
    Symbol,
)
from gobby.code_index.security import (
    has_secret_extension,
    is_binary,
    is_symlink_safe,
    should_exclude,
    validate_path,
)
from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)


class CodeParser:
    """Parses source files into symbols using tree-sitter."""

    def __init__(self, config: CodeIndexConfig) -> None:
        if Query is None:
            raise ImportError(
                "tree-sitter is required for code indexing. "
                "Install it with: uv pip install tree-sitter-language-pack"
            )
        self.config = config
        self._supported_languages = set(config.languages)

    def parse_file(
        self,
        file_path: str,
        project_id: str,
        root_path: str,
    ) -> ParseResult | None:
        """Parse a single file into symbols, imports, and calls.

        Returns None if file should be skipped (binary, excluded, etc).
        """
        path = Path(file_path)
        root = Path(root_path)

        # Security checks
        if not validate_path(path, root):
            return None
        if not is_symlink_safe(path, root):
            return None
        if should_exclude(path, self.config.exclude_patterns):
            return None
        if has_secret_extension(path):
            return None

        # Size check
        try:
            size = path.stat().st_size
            if size > self.config.max_file_size_bytes or size == 0:
                return None
        except OSError:
            return None

        # Binary check
        if is_binary(path):
            return None

        # Language detection
        language = detect_language(file_path)
        if language is None or language not in self._supported_languages:
            return None

        spec = LANGUAGE_SPECS.get(language)
        if spec is None:
            return None

        # Get parser
        parser = get_parser_for_language(language)
        if parser is None:
            return None

        # Read and parse
        try:
            source = path.read_bytes()
        except OSError as e:
            logger.debug(f"Cannot read {file_path}: {e}")
            return None

        try:
            tree = parser.parse(source)
        except Exception as e:
            logger.warning(f"Parse error for {file_path}: {e}")
            return None

        # Make path relative to root for storage
        try:
            rel_path = str(path.resolve().relative_to(root.resolve()))
        except ValueError:
            rel_path = str(path)

        # Extract symbols
        symbols = self._extract_symbols(tree, source, spec, language, project_id, rel_path)

        # Extract imports
        imports = self._extract_imports(tree, source, spec, rel_path)

        # Extract calls
        calls = self._extract_calls(tree, source, spec, language, rel_path, symbols)

        return ParseResult(symbols=symbols, imports=imports, calls=calls)

    def _extract_symbols(
        self,
        tree: Any,
        source: bytes,
        spec: object,
        language: str,
        project_id: str,
        rel_path: str,
    ) -> list[Symbol]:
        """Extract symbols from parsed tree using language-specific queries."""
        if not hasattr(spec, "symbol_query") or not spec.symbol_query.strip():
            return []

        lang_obj = get_language_obj(language)
        if lang_obj is None:
            return []

        try:
            query = Query(lang_obj, spec.symbol_query)
        except Exception as e:
            logger.debug(f"Query compilation failed for {language}: {e}")
            return []

        root_node = tree.root_node
        cursor = QueryCursor(query)
        matches = cursor.matches(root_node)

        symbols: list[Symbol] = []
        seen_ids: set[str] = set()

        for _, captures in matches:
            # Find the name capture and the definition capture
            name_node = None
            def_node = None
            kind = "function"

            for cap_name, nodes in captures.items():
                node = nodes[0] if isinstance(nodes, list) else nodes
                if cap_name == "name":
                    name_node = node
                elif cap_name.startswith("definition."):
                    def_node = node
                    kind = cap_name.split(".", 1)[1]

            if name_node is None or def_node is None:
                continue

            name = source[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )

            # Build qualified name (will be updated for nested symbols below)
            qualified_name = name

            # Extract signature (first line of the definition)
            start_line_bytes = source[
                def_node.start_byte : source.find(b"\n", def_node.start_byte)
                if source.find(b"\n", def_node.start_byte) != -1
                else def_node.end_byte
            ]
            signature = start_line_bytes.decode("utf-8", errors="replace").strip()
            if len(signature) > 200:
                signature = signature[:200] + "..."

            # Extract docstring (look for string as first child of body)
            docstring = self._extract_docstring(def_node, source, language)

            # Compute content hash
            c_hash = symbol_content_hash(source, def_node.start_byte, def_node.end_byte)

            symbol_id = Symbol.make_id(project_id, rel_path, name, kind, def_node.start_byte)

            if symbol_id in seen_ids:
                continue
            seen_ids.add(symbol_id)

            symbols.append(
                Symbol(
                    id=symbol_id,
                    project_id=project_id,
                    file_path=rel_path,
                    name=name,
                    qualified_name=qualified_name,
                    kind=kind,
                    language=language,
                    byte_start=def_node.start_byte,
                    byte_end=def_node.end_byte,
                    line_start=def_node.start_point[0] + 1,
                    line_end=def_node.end_point[0] + 1,
                    signature=signature,
                    docstring=docstring,
                    content_hash=c_hash,
                )
            )

        # Link parent symbols (methods inside classes)
        self._link_parents(symbols, spec)

        return symbols

    def _link_parents(self, symbols: list[Symbol], spec: object) -> None:
        """Set parent_symbol_id and qualified_name for nested symbols."""
        # Sort by byte_start so parents come before children
        sorted_syms = sorted(symbols, key=lambda s: s.byte_start)

        # Build a stack of potential parents
        for i, sym in enumerate(sorted_syms):
            # Look for the nearest enclosing class/container
            for j in range(i - 1, -1, -1):
                parent = sorted_syms[j]
                if parent.kind in ("class", "type") and (
                    parent.byte_start <= sym.byte_start and parent.byte_end >= sym.byte_end
                ):
                    sym.parent_symbol_id = parent.id
                    sym.qualified_name = f"{parent.name}.{sym.name}"
                    if sym.kind == "function":
                        sym.kind = "method"
                    break

    def _extract_docstring(self, node: Any, source: bytes, language: str) -> str | None:
        """Try to extract a docstring from a definition node."""
        if language not in ("python", "javascript", "typescript"):
            return None

        # For Python: look for expression_statement > string as first child of body
        body = None
        for child in node.children:
            if hasattr(child, "type") and child.type in ("block", "statement_block"):
                body = child
                break

        if body is None:
            return None

        for child in body.children:
            if not hasattr(child, "type"):
                continue
            # Skip comments, newlines
            if child.type in ("comment", "\n", "newline"):
                continue

            # Find the string node — may be direct child or inside expression_statement
            string_node = None
            if child.type == "string":
                string_node = child
            elif child.type == "expression_statement":
                for grandchild in child.children:
                    if hasattr(grandchild, "type") and grandchild.type == "string":
                        string_node = grandchild
                        break

            if string_node is None:
                break

            # Extract text — try string_content child first (newer tree-sitter),
            # fall back to stripping quotes from full text
            for sc in string_node.children:
                if hasattr(sc, "type") and sc.type == "string_content":
                    raw = source[sc.start_byte : sc.end_byte].decode("utf-8", errors="replace")
                    return raw.strip() if raw.strip() else None

            raw = source[string_node.start_byte : string_node.end_byte].decode(
                "utf-8", errors="replace"
            )
            for q in ('"""', "'''", '"', "'"):
                if raw.startswith(q) and raw.endswith(q):
                    raw = raw[len(q) : -len(q)]
                    break
            return raw.strip() if raw.strip() else None

        return None

    def _extract_imports(
        self,
        tree: Any,
        source: bytes,
        spec: object,
        rel_path: str,
    ) -> list[ImportRelation]:
        """Extract import statements."""
        if not hasattr(spec, "import_query") or not spec.import_query.strip():
            return []

        lang_name = None
        for lname, lspec in LANGUAGE_SPECS.items():
            if lspec is spec:
                lang_name = lname
                break
        if lang_name is None:
            return []

        lang_obj = get_language_obj(lang_name)
        if lang_obj is None:
            return []

        try:
            query = Query(lang_obj, spec.import_query)
        except Exception:
            return []

        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)
        imports: list[ImportRelation] = []

        for _, captures in matches:
            for _cap_name, nodes in captures.items():
                node = nodes[0] if isinstance(nodes, list) else nodes
                text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
                # Simple extraction: store the full import text as target_module
                imports.append(
                    ImportRelation(
                        source_file=rel_path,
                        target_module=text.strip(),
                        imported_names=[],
                    )
                )

        return imports

    def _extract_calls(
        self,
        tree: Any,
        source: bytes,
        spec: Any,
        language: str,
        rel_path: str,
        symbols: list[Symbol],
    ) -> list[CallRelation]:
        """Extract function/method calls."""
        if not hasattr(spec, "call_query") or not spec.call_query.strip():
            return []

        lang_obj = get_language_obj(language)
        if lang_obj is None:
            return []

        try:
            query = Query(lang_obj, spec.call_query)
        except Exception:
            return []

        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)
        calls: list[CallRelation] = []

        for _, captures in matches:
            name_node = None
            call_node = None
            for cap_name, nodes in captures.items():
                node = nodes[0] if isinstance(nodes, list) else nodes
                if cap_name == "name":
                    name_node = node
                elif cap_name == "call":
                    call_node = node

            if name_node is None:
                continue

            callee_name = source[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )

            # Find enclosing symbol (caller)
            target_node = call_node or name_node
            caller_id = ""
            for sym in symbols:
                if sym.byte_start <= target_node.start_byte <= sym.byte_end:
                    caller_id = sym.id

            calls.append(
                CallRelation(
                    caller_symbol_id=caller_id,
                    callee_name=callee_name,
                    file_path=rel_path,
                    line=name_node.start_point[0] + 1,
                )
            )

        return calls

    def get_file_hash(self, file_path: str) -> str | None:
        """Get content hash for a file, or None if unreadable."""
        try:
            return file_content_hash(file_path)
        except OSError:
            return None
