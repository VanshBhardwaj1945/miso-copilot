import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: the UI fetches "/ask" and Vite forwards it to the FastAPI
// backend, so no CORS config is needed anywhere. VITE_BACKEND_URL lets
// docker-compose point at the backend container; bare `npm run dev` still
// defaults to the local server.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ask": process.env.VITE_BACKEND_URL || "http://127.0.0.1:8000",
      "/crosswalk.csv": process.env.VITE_BACKEND_URL || "http://127.0.0.1:8000",
    },
  },
});
