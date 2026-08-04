# Model configuration

### Model aliases

| Model alias | Behavior |
| --- | --- |
| **`default`** | Clears any model override. |
| **`best`** | Uses Fable 5 where available, otherwise Opus. |
| **`fable`** | Uses Claude Fable 5. |
| **`sonnet`** | Uses the latest Sonnet model. |
| **`opus`** | Uses the latest Opus model. |
| **`haiku`** | Uses the fast and efficient Haiku model. |
| **`sonnet[1m]`** | Uses Sonnet with a 1 million token context window. |
| **`opus[1m]`** | Uses Opus with a 1 million token context window. |
| **`opusplan`** | Uses `opus` during plan mode, then switches to `sonnet`. |

The version that the `opus` and `sonnet` aliases resolve to depends on the provider:

| Provider | `opus` | `sonnet` |
| :--- | :--- | :--- |
| Anthropic API | Opus 5 | Sonnet 5 |
| Amazon Bedrock | Opus 5 | Sonnet 4.5 |
