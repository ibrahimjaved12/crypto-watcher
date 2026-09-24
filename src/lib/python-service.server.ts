type Env = Record<string, string | undefined>;

export function pythonServiceConfig(path: string, env: Env = process.env) {
  if (env["PYTHON_ANALYSIS_ENABLED"] !== "true") throw new Error("disabled");
  const token = env["PYTHON_ANALYSIS_TOKEN"] ?? "";
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(token)) throw new Error("token");
  const origin = new URL(env["PYTHON_ANALYSIS_URL"] ?? "");
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname);
  if (
    (origin.protocol !== "https:" && !(origin.protocol === "http:" && local)) ||
    origin.username ||
    origin.password ||
    origin.search ||
    origin.hash ||
    origin.pathname !== "/"
  ) {
    throw new Error("url");
  }
  return { url: new URL(path, origin).toString(), token };
}
