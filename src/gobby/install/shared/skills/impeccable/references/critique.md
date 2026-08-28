# Critique mode

Establish the design contract and call `get_skill_file(name="impeccable", path="references/critique-workflow.md")` for every critique.

- Load `references/critique-cognitive-load.md` when density, hierarchy, comprehension, or interaction burden is in scope.
- Load `references/critique-scoring.md` when assigning heuristic scores or severity.
- Load `references/critique-personas.md` when testing multiple user perspectives.

Use at most two conditional references alongside the workflow. Preserve the persisted critique snapshot, recommended actions, validators, and recovery behavior from the workflow.
