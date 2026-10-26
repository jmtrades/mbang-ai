import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/",
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  server: { port: 5173, proxy: { "/t": "http://127.0.0.1:8850", "/health": "http://127.0.0.1:8850" } },
});
