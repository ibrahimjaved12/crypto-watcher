import tailwindcss from "@tailwindcss/vite";
import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import viteReact from "@vitejs/plugin-react";
import { nitro } from "nitro/vite";
import { loadEnv, type ConfigEnv, type PluginOption, type UserConfig } from "vite";
import tsConfigPaths from "vite-tsconfig-paths";

import { validateEnvironment } from "./src/lib/environment";

export default async (context: ConfigEnv): Promise<UserConfig> => {
  const env = { ...loadEnv(context.mode, process.cwd(), ""), ...process.env };
  validateEnvironment(env);
  // Vite loads browser values itself; local SSR also needs the server values.
  if (context.command === "serve") {
    for (const [name, value] of Object.entries(env)) {
      if (value !== undefined && process.env[name] === undefined) process.env[name] = value;
    }
  }

  const plugins: PluginOption[] = [];
  if (context.mode === "development") {
    // Dev-only and first, so it can annotate source before other transforms.
    const { devtools } = await import("@tanstack/devtools-vite");
    plugins.push(
      devtools({
        logging: false,
        eventBusConfig: { enabled: false },
        enhancedLogs: { enabled: false },
        consolePiping: { enabled: false },
        removeDevtoolsOnBuild: false,
        injectSource: { enabled: true },
      }),
    );
  }
  plugins.push(
    tailwindcss(),
    tsConfigPaths({ projects: ["./tsconfig.json"] }),
    tanstackStart({
      // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
      // nitro/vite builds from this
      server: { entry: "server" },
      importProtection: {
        behavior: "error",
        client: { files: ["**/server/**"], specifiers: ["server-only"] },
      },
    }),
  );
  // Build-only: Cloudflare Workers output (.output/server/wrangler.json, nodejs_compat, ASSETS).
  if (context.command === "build") plugins.push(nitro({ defaultPreset: "cloudflare-module" }));
  plugins.push(viteReact());

  // Expose VITE_* values to the browser bundle exactly as import.meta.env.
  const envDefine: Record<string, string> = {};
  for (const [key, value] of Object.entries(loadEnv(context.mode, process.cwd(), "VITE_"))) {
    envDefine[`import.meta.env.${key}`] = JSON.stringify(value);
  }

  return {
    define: envDefine,
    ...(context.command === "build" && context.mode === "development"
      ? {
          environments: { client: { define: { "process.env.NODE_ENV": JSON.stringify("development") } } },
        }
      : {}),
    css: { transformer: "lightningcss" },
    resolve: {
      alias: { "@": `${process.cwd()}/src` },
      dedupe: [
        "react",
        "react-dom",
        "react/jsx-runtime",
        "react/jsx-dev-runtime",
        "@tanstack/react-query",
        "@tanstack/query-core",
      ],
    },
    optimizeDeps: {
      include: ["react", "react-dom", "react-dom/client", "react/jsx-runtime", "react/jsx-dev-runtime"],
      ignoreOutdatedRequests: true,
    },
    server: {
      host: "::",
      port: 8080,
      watch: { awaitWriteFinish: { stabilityThreshold: 1000, pollInterval: 100 } },
    },
    plugins,
  };
};
