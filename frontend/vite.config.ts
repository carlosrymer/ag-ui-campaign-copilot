import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// GitHub Pages serves this project site from /<repo>/, so the base path must match.
// Override with BASE_PATH=/ when serving from a domain root.
const base = process.env.BASE_PATH ?? "/ag-ui-campaign-copilot/";

export default defineConfig({
  plugins: [react()],
  base,
  build: { outDir: "dist", sourcemap: false },
});
