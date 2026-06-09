# JavaScript Contracts

JavaScript does not have compile-time types by default, so make important contracts explicit at runtime and in editor-visible metadata.

## JSDoc

Use JSDoc for exported functions, domain objects, and non-obvious local contracts.

```js
// @ts-check

/**
 * @typedef {object} User
 * @property {string} id
 * @property {string} email
 * @property {"active" | "disabled"} status
 */

/**
 * @param {unknown} value
 * @returns {User}
 */
export function parseUser(value) {
  if (!value || typeof value !== "object") {
    throw new TypeError("Expected user object");
  }
  const record = /** @type {Record<string, unknown>} */ (value);
  if (typeof record.id !== "string" || typeof record.email !== "string") {
    throw new TypeError("Invalid user payload");
  }
  if (record.status !== "active" && record.status !== "disabled") {
    throw new TypeError("Invalid user status");
  }
  return {
    id: record.id,
    email: record.email,
    status: record.status,
  };
}
```

## Runtime Validation

- Validate data at external boundaries before property access.
- Use a schema library already present in the repo when payloads are shared or complex.
- Keep small hand-written validators for narrow local payloads.
- Return normalized objects instead of passing raw API or file data deeper into the system.

## Object Shapes

- Prefer explicit object construction over mutating arbitrary bags.
- Avoid adding ad hoc properties to built-in objects, request objects, or errors unless the framework documents that extension point.
- Keep variant state explicit with a `kind`, `type`, or `status` field.
- Use `Object.freeze` or copy-on-write when shared constants must not be mutated.

## Migration-Friendly JavaScript

- Avoid implicit globals, dynamic `require` paths, prototype monkey-patching, and broad `this` binding.
- Keep exported APIs small and named.
- Make nullability visible with guards and defaults near the boundary.
- Do not hide important data shapes in comments that tooling cannot read when JSDoc or schemas would work.
