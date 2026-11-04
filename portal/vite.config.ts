import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the portal proxies /api to the FastAPI server, so there is no CORS
// and the same relative URLs work when the API serves the built portal in production.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    proxy: { "/api": `http://127.0.0.1:${process.env.ALERTBOT_API_PORT ?? "8000"}` },
  },
});
