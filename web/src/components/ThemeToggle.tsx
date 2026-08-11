import { LightbulbIcon, MoonIcon } from "./icons";
import { cn } from "../lib/utils";
import { useResolvedTheme } from "../hooks/useResolvedTheme";
import type { Theme } from "../hooks/useSettings";
import { Button } from "./ui/Button";
import { coarseHitAreaCls } from "./ui/controlStyles";

interface ThemeToggleProps {
  /** Current theme setting ('dark' | 'light' | 'system'). */
  theme: Theme;
  /** Persisted setter from useSettings — writes localStorage + API. */
  onThemeChange: (theme: Theme) => void;
  disabled?: boolean;
}

/**
 * Dual-state theme toggle for the header. Glyph-only, sized to match the
 * collapsed New Chat / Toggle Panel buttons via the shared accent icon
 * Button. The icon shows the *destination*:
 * dark mode shows a light bulb (click → light), light mode a moon (click → dark).
 * It reads the resolved theme so a `system` setting still flips to an
 * explicit choice, and persists through the settings store rather than
 * touching `data-theme` directly.
 */
export function ThemeToggle({ theme, onThemeChange, disabled = false }: ThemeToggleProps) {
  // `theme` is accepted for API symmetry with other settings-driven controls;
  // the rendered glyph follows what's actually on screen (resolves `system`).
  void theme;
  const resolved = useResolvedTheme();
  const isDark = resolved === "dark";
  const next: Theme = isDark ? "light" : "dark";
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <Button
      type="button"
      variant="accent"
      size="icon"
      dense
      className={cn("shrink-0", coarseHitAreaCls)}
      disabled={disabled}
      aria-label={label}
      title={label}
      onClick={() => {
        if (disabled) return;
        onThemeChange(next);
      }}
    >
      {isDark ? <LightbulbIcon /> : <MoonIcon />}
    </Button>
  );
}
