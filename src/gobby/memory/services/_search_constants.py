"""Constants shared by memory search helpers."""

DEFAULT_SEARCH_LIMIT = 10
_USER_SOURCE_BOOST = 1.2
_GRAPH_EXPANSION_ENTITY_SEED_LIMIT = 8
_GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS = 2.0
# Recall expander (#17104): a memory the vector index missed, surfaced by an entity it
# mentions that matched the query, enters the similarity axis at its entity-match cosine
# discounted by this factor. The discount reflects the indirection (entity match, not a
# direct document match) and keeps graph-only hits conservative, so a strong real
# semantic hit always outranks them. Both values are cosines, so the larger always wins
# and semantic-first is preserved for every hit carrying a real similarity score.
_GRAPH_SYNTHETIC_SIM_DISCOUNT = 0.9
# A CO_OCCURS-traversed memory is one structural hop removed from a direct entity match,
# so its synthetic confidence is the seed entity cosine attenuated by this factor.
_GRAPH_TRAVERSAL_CONFIDENCE_FACTOR = 0.9

# Backfill against soft-delete top-k poisoning (#17162). SQL hydration is the source of
# truth for visibility: ranked candidates come from Qdrant/graph, which retain soft-hidden
# rows until purge, so hidden IDs eat result slots after materialization drops them.
_OVERFETCH_FACTOR = 2
_BACKFILL_GROWTH = 2
_MAX_BACKFILL_ROUNDS = 3
