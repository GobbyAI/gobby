---
title: Effort
url: https://platform.claude.com/docs/en/build-with-claude/effort
description: Control how many tokens Claude uses when responding with the effort parameter, trading off between response thoroughness and token efficiency.
featureMetadata:
  status: ga
  zdr:
    eligibility: eligible
    note: Excludes [Covered Models](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention#model-specific-data-retention-requirements).
  supportedModels:
    - claude-fable-5-1
    - claude-mythos-5-1
    - claude-fable-5
    - claude-mythos-5
    - claude-mythos-preview
    - claude-opus-5
    - claude-opus-4-8
    - claude-opus-4-7
    - claude-opus-4-6
    - claude-opus-4-5-20251101
    - claude-sonnet-5
    - claude-sonnet-4-6
  supportedPlatforms:
    Claude API: ga
    Claude Platform on AWS: ga
    Amazon Bedrock: ga
    Google Cloud: ga
    Microsoft Foundry: ga
---

The effort parameter is available on all supported models with no beta header required.

### Effort levels

| Level | Description | Typical use case |
| --- | --- | --- |
| `max` | Available on Claude Fable 5.1, Claude Mythos 5.1, Claude Fable 5, Claude Mythos 5, Claude Mythos Preview, Claude Opus 5, Claude Opus 4.8, Claude Opus 4.7, Claude Opus 4.6, Claude Sonnet 5, and Claude Sonnet 4.6. | Deep reasoning |
| `xhigh` | Available on Claude Fable 5.1, Claude Mythos 5.1, Claude Fable 5, Claude Mythos 5, Claude Opus 5, Claude Opus 4.8, Claude Opus 4.7, and Claude Sonnet 5. | Long-running tasks |
| `high` | Equivalent to omitting the effort parameter. | Complex tasks |
| `medium` | Balanced token savings. | Agentic tasks |
| `low` | Most efficient. | Simple tasks |
