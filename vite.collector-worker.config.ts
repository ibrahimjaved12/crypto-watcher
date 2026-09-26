import { defineConfig } from "vite";

// Host-independent production bundle for the persistent collector worker.
// It runs with plain `node` and needs no devDependencies (Vite is build-time only).
export default defineConfig({
  build: {
    ssr: "src/worker/collector-worker.ts",
    outDir: "dist/collector-worker",
    emptyOutDir: true,
    rollupOptions: {
      output: { entryFileNames: "collector-worker.mjs" },
    },
  },
  ssr: { noExternal: true },
});
