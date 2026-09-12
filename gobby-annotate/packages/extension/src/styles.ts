export function scopeStyles(css: string): string {
  // Shadow stylesheets do not register @property consistently. Materialize
  // Tailwind's non-inheriting initial values locally so borders/rings work.
  const defaults = [
    ...css.matchAll(
      /@property\s+(--[\w-]+)\s*\{[^}]*?initial-value:\s*([^;}]+)[^}]*\}/g,
    ),
  ]
    .map((match) => `${match[1]}:${match[2]};`)
    .join("");
  return (
    (
      css
        .replaceAll(":root", ":host")
        .replace(
          /(^|[{},]\s*)\[data-theme=(?:"light"|'light'|light)\]/g,
          '$1:host([data-theme="light"])',
        ) + `@layer properties{:host,*,::before,::after{${defaults}}}`
    )
      // rem otherwise follows the annotated page's root size, outside our shadow.
      .replace(
        /(-?\d*\.?\d+)rem\b/g,
        (_, value: string) => `${Number(value) * 16}px`,
      )
  );
}
