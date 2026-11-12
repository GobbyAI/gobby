# Completed study and Codex/Clef diagnostic, 2026-11-11

`run.json`, `key.json`, `sheet.csv` and `sheet.rated.csv` retain the original
15-community D/G/J and carry study. Josh supplied 44 new ratings; one prior rating
matched the exact displayed member list and name. Completion and reuse provenance
are in `rating-completion.json` and `rating-provenance.json`. The scorer output is
`label-quality-results.md`. Keep the original Gemma experiment distinct from the
following operator-requested diagnostic.

## Codex-authored names checked with Clef

Codex authored the 15 fixed names in `codex-label-candidates-20261111.json` directly
from the saved member paths. No text generation service generated these candidates.
`codex-clef-comparison-20261111.json` holds all successful results. The exact executed
probe is preserved as `paired-diagnostic.py.txt`; its workspace paths identify the
original inputs and output. To reproduce, copy the probe to a Python file, point
ROOT and PRIOR at retained artifacts, choose a new output path, and run it with
`uv run --no-sync python <probe.py>` in a checkout providing the Gobby package and
the same local Clef endpoint. Reproduction performs local inference and may differ.

Backend: local `http://127.0.0.1:11434`, `systemone`, `clef:27b`, response identity
`response:clef:27b`, configured context 16384, service timeout 30 seconds. The first
attempt used a two-second timeout and failed; the retained complete run used 30.
There were 45 successful calls, yielding 30 answers each for Choice, Score and Noul.
API charges were zero; local compute cost was unmeasured.

Choice hid the candidate sources behind `a` and `b`, offered the original deterministic
fallback, and asked twice with option positions swapped in the same request. Score
asked usefulness for finding code, with levels 0 (misleading or unhelpful), 1 (partly
useful), 2 (clear and useful). The reported score is the probability-weighted expected
value. Noul checked whether each name still usefully describes the saved perturbed
membership. Score and Noul always placed Codex first; those shapes have no order
control. The paired questions share a request and are not independent trials.

| Observation | Codex | Gemma |
| --- | ---: | ---: |
| Choice wins across both orders | 13/30 | 17/30 |
| Communities winning in both orders | 6 | 8 |
| Mean expected usefulness (0–2) | 1.493682 | 1.498268 |
| Noul keep at probability ≥ 0.5 | 7/15 | 7/15 |

Community 90 changed its selected source when positions swapped; in both positions
Clef selected option `a`. No fallback was selected. Names and per-community values
remain in the raw JSON. Examples favored in both orders include Codex's “Session
Startup and Handoff Binding” and “Migration and Legacy Removal Checks,” and Gemma's
“Web Interface Components.”

The aggregate usefulness difference is 0.004586/2. These observations do not establish
human quality, carry precision, statistical equivalence or a better production
generator. Complete frozen member lists were supplied, unlike the production
40-path/8000-character representation. Only paths, rather than file bodies, informed
the names and checks. The comparison is diagnostic and does not replace Josh's
ratings or qualify enforcement. No candidate labels or configuration were written
to the live index. No new ratings are requested by this evidence.

Source reference for the representation difference, at
`add0e6c3e496fd81ba9439b1bfe8e5eebc624789`:
`src/gobby/code_index/community_labeler.py`, `67| _MAX_PROMPT_MEMBERS = 40` and
`68| _MAX_PROMPT_DATA_CHARS = 8000`, excerpt hash
`c47dcb97699a9f5835c402df9fba306b2efb2f690c98edf2c3d08b70dbb61a65`.
The `_community_state` implementation at lines 502–532 admits whole paths under
those limits, excerpt hash
`73e997650b77a0f53343e8fed61e5beff2fd13dc3abbac6891f0c292f5aa2d3f`.
