pub(crate) const DEFAULT_SCHEMA: &str = "public";
pub(super) const NAMESPACE: &str = "gcode";
pub(super) const OVERWRITE_GUIDANCE: &str = "Rerun with `gcode setup --standalone --overwrite-code-index` to replace only gcode-owned code-index relations.";

pub(crate) struct TableContract {
    pub(crate) name: &'static str,
    pub(crate) required_columns: &'static [&'static str],
}

pub(super) struct IndexContract {
    pub(super) name: &'static str,
    pub(super) table: &'static str,
    pub(super) method: &'static str,
}

pub(crate) const TABLE_CONTRACTS: &[TableContract] = &[
    TableContract {
        name: "code_indexed_projects",
        required_columns: &[
            "id",
            "root_path",
            "total_files",
            "total_symbols",
            "last_indexed_at",
            "index_duration_ms",
            "created_at",
            "updated_at",
        ],
    },
    TableContract {
        name: "code_indexed_files",
        required_columns: &[
            "id",
            "project_id",
            "file_path",
            "language",
            "content_hash",
            "symbol_count",
            "byte_size",
            "graph_synced",
            "vectors_synced",
            "graph_sync_attempted_at",
            "vector_sync_attempted_at",
            "indexed_at",
        ],
    },
    TableContract {
        name: "code_symbols",
        required_columns: &[
            "id",
            "project_id",
            "file_path",
            "name",
            "qualified_name",
            "kind",
            "language",
            "byte_start",
            "byte_end",
            "line_start",
            "line_end",
            "signature",
            "docstring",
            "parent_symbol_id",
            "content_hash",
            "summary",
            "summary_attempted_at",
            "created_at",
            "updated_at",
        ],
    },
    TableContract {
        name: "code_content_chunks",
        required_columns: &[
            "id",
            "project_id",
            "file_path",
            "chunk_index",
            "line_start",
            "line_end",
            "content",
            "language",
            "created_at",
        ],
    },
    TableContract {
        name: "code_imports",
        required_columns: &["id", "project_id", "source_file", "target_module"],
    },
    TableContract {
        name: "code_calls",
        required_columns: &[
            "id",
            "project_id",
            "caller_symbol_id",
            "callee_symbol_id",
            "callee_name",
            "callee_target_kind",
            "callee_external_module",
            "file_path",
            "line",
        ],
    },
];

pub(super) const INDEX_CONTRACTS: &[IndexContract] = &[
    IndexContract {
        name: "idx_cif_project",
        table: "code_indexed_files",
        method: "btree",
    },
    IndexContract {
        name: "idx_cif_graph_synced",
        table: "code_indexed_files",
        method: "btree",
    },
    IndexContract {
        name: "idx_cif_vectors_synced",
        table: "code_indexed_files",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_project",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_file",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_name",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_qualified",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_kind",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_cs_parent",
        table: "code_symbols",
        method: "btree",
    },
    IndexContract {
        name: "idx_ccc_project",
        table: "code_content_chunks",
        method: "btree",
    },
    IndexContract {
        name: "idx_ccc_file",
        table: "code_content_chunks",
        method: "btree",
    },
    IndexContract {
        name: "idx_ci_file",
        table: "code_imports",
        method: "btree",
    },
    IndexContract {
        name: "idx_cc_file",
        table: "code_calls",
        method: "btree",
    },
    IndexContract {
        name: "idx_cc_caller",
        table: "code_calls",
        method: "btree",
    },
    IndexContract {
        name: "idx_cc_target",
        table: "code_calls",
        method: "btree",
    },
    IndexContract {
        name: "code_symbols_search_bm25",
        table: "code_symbols",
        method: "bm25",
    },
    IndexContract {
        name: "code_content_search_bm25",
        table: "code_content_chunks",
        method: "bm25",
    },
];

pub(super) fn code_index_table_names() -> impl DoubleEndedIterator<Item = &'static str> {
    TABLE_CONTRACTS.iter().map(|contract| contract.name)
}

pub(super) fn code_index_index_names() -> impl DoubleEndedIterator<Item = &'static str> {
    INDEX_CONTRACTS.iter().map(|contract| contract.name)
}
