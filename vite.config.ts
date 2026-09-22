// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

import { loadEnv } from "vite";
import { validateEnvironment } from "./src/lib/environment";

const config = defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },
  },
});

export default async (context: import("vite").ConfigEnv) => {
  const env = { ...loadEnv(context.mode, process.cwd(), ""), ...process.env };
  validateEnvironment(env);
  // Vite loads browser values itself; local SSR also needs the server values.
  if (context.command === "serve") {
    for (const [name, value] of Object.entries(env)) {
      if (value !== undefined && process.env[name] === undefined) process.env[name] = value;
    }
  }
  return config(context);
};
