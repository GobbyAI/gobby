import type { FieldOption } from "../activity/fields";

/**
 * Schema helpers for the settings overlay, owned here rather than imported from
 * the legacy configuration page, which has been deleted. The daemon config
 * schema (`/api/config/schema`) is a nested
 * JSON-schema with `$defs` + `$ref`: nested config objects are `$ref`s into
 * `$defs`, and even scalar leaves like `*.profile` are `$ref`s to an enum def.
 */

type SchemaNode = Record<string, unknown>;

function getDefs(root: SchemaNode): Record<string, SchemaNode> {
  const defs = (root.$defs ?? root.definitions) as
    Record<string, SchemaNode> | undefined;
  return defs ?? {};
}

/**
 * Follow a node's `$ref` chain into `$defs`, merging any sibling keys (e.g. a
 * field-local `default`/`description`) over the referenced def so local
 * overrides win. Guards against ref cycles.
 */
function resolveRef(
  node: SchemaNode,
  defs: Record<string, SchemaNode>,
): SchemaNode {
  let resolved = node;
  const seen = new Set<string>();
  while (typeof resolved.$ref === "string") {
    const refName = resolved.$ref.split("/").pop();
    if (!refName || seen.has(refName)) break;
    const target = defs[refName];
    if (!target) break;
    seen.add(refName);
    const { $ref: _ref, ...overrides } = resolved;
    resolved = { ...target, ...overrides };
  }
  return resolved;
}

/**
 * Resolve the fully-dereferenced schema node at a dotted config path, walking
 * `properties` segment by segment and resolving `$ref`s at each step (including
 * the leaf's own `$ref`). Returns null for the empty/unknown paths or a null
 * schema.
 */
export function resolveSchemaNode(
  root: SchemaNode | null | undefined,
  dottedPath: string,
): SchemaNode | null {
  if (!root || !dottedPath) return null;
  const direct = (root.properties as Record<string, SchemaNode> | undefined)?.[
    dottedPath
  ];
  if (direct && typeof direct === "object") return direct;
  const defs = getDefs(root);
  let current = resolveRef(root, defs);
  for (const part of dottedPath.split(".")) {
    const props = current.properties as Record<string, SchemaNode> | undefined;
    const next = props?.[part];
    if (next && typeof next === "object") {
      current = resolveRef(next, defs);
      continue;
    }
    // Open maps (`dict[str, T]`) expose the item schema as additionalProperties.
    const additional = current.additionalProperties;
    if (additional && typeof additional === "object") {
      current = resolveRef(additional as SchemaNode, defs);
      continue;
    }
    return null;
  }
  return current;
}

/**
 * The `enum` values at a dotted path as bounded-select options, or `[]` when
 * the path is missing or not an enum.
 */
export function enumOptionsAt(
  root: SchemaNode | null | undefined,
  dottedPath: string,
): FieldOption[] {
  const node = resolveSchemaNode(root, dottedPath);
  const values = node?.enum;
  if (!Array.isArray(values)) return [];
  return values
    .filter((value): value is string => typeof value === "string")
    .map((value) => ({ value, label: value }));
}

/** Numeric `minimum`/`maximum` bounds at a dotted path, omitting absent ones. */
export function numberBoundsAt(
  root: SchemaNode | null | undefined,
  dottedPath: string,
): { min?: number; max?: number } {
  const node = resolveSchemaNode(root, dottedPath);
  const bounds: { min?: number; max?: number } = {};
  if (typeof node?.minimum === "number") bounds.min = node.minimum;
  if (typeof node?.maximum === "number") bounds.max = node.maximum;
  return bounds;
}
