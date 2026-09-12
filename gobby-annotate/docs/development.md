# Development boundaries

The extension consumes Gobby's existing Button, Input, Textarea, CVA styles and
design tokens at build time. It does not change those shared source files.
The distributable bundles them, so installation does not require this checkout.

The workspace uses TypeScript `strict`. It follows the shared UI's existing
optional-property and indexed-access semantics: enabling `exactOptionalPropertyTypes`
and `noUncheckedIndexedAccess` for the consumer also checks imported shared source
under those settings, which the shared components do not currently support.
Those two additional flags are therefore left at their defaults at this boundary.

Brand logos referenced by the shared tokens are packaged locally; controls use
the existing primitives and Lucide icons, without remote assets.
