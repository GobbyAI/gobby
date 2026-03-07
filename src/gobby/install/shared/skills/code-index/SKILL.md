---
name: code-index
description: Instructions for using gobby-code symbol-level search and retrieval. Auto-injected when project has a code index.
category: core
alwaysApply: true
injectionFormat: full
metadata:
  gobby:
    audience: all
---

# Code Index

This project is indexed in `gobby-code`. Before reading large files, prefer symbol-level retrieval:

- `get_file_outline(file_path)` — hierarchical symbol map (much cheaper than Read)
- `search_symbols(query)` — find symbols by name or description
- `get_symbol(symbol_id)` — retrieve just the source you need (O(1) via byte offsets)

Discover the full tool list: `list_tools(server_name="gobby-code")`

This saves 90%+ tokens vs reading entire files.
