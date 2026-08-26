"""Constants shared by memory search helpers."""

DEFAULT_SEARCH_LIMIT = 10
# Two hits whose stored vectors sit at or above this cosine are one memory said
# twice: the lower-ranked one is folded into the higher-ranked one's
# ``collapsed_duplicates`` before the limit cut (#21010). 0.92 sits above the live
# corpus's query-hit band (p99 0.80) and below exact restatements, so paraphrases
# of one contract collapse while distinct facts on one topic do not.
_NEAR_DUPLICATE_COSINE = 0.92
_USER_SOURCE_BOOST = 1.2
_GRAPH_EXPANSION_ENTITY_SEED_LIMIT = 8
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

# PROVISIONAL, owned by the Phase 4 refit (#20873).
#
# A graph-expander find is admitted on its entity-match confidence, not on its
# cosine. The expander's whole value is the low-cosine, high-confidence hit --
# anything it surfaces that also clears the cosine floor, the vector leg already
# returned -- so judging it on cosine switches #17104 off. Since #20858 filled a
# real cosine in for every scorable candidate, that is exactly what happened.
#
# These are separate constants rather than the cosine floors divided at runtime
# because confidence and cosine are different distributions. The measured 2026-08
# confidence distribution over 17,975 graph-sourced hits in recall_signal_hits:
#
#     min 0.451   p10 0.512   p50 0.572   p90 0.653   max 0.896
#
# SEARCH FLOOR 0.611 is the faithful translation of the pre-#20858 behaviour,
# where a graph-only hit entered the similarity axis at
# `confidence * _GRAPH_SYNTHETIC_SIM_DISCOUNT` and so faced `0.55 / 0.9`. It
# admits 26.3% of graph hits, which is a real restoration.
#
# SELECTION FLOOR 0.653 is the p90 of that distribution, and is deliberately NOT
# the faithful translation. That would be `0.70 / 0.9 = 0.778`, which measured out
# at the 99.6th percentile: 77 of 17,975 hits. The pre-#20858 injection gate was
# therefore already functionally off, and #20858 only took 0.43% to zero --
# preserving it would preserve an accident. Seating at p90 puts the top decile
# through the gate, which is also what generates the injection-outcome
# observations Phase 4 needs to refit against; a floor firing on 0.43% of hits
# generates none. Exposure stays bounded because these hits still rank by real
# cosine and so sit low, and MAX_RECALL_MEMORIES caps injection volume regardless
# of how many clear the floor. Selection stays strictly tighter than search.
#
# The 0.65 fossil #20771 had to unwind happened because a constant's calibration
# context lived nowhere near the constant. Hence this block.
_GRAPH_CONFIDENCE_SEARCH_FLOOR = 0.611
_GRAPH_CONFIDENCE_SELECTION_FLOOR = 0.653

# Backfill against soft-delete top-k poisoning (#17162). SQL hydration is the source of
# truth for visibility: ranked candidates come from Qdrant/graph, which retain soft-hidden
# rows until purge, so hidden IDs eat result slots after materialization drops them.
_OVERFETCH_FACTOR = 2
_BACKFILL_GROWTH = 2
_MAX_BACKFILL_ROUNDS = 3
