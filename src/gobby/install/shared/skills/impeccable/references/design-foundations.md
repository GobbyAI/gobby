# Design foundations

## Design Direction

Commit to a bold aesthetic direction:
- **Purpose**: What problem does this interface solve? Who uses it?
- **Tone**: Pick an extreme: brutally minimal, maximalist chaos, retro-futuristic, organic/natural, luxury/refined, playful/toy-like, editorial/magazine, brutalist/raw, art deco/geometric, soft/pastel, industrial/utilitarian, etc. There are so many flavors to choose from. Use these for inspiration but design one that is true to the aesthetic direction.
- **Constraints**: Technical requirements (framework, performance, accessibility).
- **Differentiation**: What makes this unforgettable? What's the one thing someone will remember?

Choose a clear conceptual direction and execute it with precision. Bold maximalism and refined minimalism both work; the key is intentionality rather than intensity.

Then implement working code that is:
- Production-grade and functional
- Visually striking and memorable
- Cohesive with a clear aesthetic point-of-view
- Meticulously refined in every detail

## Frontend Aesthetics Guidelines

### Typography

→ *Consult [typography reference](references/typography.md) for OpenType features, web font loading, and the deeper material on scales.*

Choose fonts that are beautiful, unique, and interesting. Pair a distinctive display font with a refined body font.

<typography_principles>
Always apply these — do not consult a reference, just do them:

- Use a modular type scale with fluid sizing (clamp) for headings on marketing/content pages. Use fixed `rem` scales for app UIs and dashboards (no major design system uses fluid type in product UI).
- Use fewer sizes with more contrast. A 5-step scale with at least a 1.25 ratio between steps creates clearer hierarchy than 8 sizes that are 1.1× apart.
- Line-height scales inversely with line length. Narrow columns want tighter leading, wide columns want more. For light text on dark backgrounds, add 0.05-0.1 to your normal line-height — light type reads as lighter weight and needs more breathing room.
- Cap line length at ~65-75ch. Body text wider than that is fatiguing.
</typography_principles>

<font_selection_procedure>
Run this procedure before typing any font name.

The model's natural failure mode is "I was told not to use Inter, so I will pick my next favorite font, which becomes the new monoculture." Avoid this by performing the following procedure on every project, in order:

Step 1. Read the brief once. Write down 3 concrete words for the brand voice (e.g., "warm and mechanical and opinionated", "calm and clinical and careful", "fast and dense and unimpressed", "handmade and a little weird"). Skip "modern" and "elegant" — those are dead categories.

Step 2. List the 3 fonts you would normally reach for given those words. Write them down. They are most likely from this list:

<reflex_fonts_to_reject>
Fraunces
Newsreader
Lora
Crimson
Crimson Pro
Crimson Text
Playfair Display
Cormorant
Cormorant Garamond
Syne
IBM Plex Mono
IBM Plex Sans
IBM Plex Serif
Space Mono
Space Grotesk
Inter
DM Sans
DM Serif Display
DM Serif Text
Outfit
Plus Jakarta Sans
Instrument Sans
Instrument Serif
</reflex_fonts_to_reject>

Reject every font that appears in the reflex_fonts_to_reject list. They are your training-data defaults and they create monoculture across projects.

Step 3. Browse a font catalog with the 3 brand words in mind. Sources: Google Fonts, Pangram Pangram, Future Fonts, Adobe Fonts, ABC Dinamo, Klim Type Foundry, Velvetyne. Look for something that fits the brand as a *physical object* — a museum exhibit caption, a hand-painted shop sign, a 1970s mainframe terminal manual, a fabric label on the inside of a coat, a children's book printed on cheap newsprint. Reject the first thing that "looks designy" — that's the trained reflex too. Keep looking.

Step 4. Cross-check the result. The right font for an "elegant" brief is not necessarily a serif. The right font for a "technical" brief is not necessarily a sans-serif. The right font for a "warm" brief is not Fraunces. If your final pick lines up with your reflex pattern, go back to Step 3.
</font_selection_procedure>

<typography_rules>
Use a modular type scale with fluid sizing (clamp) for marketing/content
headings. Use fixed rem scales for app UI and dashboard headings.
Vary font weights and sizes to create clear visual hierarchy.
Vary your font choices across projects. If you used a serif display font on the last project, look for a sans, monospace, or display face on this one.

Skip overused fonts like Inter, Roboto, Arial, Open Sans, and system defaults — and skip your second-favorite too: every font in the reflex_fonts_to_reject list above is banned. Look further.
Avoid monospace typography as lazy shorthand for "technical/developer" vibes.
Avoid large icons with rounded corners above every heading; they rarely add value and make sites look templated.
Avoid setting the whole page in one font family. Pair a distinctive display font with a refined body font.
Avoid a flat type hierarchy where sizes are too close together. Aim for at least a 1.25 ratio between steps.
Avoid long body passages in uppercase. Reserve all-caps for short labels and headings.
</typography_rules>

### Color & Theme

→ *Consult [color reference](references/color-and-contrast.md) for the deeper material on contrast, accessibility, and palette construction.*

Commit to a cohesive palette. Dominant colors with sharp accents outperform timid, evenly-distributed palettes.

<color_principles>
Always apply these — do not consult a reference, just do them:

- Use OKLCH, not HSL. OKLCH is perceptually uniform: equal steps in lightness *look* equal, which HSL does not deliver. As you move toward white or black, reduce chroma — high chroma at extreme lightness looks garish. A light blue at 85% lightness wants ~0.08 chroma, not the 0.15 of your base color.
- Tint your neutrals toward your brand hue. Even a chroma of 0.005-0.01 is perceptible and creates subconscious cohesion between brand color and UI surfaces. The hue you tint toward should come from THIS brand, not from a "warm = friendly" or "cool = tech" formula. Pick the brand's actual hue first, then tint everything toward it.
- The 60-30-10 rule is about visual *weight*, not pixel count. 60% neutral / surface, 30% secondary text and borders, 10% accent. Accents work because they're rare. Overuse kills their power.
</color_principles>

<theme_selection>
Derive the theme (light vs dark) from audience and viewing context rather than picking a default. Read the brief and ask: when is this product used, by whom, in what physical setting?

- A perp DEX consumed during fast trading sessions → dark
- A hospital portal consumed by anxious patients on phones late at night → light
- A children's reading app → light
- A vintage motorcycle forum where users sit in their garage at 9pm → dark
- An observability dashboard for SREs in a dark office → dark
- A wedding planning checklist for couples on a Sunday morning → light
- A music player app for headphone listening at night → dark
- A food magazine homepage browsed during a coffee break → light

Do not default everything to light "to play it safe." Do not default everything to dark "to look cool." Both defaults are the lazy reflex. The correct theme is the one the actual user wants in their actual context.
</theme_selection>

<color_rules>
Use modern CSS color functions (oklch, color-mix, light-dark) for perceptually uniform, maintainable palettes.
Tint your neutrals toward your brand hue. Even a subtle hint creates subconscious cohesion.

Avoid gray text on colored backgrounds; it looks washed out. Use a shade of the background color instead.
Avoid pure black (#000) and pure white (#fff). Always tint; pure black/white never appears in nature.
Avoid the AI color palette: cyan-on-dark, purple-to-blue gradients, neon accents on dark backgrounds.
Avoid gradient text for impact — see <absolute_bans> below for the strict definition. Solid colors only for text.
Avoid defaulting to dark mode with glowing accents. It looks "cool" without requiring actual design decisions.
Avoid defaulting to light mode "to be safe" either. The point is to choose, not to retreat to a safe option.
</color_rules>

When layout, visual detail, motion, interaction, responsive behavior, UX writing, implementation, or the AI-slop check is in scope, call `get_skill_file(name="impeccable", path="references/design-execution.md")`.
