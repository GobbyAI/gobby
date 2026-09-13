# Whisper vocabulary

Load before inspecting or changing speech-recognition terminology.
Discover `gobby-voice`; fetch the schemas for `list_vocab`, `add_vocab`,
`remove_vocab`, or `clear_vocab` as needed.

1. Call `list_vocab()` to see active terms and `whisper_prompt`.
2. For an authorized edit, call `add_vocab(terms="Kubernetes, FastAPI")` or
   `remove_vocab(terms="obsolete term")`. Input is comma-separated, whitespace
   is trimmed, and matching uses lowercase comparisons.
3. Inspect `success`, counts, and any configuration error. Blank input is
   rejected; duplicate additions and absent removals need no write.
4. Compare desired and active configuration if a successful change is not
   visible yet. Mutations base their revision-checked write on desired values;
   `list_vocab` reads active values. Reload current configuration after a
   revision conflict before retrying the intended edit.

`clear_vocab()` empties the entire desired vocabulary, including seeded terms.
It preserves `whisper_prompt`; it does not restore defaults. Load the config
reference for default restoration and model/session refresh. An already-created
Whisper instance can retain its earlier configuration until unloaded.

Vocabulary biases Whisper recognition; it does not rename words in transcripts
or configure external providers. Do not clear terms merely to diagnose a typo.

See [vocabulary tools](../../../../../../../../docs/guides/voice.md#whisper-vocabulary-tools).
