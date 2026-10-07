// The production build the backend serves (ENGINEERING §3.1: E2E runs against it under the real CSP).
// Everything is bundled into files: no CDN, no inline assets or scripts (T-104, T-106).
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    assetsInlineLimit: 0, // no data: URLs for scripts or styles
    sourcemap: false,
    modulePreload: { polyfill: false },
  },
  server: { host: "127.0.0.1", strictPort: true },
});
