export const PRIORITY_GLYPH_PATHS: Record<number, string> = {
  0: "M5 1.5 1.5 6h7zM5 5.5 1.5 10h7z", // Critical: double up
  1: "M5 3 1.5 7.5h7z", // High: up
  2: "M2 4h6M2 7h6", // Medium: two bars
  3: "M1.5 3.5h7L5 8z", // Low: down
  4: "M2.5 5.5h5", // Backlog: muted dash
};
