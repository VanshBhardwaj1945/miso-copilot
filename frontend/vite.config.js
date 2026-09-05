import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: the UI fetches "/ask" and Vite forwards it to the FastAPI
// backend on :8000, so no CORS config is needed anywhere.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // "/ask": "http://localhost:8000",
      "/ask": "http://127.0.0.1:8000"
    },
  },
});
