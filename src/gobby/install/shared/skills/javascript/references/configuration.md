# JavaScript Configuration

Configure JavaScript packages so runtime behavior is obvious from files and CI.

## Package Manager

- Use the lockfile already present in the repo: `pnpm-lock.yaml`, `package-lock.json`, `yarn.lock`, or `bun.lockb`.
- Do not mix package managers in one change.
- Keep generated lockfile changes in the same commit as dependency changes.
- Prefer workspace commands when the repo is a monorepo, such as `pnpm --filter <pkg> test`.

## Module System

- Make `package.json` `type` explicit for packages that publish or run Node code.
- Use `.mjs` for ESM and `.cjs` for CommonJS when a package mixes module systems.
- Prefer ESM for new application code unless the runtime, test runner, or dependency surface requires CommonJS.
- Use `exports` to define public entry points and hide internal modules from accidental imports.

Example dual-package surface:

```json
{
  "type": "module",
  "exports": {
    ".": {
      "import": "./dist/index.js",
      "require": "./dist/index.cjs"
    }
  }
}
```

## Lint and Format

- Keep one formatter authoritative. Do not add Prettier and Biome formatting for the same package unless the repo already does.
- Enable rules that catch accidental globals, unused values, unhandled promises, unsafe equality, and import mistakes.
- Do not disable a rule for a whole file when a local suppression with a reason is enough.
- Keep generated files, build outputs, vendored files, and snapshots out of lint scope.

## Checked JavaScript

Use checked JavaScript when the package is large, public, or has cross-module contracts:

```json
{
  "compilerOptions": {
    "allowJs": true,
    "checkJs": true,
    "noEmit": true,
    "moduleResolution": "bundler"
  },
  "include": ["src/**/*.js"]
}
```

Add `// @ts-check` to files that benefit from local checking if repo-wide `checkJs` is not practical yet.

## Scripts

- Scripts should be deterministic and runnable from a clean checkout.
- Prefer `lint`, `test`, `build`, and `typecheck` names that fit the existing repo.
- Avoid scripts that depend on a developer's global packages, shell aliases, or local daemon state.
- Document required environment variables through examples or validation, not by letting scripts fail later.
