import { expect, it } from "vitest";
import { scopeStyles } from "../src/styles";

it.each(['"light"', "'light'", "light"])(
  "scopes compiled theme selectors (%s) without nesting existing hosts",
  (value) => {
    const css = `:root{--bg:dark}[data-theme=${value}]{--bg:light}:host([data-theme="light"]){color-scheme:light}`;
    const result = scopeStyles(css);
    expect(result).toContain(
      ':host{--bg:dark}:host([data-theme="light"]){--bg:light}',
    );
    expect(result).toContain(':host([data-theme="light"]){color-scheme:light}');
    expect(result).not.toContain(":host(:host(");
  },
);
