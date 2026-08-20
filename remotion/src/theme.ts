// theme.ts — single source of truth for the FYF Remotion project.
// FYF Brand colors + premium easing curves. NEVER inline these in components.
import { Easing } from "remotion";

export const theme = {
  colors: {
    bg: "#F4F0E6", // Warm Ivory — main canvas
    bgAlt: "#FFFFFF", // White cards/surfaces
    primary: "#16856B", // Viridian — THE hero color (max one element per frame)
    accent: "#A8B7A2", // Soft Sage — secondary
    alert: "#C8583D", // Muted coral — warnings and consequences only
    text: "#30382C", // Olive Ink — body text
    textDim: "#30382C99", // Olive Ink at 60% — dim text
    glow: "rgba(22, 133, 107, 0.35)", // Viridian glow
  },
  fonts: {
    display: "'Noto Sans Myanmar', Arial, 'Helvetica Neue', sans-serif",
    body: "'Noto Sans Myanmar', Arial, 'Helvetica Neue', sans-serif",
    mono: "'JetBrains Mono', monospace",
  },
  // THE easing curves. Linear is forbidden.
  ease: {
    out: Easing.bezier(0.16, 1, 0.3, 1), // easeOutExpo — entrances
    inOut: Easing.bezier(0.83, 0, 0.17, 1), // easeInOutQuint — moves, Ken Burns
    in: Easing.bezier(0.7, 0, 0.84, 0), // exits only
  },
  spring: {
    snappy: { damping: 14, stiffness: 160, mass: 0.6 }, // UI pops, words
    smooth: { damping: 20, stiffness: 90, mass: 1 }, // big elements
    bouncy: { damping: 11, stiffness: 170, mass: 0.7 }, // playful accents
  },
} as const;
