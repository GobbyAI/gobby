# Model configuration

### Model aliases

| Model alias | Behavior |
| --- | --- |
| **`default`** | Special value that clears any model override. |
| **`best`** | Uses the model the `fable` alias resolves to where Fable is available, otherwise the same model as `opus`. |
| **`fable`** | Uses the Fable model for your provider. |
| **`sonnet`** | Uses the latest Sonnet model for daily coding tasks. |
| **`opus`** | Uses the latest Opus model for complex reasoning tasks. |
| **`haiku`** | Uses the fast and efficient Haiku model for simple tasks. |
| **`sonnet[1m]`** | Uses Sonnet with a 1 million token context window. |
| **`opus[1m]`** | Uses Opus with a 1 million token context window. |
| **`opusplan`** | Uses `opus` during plan mode, then switches to `sonnet`. |

The version that the `opus` and `sonnet` aliases resolve to depends on the provider:

| Provider | `opus` | `sonnet` |
| :--- | :--- | :--- |
| Anthropic API | Opus 5 | Sonnet 5 |
| Amazon Bedrock | Opus 5 | Sonnet 4.5 |
