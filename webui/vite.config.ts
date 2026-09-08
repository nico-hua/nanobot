import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // The Python runtime and local frontend share the repository-root .env.
  // Vite exposes only variables prefixed with VITE_ to browser code.
  envDir: "..",
  cacheDir: ".vite",
  plugins: [react()],
});
