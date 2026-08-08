> You are continuing a session under the `impeccable` skill; the design-context protocol and anti-pattern rules already apply.

# Init routes to teach mode

Gobby keeps durable product and design context in `.impeccable.md`. The inline
`teach` mode in `SKILL.md` owns that contract, so `init` delegates to it instead
of creating upstream product artifacts.

When `init` is dispatched:

1. Return to `SKILL.md` and follow `## Teach Mode` exactly.
2. Create `.impeccable.md` when it is missing, or update its `## Design Context`
   section in place when it exists.
3. Resume the request that routed here after teach mode finishes; keep its
   confirmed audience, constraints, assets, and design principles loaded.

Do not create `PRODUCT.md`, `DESIGN.md`, or an upstream design sidecar. Managed
CLI setup belongs to `gobby install`; design-context setup belongs to teach mode.
