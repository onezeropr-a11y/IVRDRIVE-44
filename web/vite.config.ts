import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs so the same bundle serves both from the static
  // service root and from the backend's /console/ proxy path.
  base: "./",
  server: {
    // Local development runs the backend on 8000; production points
    // VITE_API_BASE at the Render backend service instead.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
