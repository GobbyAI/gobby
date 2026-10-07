"""Tags that keep a memory out of the pushed memory index and away from the dream.

Memories carrying any of these tags are minted and verified by their own
pipelines and reach agents only through their own injection paths. The
memory-index push filters them out, and the memory dream never lists them as
candidates, so a dream refresh cannot rewrite the tag away and leak them into
the index. Adding a tag here protects it on both paths.
"""

# Review lessons swamped generically worded queries in the graded ranking
# cohort and already have their own injection path.
PUSH_EXCLUDED_TAGS: tuple[str, ...] = ("review-lesson",)
