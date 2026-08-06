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

- Supported models: `claude-fable-5`, `claude-mythos-5`, `claude-mythos-preview`, `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-opus-4-5-20251101`
