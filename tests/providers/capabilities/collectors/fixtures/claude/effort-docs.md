# Effort

The effort parameter is available on all supported models with no beta header required.

### Effort levels

| Level | Description | Typical use case |
| --- | --- | --- |
| `max` | Available on Claude Fable 5, Claude Opus 5, Claude Opus 4.6, and Claude Sonnet 5. | Deep reasoning |
| `xhigh` | Available on Claude Fable 5, Claude Opus 5, and Claude Sonnet 5. | Long-running tasks |
| `high` | Equivalent to omitting the effort parameter. | Complex tasks |
| `medium` | Balanced token savings. | Agentic tasks |
| `low` | Most efficient. | Simple tasks |

## Compatibility

Supported models

- Fable 5
- Opus 4.6 and 5
- Sonnet 5
