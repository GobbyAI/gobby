# New-work direction

# New visual work

Use this flow when making a new surface or replacing a visual identity. `.impeccable.md` owns product truth and durable visual decisions. A surface brief keeps strategy that belongs only to one route or artifact. When `.impeccable.md` is missing, run the main skill's inline `## Teach Mode` first, then resume this flow.

## 1. Decide what is already true

Read `.impeccable.md`, representative code, tokens, components, and assets.

- **Redesign:** preserve product truth, content, function, constraints, and explicit brand commitments; replace the old visual world rather than polishing it. The old look is evidence of what the subject is, not authority over what it becomes.
- **Established world:** inherit it. A missing `.impeccable.md` does not erase a coherent identity already present in code; document that identity instead of inventing a replacement.
- **Incomplete brand:** preserve confirmed assets and recognizable traits, then help the user expand the system for this new surface.
- **No visual authority:** create a new world with the user.

A section, component, feature, or state inside an established surface inherits that surface. Do not turn a local addition into a new identity exercise.

## 2. Ask what will change the work

Ask one round of two or three related questions through the structured question tool when available. Skip settled facts; a precise request may need only a compact confirmation.

- **Persuade:** clarify who must act, what they should believe, and which real proof, content, or assets can earn that belief.
- **Operate:** clarify the task, information, important states, frequency, and constraints.
- **Read:** clarify the reader's question, source material, structure, and wayfinding.
- **Experience:** clarify what leads, how exploration unfolds, and which interaction or transition matters.

Across modes, ask what success looks like, what must remain untouched, and what would make a polished result feel wrong. Do not ask for CSS values or canned aesthetic lanes.

## 3. Choose the right amount of invention

### Extend an existing surface

Inherit its world and composition. Resolve only the new purpose, content, hierarchy, states, interaction, and how the addition joins the surrounding experience. Do not run a concept tournament or change `.impeccable.md` unless the user approves a durable system change.

### Create a whole surface inside an established world

Keep the visual system fixed. Derive five to seven materially different structures from the content, task, and user behavior, ordered by resonance. For a genuinely open whole page, screen, or flow, run:

`node <scripts_dir>/concept-seed.mjs --scope surface --mode <mode>`

Resolve `<scripts_dir>` by calling `materialize_skill_scripts(name="impeccable")` on `gobby-skills`; it returns the absolute path of the skill's materialized `scripts/` directory. Export the returned `environment.PUPPETEER_CACHE_DIR` before any browser-engine invocation. If the tool or Node is unavailable, skip detector runs and scan manually.

The script assigns which structure gets built; your top-ranked structure is what every run would ship, so the dice come from outside. Never run the script for a local extension or a precisely specified narrow request; shape those directly.

### Create or replace the visual world

When creating or replacing the visual world, call `get_skill_file(name="impeccable", path="references/new-work-invention.md")`. Complete its direction roll and user decision before committing the world below.

## 4. Commit the world

Pick a color strategy before picking colors: Restrained (neutrals plus one accent; the default when the visitor came to operate or read), Committed (one saturated color carries 30-60% of the surface), Full palette (3-4 named roles), or Drenched (the surface IS the color). Persuade and Experience surfaces have permission for the bolder strategies; take them when the brief allows. Color commits at page scale: fields that own whole regions, not accents scattered over a neutral ground. Dark or light is never a default: write one sentence of physical scene (who uses this, where, under what light) and let it force the answer.

Choose faces like objects from the subject's world, in the mode's register. Operate and Read surfaces are well served by system stacks and workhorse UI faces; Persuade and Experience surfaces want faces with a point of view, and these training-data defaults mean you stopped looking: Fraunces, Playfair Display, Cormorant, Lora, Crimson, Newsreader, Syne, Space Grotesk, Space Mono, IBM Plex, Inter-as-display, DM Sans, DM Serif, Outfit, Plus Jakarta Sans, Instrument Sans. Naming one of these faces anyway requires a reason no other face could satisfy, and a subject association is never that reason: books wanting a serif, bookshops wanting hand-lettering, and tech wanting a mono are the associations the list exists to break.

Calibration: AI-generated interfaces cluster around a few looks regardless of subject: warm cream ground, high-contrast serif display, and a terracotta or signal-red accent; near-black with one neon accent and glowing edges; broadsheet-editorial hairlines, italic display serif, and small tracked mono labels. All are legitimate when the brief calls for them. Where the brief leaves the aesthetic free, landing in one means the self-check failed: if someone could guess your aesthetic from the category alone, or from category-plus-avoidance, rework until neither answer is obvious. Energy is not the enemy of trust: a brief's negative constraints (no gamification, no hype) rule out those devices, not exuberance, and adjectives describing the product's behavior (quiet support, calm coaching) do not dictate the surface's energy. A bookish, warm, or child-facing subject does not soften the calibration: book cloth, thread, jackets, endpapers, and shelf ephemera span the whole saturated spectrum, and cream paper is the smallest corner of that world; landing on cream plus serif for a book subject is the default wearing the subject's clothes. A brief-pinned world pins the world, not its softest rendition: the pinned world's full material range stays in play, and a rendition that matches what any model ships for that world failed the self-check at execution rather than selection.

## 5. Record the decision

Before code, state the chosen direction as a contract in the artifact's opening comment, five short blocks, 150 words at most, in a form that survives the production build: an HTML comment in the emitted markup, never only a templating-frontmatter comment, placed as the first child of the document's body in the root layout, never inside a slotted or child component (some compilers, Astro among them, strip a slot's leading comment while keeping deeper ones). After the first production build, grep the built output for the seed key; a contract the build erased is a contract nobody can audit. THESIS: the one idea this surface owns and the category-default arrangement it refuses. OWN-WORLD: the palette and component language, specific enough to be recognizable with all content removed. STORY: what the visitor understands, believes, and does. FIRST VIEWPORT: the exact composition, what is where and at what scale, and where the primary action sits. FORM: the chosen form, its position on your ordered list, and the seed key the script printed. Close the comment with one more line, FINISH: the run's exit condition, verbatim "unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and `.impeccable.md`". The comment tops the artifact you re-open on every edit, the one reminder that survives a long build: a page that looks complete with the FINISH line undischarged is not done, it is abandoned at the finish line. If a block reads like a mood, the direction is not decided yet; the finishing review audits the render against this contract.

On a new or replacement world, `.impeccable.md` is written at finish, from the built world, by the shipped documenter (section 7); a rulebook written before the build gets defended against reality instead of describing it, and hands the design-system detector an unstable target. A new world shipped with no `.impeccable.md` is still an incomplete run. An ordinary extension does not rewrite `.impeccable.md`.

If the work establishes durable strategy for a route or artifact, read its existing surface brief, then update it:

`node <scripts_dir>/surface-brief.mjs read <primary-target>`

`node <scripts_dir>/surface-brief.mjs write <primary-target> <body-file> [related-target ...]`

Keep the brief small: scope and visitor mode; audience, job, action/task, proof/content, and constraints; chosen direction and memorable moment; unresolved decisions. Do not copy global product truth or `.impeccable.md` tokens into it.

Whenever image generation is available in the current session, the locked direction is visualized before it is built: call `get_skill_file(name="impeccable", path="references/visualize.md")` on `gobby-skills` and follow it, with three compositional options rendered and put before the user for approval. This step is proven to produce the most compositional and ambitious work.

For `shape`, call `get_skill_file(name="impeccable", path="references/shape.md")` on `gobby-skills`, return the selected direction to that flow, and stop before persistence or implementation.
