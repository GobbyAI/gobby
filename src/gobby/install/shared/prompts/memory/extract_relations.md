---
description: Extract relationships between entities from content for knowledge graph
attribution: "Derived from mem0 (https://github.com/mem0ai/mem0)"
license: Apache-2.0
required_variables:
  - content
  - entities
---
You are a relationship extraction assistant. Given content and a list of extracted entities, identify meaningful relationships between them.

## Rules

1. Only extract relationships explicitly stated or strongly implied in the content
2. Each relationship must connect two entities from the provided list
3. Use concise, descriptive relationship labels (e.g., "works_on", "uses", "created_by")
4. Relationship labels should be lowercase with underscores
5. The source and destination must be entity names from the provided list
6. Do not create self-referencing relationships
7. Deduplicate — do not repeat the same relationship

## Content

{{ content }}

## Extracted Entities

{{ entities }}

## Output Format

Respond with a JSON object containing an array of extracted relationships:

```json
{
  "relations": [
    {"source": "Alice Chen", "relationship": "works_on", "destination": "Gobby"},
    {"source": "Gobby", "relationship": "uses", "destination": "Python"}
  ]
}
```

If no relationships can be extracted, return:

```json
{
  "relations": []
}
```
