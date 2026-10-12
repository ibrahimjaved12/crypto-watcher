import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { createRateLimiter, secretMatches, validateGatewayEnv } from "../lib/runner/gateway-core";
import { UnauthorizedError, verifyUserToken } from "../lib/runner/verify-user.server";
import { analyzeForUser } from "../lib/analysis.server";
import { analysisInput } from "../lib/analysis.contract";
import { runManualMonitorCheck } from "../lib/monitor/manual-check.server";
import { supabaseAdmin } from "../integrations/supabase/client.server";

const env = process.env;
if (validateGatewayEnv(env).length) {
  console.error("internal startup");
  process.exit(1);
}
const secret = env["RUNNER_GATEWAY_SECRET"]!;
const port = Number(env["RUNNER_GATEWAY_PORT"] ?? "8787");
const analysisLimit = createRateLimiter({ windowMs: 60_000, max: 12 }, Date.now);
const checkLimit = createRateLimiter({ windowMs: 60_000, max: 6 }, Date.now);
const checking = new Set<string>();
const BODY_CAP = 4096;

class RequestError extends Error {
  constructor(readonly status: number, readonly category: string, message: string) { super(message); }
}
function json(response: ServerResponse, status: number, body: unknown) {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(body));
}
async function readBody(request: IncomingMessage): Promise<unknown> {
  const length = request.headers["content-length"];
  if (length !== undefined && Number(length) > BODY_CAP)
    throw new RequestError(413, "bad_request", "Request body is too large.");
  const chunks: Buffer[] = [];
  let size = 0;
  // Event listeners allow a 413 response without the async iterator destroying the socket.
  return new Promise((resolve, reject) => {
    request.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > BODY_CAP) {
        chunks.length = 0;
        reject(new RequestError(413, "bad_request", "Request body is too large."));
      } else chunks.push(chunk);
    });
    request.on("end", () => {
      if (size > BODY_CAP) return;
      if (!size) return resolve(undefined);
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
      catch { reject(new RequestError(400, "bad_request", "Invalid JSON body.")); }
    });
    request.on("error", () => reject(new RequestError(400, "bad_request", "Invalid request body.")));
    request.on("aborted", () => reject(new RequestError(400, "bad_request", "Incomplete request body.")));
  });
}

let activeRequests = 0;
let listenerClosed = false;
let shutdownDeadline: ReturnType<typeof setTimeout> | undefined;
function finishShutdown() {
  if (listenerClosed && activeRequests === 0) {
    clearTimeout(shutdownDeadline);
    process.exit(0);
  }
}

const server = createServer(async (request, response) => {
  activeRequests++;
  // Never put arbitrary request URLs or headers into logs.
  const route = ["/health", "/v1/analysis", "/v1/monitor/check"].includes(request.url ?? "")
    ? request.url! : "unknown";
  try {
    if (request.method === "GET" && route === "/health") return json(response, 200, { ok: true });
    if (request.method !== "POST" || !["/v1/analysis", "/v1/monitor/check"].includes(route))
      throw new RequestError(400, "bad_request", "Unsupported route or method.");
    const provided = request.headers["x-runner-secret"];
    const authorization = request.headers.authorization;
    if (typeof provided !== "string" || !secretMatches(provided, secret) || !authorization?.startsWith("Bearer "))
      throw new UnauthorizedError();
    const contentType = request.headers["content-type"]?.split(";")[0]?.trim().toLowerCase();
    const hasBody = request.headers["transfer-encoding"] !== undefined || Number(request.headers["content-length"] ?? 0) > 0;
    if ((contentType !== undefined && contentType !== "application/json") ||
        ((hasBody || route === "/v1/analysis") && contentType !== "application/json"))
      throw new RequestError(400, "bad_request", "Expected application/json.");
    const body = await readBody(request);
    const parsed = route === "/v1/analysis" ? analysisInput.safeParse(body) : undefined;
    if (parsed && !parsed.success) throw new RequestError(400, "bad_request", "Invalid analysis request.");
    const { supabase, userId } = await verifyUserToken(authorization.slice(7), env);
    if (route === "/v1/monitor/check" && checking.has(userId))
      throw new RequestError(409, "busy", "A check is already running.");
    const limit = (route === "/v1/analysis" ? analysisLimit : checkLimit)(userId);
    if (!limit.ok) {
      response.setHeader("Retry-After", String(limit.retryAfterSeconds));
      throw new RequestError(429, "rate_limited", "Too many requests.");
    }
    if (parsed?.success) {
      return json(response, 200, await analyzeForUser(supabase, userId, parsed.data.symbol));
    }
    checking.add(userId);
    try {
      const result = await runManualMonitorCheck(supabase, supabaseAdmin, userId);
      json(response, 200, { ...result, error: result.error ? result.error.slice(0, 1000) : result.error });
    } finally { checking.delete(userId); }
  } catch (error) {
    const failure = error instanceof UnauthorizedError
      ? new RequestError(401, "unauthorized", "Unauthorized.")
      : error instanceof RequestError ? error : new RequestError(500, "internal", "Request failed.");
    if (failure.category === "internal") {
      console.error(failure.category, route,
        error instanceof Error ? error.name + ": " + error.message : "non-error thrown");
    } else {
      console.error(failure.category, route);
    }
    // Do not keep unread rejected bodies on a reusable connection.
    if (!request.complete) response.setHeader("Connection", "close");
    json(response, failure.status, { ok: false, category: failure.category, error: failure.message });
    request.resume();
  } finally {
    activeRequests--;
    finishShutdown();
  }
});
server.on("error", () => { console.error("internal startup"); process.exit(1); });
server.listen(port, "127.0.0.1", () => console.log(`Runner gateway listening on port ${port}`));
let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true;
  shutdownDeadline = setTimeout(() => { server.closeAllConnections(); process.exit(0); }, 10_000);
  server.close(() => { listenerClosed = true; finishShutdown(); });
  server.closeIdleConnections();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
