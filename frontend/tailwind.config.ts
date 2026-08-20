import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // FYF AI Brand Colors
        "fyf-ivory": "#F4F0E6",   // Warm Ivory - Main Background
        "fyf-olive": "#30382C",   // Olive Ink - Text & Structure
        "fyf-viridian": "#16856B", // Viridian - Primary Action
        "fyf-sage": "#A8B7A2",    // Soft Sage - Secondary/Support
      },
      fontFamily: {
        sans: ["Arial", "Helvetica", "sans-serif"],
      },
    },
  },
  plugins: [],
};
export default config;
