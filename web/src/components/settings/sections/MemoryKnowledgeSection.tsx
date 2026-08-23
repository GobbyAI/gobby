import { useState } from "react";
import { DetailActionButton, TextField } from "../../activity/fields";
import { TypedListField } from "../fields";
import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import { useSettingsSectionContext } from "./SettingsSectionContext";
import {
  NumberConfigField,
  OptionsSelectField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextConfigField,
} from "./configFields";
import { asString, asTypedList } from "./configAccessors";

/**
 * Memory & Knowledge settings section: the persistent memory store, its
 * knowledge-graph extraction and dreaming maintenance, observational recall,
 * the embedding model, the Qdrant vector store and FalkorDB graph store, the
 * background knowledge-graph queue, memory backup, and the wiki watcher. These
 * are the `memory.*`, `memory_recall.*`, `embeddings.*`, `databases.*`,
 * `knowledge_graph_queue.*`, `memory_backup.*`, and `wiki.*` keep-rows from the
 * configuration audit, with the array "fix" rows (`candidates`,
 * `wiki.ignore_globs`, `wiki.roots`) given typed list editors.
 *
 * Secret credentials (`embeddings.api_key`, `databases.qdrant.api_key`,
 * `databases.falkordb.password`) route to the Secrets & Auth section and are
 * deliberately absent here. Every row is a draft-backed DaemonConfig
 * dotted-path saved through the section shell's Save/Discard footer.
 */

const MEMORY_PATHS = [
  "memory.enabled",
  "memory.backend",
  "memory.auto_crossref",
  "memory.crossref_threshold",
  "memory.crossref_max_links",
  "memory.access_debounce_seconds",
  "memory.code_link_min_score",
  "memory.temporal_decay_half_life_days",
  "memory.min_recall_score",
  "memory.graph_edge_weighting",
  "memory.materialize_cooccurrence",
  "memory.graph_edge_decay",
  "memory.edge_half_life_days",
  "memory.recall_signal_logging",
  "memory.recall_signal_log_path",
  "memory.kg.profile",
  "memory.kg.candidates",
  "memory.dream.profile",
  "memory.dream.candidates",
  "memory.dream.enabled",
  "memory.dream.schedule_cron",
  "memory.dream.prompt_path",
  "memory.dream.max_tokens",
  "memory.dream.max_runtime_seconds",
  "memory.dream.work_unit_timeout_seconds",
  "memory.dream.evidence_channel_timeout_seconds",
  "memory.dream.evidence_retry_attempts",
  "memory.dream.evidence_phase_timeout_seconds",
  "memory.dream.min_action_confidence",
  "memory.dream.min_delete_confidence",
  "memory.dream.include_global_memories",
  "memory.dream.reconcile_after_apply",
  "memory.dream.reconcile_after_revert",
] as const;

const RECALL_PATHS = [
  "memory_recall.profile",
  "memory_recall.candidates",
  "memory_recall.enabled",
  "memory_recall.candidate_limit",
  "memory_recall.selected_limit",
  "memory_recall.min_score",
  "memory_recall.query_synthesis_threshold",
  "memory_recall.query_max_chars",
] as const;

const EMBEDDINGS_PATHS = [
  "ai.embeddings.model",
  "ai.embeddings.dim",
  "ai.embeddings.api_base",
  "ai.embeddings.query_prefix",
  "ai.embeddings.catalog_key",
] as const;

const DATABASE_PATHS = [
  "databases.qdrant.url",
  "databases.qdrant.port",
  "databases.qdrant.collection_prefix",
  "databases.falkordb.host",
  "databases.falkordb.port",
  "databases.falkordb.graph_name",
  "databases.falkordb.graph_search",
  "databases.falkordb.graph_min_score",
  "databases.falkordb.rrf_k",
] as const;

const QUEUE_PATHS = [
  "knowledge_graph_queue.interval_minutes",
  "knowledge_graph_queue.batch_size",
] as const;

const BACKUP_PATHS = [
  "memory_backup.enabled",
  "memory_backup.backup_path",
] as const;

const WIKI_PATHS = [
  "wiki.enabled",
  "wiki.roots",
  "wiki.debounce_interval",
  "wiki.poll_interval",
  "wiki.ignore_globs",
] as const;

const OWNED_PATHS: readonly string[] = [
  ...MEMORY_PATHS,
  ...RECALL_PATHS,
  ...EMBEDDINGS_PATHS,
  ...DATABASE_PATHS,
  ...QUEUE_PATHS,
  ...BACKUP_PATHS,
  ...WIKI_PATHS,
];

/**
 * `memory.backend` is validator-bounded to `{local, null}` but typed as a plain
 * string in the schema (no enum), so its options are listed explicitly.
 */
const BACKEND_OPTIONS = [
  { value: "local", label: "Local" },
  { value: "null", label: "Disabled (null)" },
];

/** One watched wiki root: a stable scope name plus a filesystem path. */
interface WikiRoot {
  scope: string;
  path: string;
}

function MemoryGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Memory"
      hint="The persistent memory store, cross-referencing, and recall scoring."
    >
      <SwitchConfigField
        fields={fields}
        path="memory.enabled"
        label="Enable memory"
        ariaLabel="Enable memory"
      />
      <OptionsSelectField
        fields={fields}
        path="memory.backend"
        label="Backend"
        ariaLabel="Memory backend"
        options={BACKEND_OPTIONS}
      />
      <SwitchConfigField
        fields={fields}
        path="memory.auto_crossref"
        label="Auto cross-reference memories"
        ariaLabel="Auto cross-reference memories"
      />
      <NumberConfigField
        fields={fields}
        path="memory.crossref_threshold"
        label="Cross-reference threshold"
        ariaLabel="Cross-reference threshold"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="memory.crossref_max_links"
        label="Max cross-reference links"
        ariaLabel="Max cross-reference links"
      />
      <NumberConfigField
        fields={fields}
        path="memory.access_debounce_seconds"
        label="Access debounce (seconds)"
        ariaLabel="Access debounce (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="memory.code_link_min_score"
        label="Code link minimum score"
        ariaLabel="Code link minimum score"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="memory.temporal_decay_half_life_days"
        label="Temporal decay half-life (days)"
        ariaLabel="Temporal decay half-life (days)"
      />
      <NumberConfigField
        fields={fields}
        path="memory.min_recall_score"
        label="Minimum recall score"
        ariaLabel="Minimum recall score"
        step={0.05}
      />
      <SwitchConfigField
        fields={fields}
        path="memory.graph_edge_weighting"
        label="Graph edge weighting"
        ariaLabel="Graph edge weighting"
      />
      <SwitchConfigField
        fields={fields}
        path="memory.materialize_cooccurrence"
        label="Materialize co-occurrence edges"
        ariaLabel="Materialize co-occurrence edges"
      />
      <SwitchConfigField
        fields={fields}
        path="memory.graph_edge_decay"
        label="Graph edge decay"
        ariaLabel="Graph edge decay"
      />
      <NumberConfigField
        fields={fields}
        path="memory.edge_half_life_days"
        label="Edge decay half-life (days)"
        ariaLabel="Edge decay half-life (days)"
      />
      <SwitchConfigField
        fields={fields}
        path="memory.recall_signal_logging"
        label="Log recall signals"
        ariaLabel="Log recall signals"
      />
      <TextConfigField
        fields={fields}
        path="memory.recall_signal_log_path"
        label="Recall signal log path"
        ariaLabel="Recall signal log path"
        placeholder="Default location"
        nullable
      />
    </Subsection>
  );
}

function KnowledgeGraphGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Knowledge graph extraction"
      hint="The model that extracts entities and relations into the knowledge graph."
    >
      <SchemaSelectField
        fields={fields}
        path="memory.kg.profile"
        label="Model profile"
        ariaLabel="Knowledge graph model profile"
      />
      <StringListConfigField
        fields={fields}
        path="memory.kg.candidates"
        label="Model candidates"
        ariaLabel="Knowledge graph model candidates"
        addLabel="Add candidate"
        placeholder="provider/model"
      />
    </Subsection>
  );
}

function DreamGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Dreaming"
      hint="Scheduled maintenance that consolidates, links, and prunes memories."
    >
      <SwitchConfigField
        fields={fields}
        path="memory.dream.enabled"
        label="Enable memory dreaming"
        ariaLabel="Enable memory dreaming"
      />
      <SchemaSelectField
        fields={fields}
        path="memory.dream.profile"
        label="Model profile"
        ariaLabel="Dream model profile"
      />
      <StringListConfigField
        fields={fields}
        path="memory.dream.candidates"
        label="Model candidates"
        ariaLabel="Dream model candidates"
        addLabel="Add candidate"
        placeholder="provider/model"
      />
      <TextConfigField
        fields={fields}
        path="memory.dream.schedule_cron"
        label="Schedule (cron)"
        ariaLabel="Dream schedule (cron)"
        placeholder="0 2 * * *"
      />
      <TextConfigField
        fields={fields}
        path="memory.dream.prompt_path"
        label="Prompt path"
        ariaLabel="Dream prompt path"
        placeholder="Default prompt"
        nullable
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.max_tokens"
        label="Max tokens"
        ariaLabel="Dream max tokens"
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.max_runtime_seconds"
        label="Admission window (seconds)"
        ariaLabel="Dream admission window (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.work_unit_timeout_seconds"
        label="Work unit timeout (seconds)"
        ariaLabel="Dream work unit timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.evidence_channel_timeout_seconds"
        label="Evidence channel timeout (seconds)"
        ariaLabel="Dream evidence channel timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.evidence_retry_attempts"
        label="Evidence retry attempts"
        ariaLabel="Dream evidence retry attempts"
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.evidence_phase_timeout_seconds"
        label="Evidence phase timeout (seconds)"
        ariaLabel="Dream evidence phase timeout (seconds)"
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.min_action_confidence"
        label="Minimum action confidence"
        ariaLabel="Minimum action confidence"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="memory.dream.min_delete_confidence"
        label="Minimum delete confidence"
        ariaLabel="Minimum delete confidence"
        step={0.05}
      />
      <SwitchConfigField
        fields={fields}
        path="memory.dream.include_global_memories"
        label="Include global memories"
        ariaLabel="Include global memories"
      />
      <SwitchConfigField
        fields={fields}
        path="memory.dream.reconcile_after_apply"
        label="Reconcile after apply"
        ariaLabel="Reconcile after apply"
      />
      <SwitchConfigField
        fields={fields}
        path="memory.dream.reconcile_after_revert"
        label="Reconcile after revert"
        ariaLabel="Reconcile after revert"
      />
    </Subsection>
  );
}

function RecallGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Recall"
      hint="Observational recall that surfaces relevant memories during a session."
    >
      <SwitchConfigField
        fields={fields}
        path="memory_recall.enabled"
        label="Enable memory recall"
        ariaLabel="Enable memory recall"
      />
      <SchemaSelectField
        fields={fields}
        path="memory_recall.profile"
        label="Model profile"
        ariaLabel="Recall model profile"
      />
      <StringListConfigField
        fields={fields}
        path="memory_recall.candidates"
        label="Model candidates"
        ariaLabel="Recall model candidates"
        addLabel="Add candidate"
        placeholder="provider/model"
      />
      <NumberConfigField
        fields={fields}
        path="memory_recall.candidate_limit"
        label="Candidate limit"
        ariaLabel="Recall candidate limit"
      />
      <NumberConfigField
        fields={fields}
        path="memory_recall.selected_limit"
        label="Selected limit"
        ariaLabel="Recall selected limit"
      />
      <NumberConfigField
        fields={fields}
        path="memory_recall.min_score"
        label="Minimum score"
        ariaLabel="Recall minimum score"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="memory_recall.query_synthesis_threshold"
        label="Query synthesis threshold"
        ariaLabel="Query synthesis threshold"
      />
      <NumberConfigField
        fields={fields}
        path="memory_recall.query_max_chars"
        label="Query max characters"
        ariaLabel="Query max characters"
      />
    </Subsection>
  );
}

function EmbeddingsGroup({ fields }: { fields: SettingsSectionFields }) {
  const { runManagedAction } = useSettingsSectionContext();
  const storedCatalogKey = asString(
    fields.getValue("ai.embeddings.catalog_key"),
  );
  const [baseline, setBaseline] = useState(storedCatalogKey);
  const [catalogKey, setCatalogKey] = useState(storedCatalogKey);
  if (storedCatalogKey !== baseline) {
    setBaseline(storedCatalogKey);
    setCatalogKey(storedCatalogKey);
  }
  // Overlays the local, managed-action-driven catalog key over the draft so
  // the catalog-key row (and only that row) renders the pending selection.
  const catalogFields: SettingsSectionFields = {
    ...fields,
    getValue: (path) =>
      path === "ai.embeddings.catalog_key" ? catalogKey : fields.getValue(path),
  };
  return (
    <Subsection
      title="Embeddings"
      hint="The embedding model shared by memory, tools, and the code index. The API key lives in Secrets & Auth."
    >
      <TextConfigField
        fields={fields}
        path="ai.embeddings.model"
        label="Model"
        ariaLabel="Embedding model"
      />
      <NumberConfigField
        fields={fields}
        path="ai.embeddings.dim"
        label="Dimensions"
        ariaLabel="Embedding dimensions"
      />
      <TextConfigField
        fields={fields}
        path="ai.embeddings.api_base"
        label="API base URL"
        ariaLabel="Embedding API base URL"
        nullable
      />
      <TextConfigField
        fields={fields}
        path="ai.embeddings.query_prefix"
        label="Query prefix"
        ariaLabel="Embedding query prefix"
        nullable
      />
      <TextConfigField
        fields={catalogFields}
        path="ai.embeddings.catalog_key"
        label="Catalog key"
        ariaLabel="Embedding catalog key"
        placeholder="provider/model"
        managedAction={(value) => {
          if (typeof value === "string") setCatalogKey(value);
        }}
      />
      <div className="flex flex-wrap gap-2">
        <DetailActionButton
          label="Start embedding switch"
          variant="accent"
          disabled={!runManagedAction || !catalogKey || catalogKey === baseline}
          onClick={() => {
            if (runManagedAction) {
              void runManagedAction("/api/embeddings/switch/start", {
                catalog_key: catalogKey,
              }).then((started) => {
                if (started) setBaseline(catalogKey);
              });
            }
          }}
        />
      </div>
    </Subsection>
  );
}

function VectorStoreGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Vector store (Qdrant)"
      hint="Connection for the Qdrant vector database. The API key lives in Secrets & Auth."
    >
      <TextConfigField
        fields={fields}
        path="databases.qdrant.url"
        label="URL"
        ariaLabel="Qdrant URL"
        placeholder="http://localhost:6333"
        nullable
      />
      <NumberConfigField
        fields={fields}
        path="databases.qdrant.port"
        label="Port"
        ariaLabel="Qdrant port"
      />
      <TextConfigField
        fields={fields}
        path="databases.qdrant.collection_prefix"
        label="Collection prefix"
        ariaLabel="Qdrant collection prefix"
        placeholder="gobby_"
      />
    </Subsection>
  );
}

function GraphStoreGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Graph store (FalkorDB)"
      hint="Connection and hybrid-search tuning for the FalkorDB graph database. The password lives in Secrets & Auth."
    >
      <TextConfigField
        fields={fields}
        path="databases.falkordb.host"
        label="Host"
        ariaLabel="FalkorDB host"
        placeholder="localhost"
      />
      <NumberConfigField
        fields={fields}
        path="databases.falkordb.port"
        label="Port"
        ariaLabel="FalkorDB port"
      />
      <TextConfigField
        fields={fields}
        path="databases.falkordb.graph_name"
        label="Graph name"
        ariaLabel="FalkorDB graph name"
        placeholder="gobby"
      />
      <SwitchConfigField
        fields={fields}
        path="databases.falkordb.graph_search"
        label="Graph search"
        ariaLabel="FalkorDB graph search"
      />
      <NumberConfigField
        fields={fields}
        path="databases.falkordb.graph_min_score"
        label="Graph minimum score"
        ariaLabel="FalkorDB graph minimum score"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="databases.falkordb.rrf_k"
        label="RRF k"
        ariaLabel="FalkorDB RRF k"
      />
    </Subsection>
  );
}

function QueueGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Knowledge graph queue"
      hint="The background queue that processes pending knowledge-graph work."
    >
      <NumberConfigField
        fields={fields}
        path="knowledge_graph_queue.interval_minutes"
        label="Interval (minutes)"
        ariaLabel="Queue interval (minutes)"
      />
      <NumberConfigField
        fields={fields}
        path="knowledge_graph_queue.batch_size"
        label="Batch size"
        ariaLabel="Queue batch size"
      />
    </Subsection>
  );
}

function BackupGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Memory backup"
      hint="Deterministic JSONL backup for explicit recovery and migration."
    >
      <SwitchConfigField
        fields={fields}
        path="memory_backup.enabled"
        label="Enable memory backup"
        ariaLabel="Enable memory backup"
      />
      <TextConfigField
        fields={fields}
        path="memory_backup.backup_path"
        label="Backup path"
        ariaLabel="Memory backup path"
        placeholder=".gobby/memories.jsonl"
      />
    </Subsection>
  );
}

/**
 * The `wiki.roots` editor: a `list[WikiRootConfig]` of watched roots. Each entry
 * is a `{scope, path}` object, so it uses the structured `TypedListField`
 * primitive with a two-field editor per row rather than a flat string list.
 */
function WikiRootsField({ fields }: { fields: SettingsSectionFields }) {
  return (
    <TypedListField<WikiRoot>
      label="Wiki roots"
      ariaLabel="Wiki root"
      value={asTypedList<WikiRoot>(fields.getValue("wiki.roots"))}
      addLabel="Add wiki root"
      createItem={() => ({ scope: "", path: "" })}
      itemLabel={(root, index) =>
        asString(root.scope) || `Wiki root ${index + 1}`
      }
      onChange={(value) => fields.setValue("wiki.roots", value)}
      renderItem={(root, onItemChange, index) => (
        <>
          <TextField
            label="Scope"
            ariaLabel={`Wiki root ${index + 1} scope`}
            value={asString(root.scope)}
            placeholder="project"
            onChange={(value) => onItemChange({ ...root, scope: value })}
          />
          <TextField
            label="Path"
            ariaLabel={`Wiki root ${index + 1} path`}
            value={asString(root.path)}
            placeholder="docs/wiki"
            onChange={(value) => onItemChange({ ...root, path: value })}
          />
        </>
      )}
    />
  );
}

function WikiGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Wiki watcher"
      hint="Filesystem watching that indexes configured wiki roots."
    >
      <SwitchConfigField
        fields={fields}
        path="wiki.enabled"
        label="Enable wiki watcher"
        ariaLabel="Enable wiki watcher"
      />
      <WikiRootsField fields={fields} />
      <NumberConfigField
        fields={fields}
        path="wiki.debounce_interval"
        label="Debounce interval (seconds)"
        ariaLabel="Wiki debounce interval (seconds)"
        step={0.25}
      />
      <NumberConfigField
        fields={fields}
        path="wiki.poll_interval"
        label="Poll interval (seconds)"
        ariaLabel="Wiki poll interval (seconds)"
        step={0.25}
      />
      <StringListConfigField
        fields={fields}
        path="wiki.ignore_globs"
        label="Ignore globs"
        ariaLabel="Wiki ignore globs"
        addLabel="Add glob"
        placeholder="outputs/**"
      />
    </Subsection>
  );
}

export function MemoryKnowledgeSection() {
  return (
    <SettingsSection sectionId="memory-knowledge" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <MemoryGroup fields={fields} />
          <KnowledgeGraphGroup fields={fields} />
          <DreamGroup fields={fields} />
          <RecallGroup fields={fields} />
          <EmbeddingsGroup fields={fields} />
          <VectorStoreGroup fields={fields} />
          <GraphStoreGroup fields={fields} />
          <QueueGroup fields={fields} />
          <BackupGroup fields={fields} />
          <WikiGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  );
}
