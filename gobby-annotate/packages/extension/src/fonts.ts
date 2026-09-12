import geist from "@fontsource-variable/geist/files/geist-latin-wght-normal.woff2?inline";
import mono from "@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2?inline";

export function installFonts(): () => void {
  if (typeof FontFace === "undefined") return () => {};
  // Font faces declared inside a shadow stylesheet do not register consistently
  // across browsers. Register bundled bytes explicitly, without global CSS rules.
  const fonts = [
    new FontFace("Geist Variable", `url(${geist})`, { weight: "100 900" }),
    new FontFace("JetBrains Mono Variable", `url(${mono})`, {
      weight: "100 800",
    }),
  ];
  for (const font of fonts) {
    document.fonts.add(font);
    void font
      .load()
      .catch((error) =>
        console.error("Gobby Annotate font failed to load", error),
      );
  }
  return () => {
    for (const font of fonts) document.fonts.delete(font);
  };
}
