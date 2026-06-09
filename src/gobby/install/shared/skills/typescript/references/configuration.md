# Configuration - Reference

Source note: adapted for Gobby from the CUBETIQ `typescript-best-practices` SkillsMP source and cross-checked against SkillsMP JavaScript/TypeScript strictness skills.

## Baseline Compiler Options

Start from the repo's existing config. For new packages or strict migrations, this is the target shape:

```jsonc
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "noPropertyAccessFromIndexSignature": true,

    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "isolatedModules": true,
    "verbatimModuleSyntax": true,

    "sourceMap": true,
    "declaration": true,
    "declarationMap": true,

    "rootDir": "src",
    "outDir": "dist"
  },
  "include": ["src/**/*.ts", "src/**/*.tsx"],
  "exclude": ["node_modules", "dist"]
}
```

Do not blindly paste this over framework-generated configs. Next.js, Vite, Remix, SvelteKit, NestJS, and library templates often need specific `jsx`, `lib`, `types`, `module`, or emit settings.

## Strictness Sequence

For a loose codebase, migrate in this order:

1. Turn on `strict: true` and fix real type holes before changing runtime behavior.
2. Add `noUncheckedIndexedAccess` and handle array or record misses explicitly.
3. Add `exactOptionalPropertyTypes` and distinguish missing fields from fields set to `undefined`.
4. Add unused, fallthrough, implicit return, and index-signature property checks.
5. Replace temporary suppressions with type guards, schema parsing, or narrower APIs.

Use `// @ts-expect-error` only as a migration marker with a comment explaining why the error is expected. Avoid `// @ts-ignore`.

## Module Resolution

Use the mode that matches runtime and bundling:

| Project kind | Recommended settings | Notes |
| --- | --- | --- |
| Browser app through Vite, webpack, esbuild, Turbopack | `module: "ESNext"`, `moduleResolution: "bundler"` | Lets the bundler own final resolution and supports package `exports`. |
| Node library or CLI using ESM | `module: "NodeNext"`, `moduleResolution: "NodeNext"` | Requires `.js` in relative source imports that emit to ESM. |
| CommonJS package | Follow existing build chain | Prefer migration to ESM only when package consumers can tolerate it. |
| Monorepo packages | project references plus per-package configs | Use `tsc --build` for incremental cross-package checks. |

Avoid legacy `moduleResolution: "node"` for new code. It does not model modern package `exports` accurately.

## Type-Only Imports

With `verbatimModuleSyntax`, write imports so runtime and type edges are visible:

```ts
import type { ClientConfig } from "./config";
import { createClient, type Client } from "./client";
```

This avoids accidental runtime imports, improves bundler output, and prepares code for stricter type-erasure workflows.

## Runtime-Erasing TypeScript

Prefer syntax that can be erased without changing runtime semantics:

| Avoid in new code | Prefer |
| --- | --- |
| `enum` | `as const` object plus value union |
| `const enum` | `as const` object |
| value-producing `namespace` | ES modules |
| parameter properties | explicit fields and assignments |
| legacy decorator metadata | framework-supported standard decorators or explicit registration |

This reduces surprises across bundlers, `isolatedModules`, native TS stripping, and future compiler modes.

## Monorepo References

Use project references when packages depend on each other:

```jsonc
// tsconfig.json
{
  "files": [],
  "references": [
    { "path": "packages/core" },
    { "path": "packages/client" }
  ]
}
```

Each referenced package needs `composite: true`, stable `rootDir`/`outDir`, and explicit package boundaries. Avoid importing through another package's private source path.

## Lint Integration

TypeScript compiler checks do not catch every unsafe pattern. When the repo uses ESLint, prefer type-aware `typescript-eslint` configs for:

- unsafe `any` flows
- floating promises
- unhandled async callbacks
- unnecessary assertions
- misuse of `void`, `await`, and template expressions

Type-aware lint is slower. Scope it to packages that need it or use separate CI jobs if the repo already optimizes lint performance.
