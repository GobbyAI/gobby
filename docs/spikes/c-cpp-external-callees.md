# C/C++ External Callee Provenance Spike

## Decision

Do not classify C or C++ external callees from tree-sitter parse output plus `#include` directives alone. Safe producer-side classification requires compile-command or symbol metadata that proves the target declaration came from an external header or library.

The current parser surface is useful for recording import edges, but not for deciding call ownership:

- `languages.rs` captures `preproc_include` as imports for C and C++.
- C call extraction captures only bare `call_expression function: (identifier)`.
- C++ call extraction captures bare identifiers and field-expression members.
- Neither grammar query expands macros, resolves overloads, applies include search paths, or distinguishes local declarations from declarations introduced by headers.

## Why Includes Are Insufficient

`#include <vector>` or `#include "vendor/foo.h"` proves that a translation unit references a header. It does not prove that `foo()` resolves to that header. C and C++ allow local declarations, overloads, macros, namespace aliases, ADL, templates, and conditional compilation to change the callable target after parsing.

Examples that must remain unresolved without semantic metadata:

```cpp
#include <algorithm>

void sort();

void run() {
    sort();
}
```

```cpp
#include "vendor/client.h"

namespace vendor {
class Client {};
}

void run(Client& client) {
    client.connect();
}
```

```c
#include <stdio.h>

#define printf my_printf

void run(void) {
    printf("x");
}
```

All three have explicit include surfaces, but tree-sitter alone cannot prove the external callee.

## Required Provenance

A safe implementation needs at least one of these sources:

- `compile_commands.json` with include search paths, language standard, defines, and source-to-translation-unit mapping.
- A semantic symbol provider such as libclang or clangd that can return the resolved declaration location for a call expression.
- A generated declaration index that records whether a declaration path is inside the project root, vendored inside the project, or outside the project.

The producer can classify a call as external only when:

- The semantic resolver returns exactly one target declaration.
- The declaration path is outside the current project root or belongs to a configured external SDK/system include root.
- No project-local declaration or macro shadows the callable at the call site.
- The target is not a receiver/member call without proven receiver type ownership.

## Minimum Implementation Path

1. Keep current `preproc_include` import storage.
2. Add optional discovery for `compile_commands.json` at the project root and common build directories.
3. Add a semantic resolution stage for C/C++ call captures that can map call byte ranges to resolved declarations.
4. Add an external-root classifier for declaration paths:
   - project root: local or unresolved
   - configured vendored roots: unresolved unless explicitly marked external
   - system/SDK/package-manager roots: external
5. Emit `ExternalSymbol` only when the semantic result is unique and external.

## Fail-Closed Boundary

Leave these unresolved:

- Calls in files without compile-command coverage.
- Macro-expanded calls and calls whose callee text differs after preprocessing.
- Overloaded/template calls without a unique resolved declaration.
- ADL-dependent calls unless the semantic provider returns one external declaration.
- Member calls without proven receiver type ownership.
- Calls to declarations inside the project root, even if the header was included with angle brackets.

_Last verified: 2026-05-24_
