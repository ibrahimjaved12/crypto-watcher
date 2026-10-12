/* TEMPORARY: delete after probe. */ import { createFileRoute } from "@tanstack/react-router";
/* TEMPORARY: delete after probe. */ // Delete this entire diagnostic route after recording the answer on discussion #280.
/* TEMPORARY: delete after probe. */ type WorkerSocket = WebSocket & { accept(): void };
/* TEMPORARY: delete after probe. */ type WorkerRequest = Request & { cf?: { colo?: string; country?: string } };
/* TEMPORARY: delete after probe. */ const KLINES = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit=1";
/* TEMPORARY: delete after probe. */ const RESPONSE_HEADERS = ["content-type", "server", "via", "x-cache", "cf-ray"];
/* TEMPORARY: delete after probe. */ async function handle(request: WorkerRequest): Promise<Response> {
/* TEMPORARY: delete after probe. */   const noStore = { "cache-control": "no-store" };
/* TEMPORARY: delete after probe. */   try {
/* TEMPORARY: delete after probe. */     // Runtime Worker secrets only; no build-time values or secret-bearing diagnostics.
/* TEMPORARY: delete after probe. */     const token = process.env["PROBE_TOKEN"];
/* TEMPORARY: delete after probe. */     const supplied = request.headers.get("x-probe-token");
/* TEMPORARY: delete after probe. */     if (!token || !supplied) return new Response(null, { status: 404, headers: noStore });
/* TEMPORARY: delete after probe. */     // Hash both inputs to fixed-length digests before the constant-time comparison.
/* TEMPORARY: delete after probe. */     const { createHash, timingSafeEqual } = await import("node:crypto");
/* TEMPORARY: delete after probe. */     const digest = (value: string) => createHash("sha256").update(value).digest();
/* TEMPORARY: delete after probe. */     if (!timingSafeEqual(digest(token), digest(supplied))) {
/* TEMPORARY: delete after probe. */       return new Response(null, { status: 404, headers: noStore });
/* TEMPORARY: delete after probe. */     }
/* TEMPORARY: delete after probe. */     const key = process.env["BINANCE_PROBE_API_KEY"];
/* TEMPORARY: delete after probe. */     const strip = (value: string) => value.replace(/\p{Cc}/gu, "");
/* TEMPORARY: delete after probe. */     const clean = (value: string): string => {
/* TEMPORARY: delete after probe. */       // Redact before truncation, including control-stripped forms of either secret.
/* TEMPORARY: delete after probe. */       for (const secret of [token, key]) {
/* TEMPORARY: delete after probe. */         if (secret) value = value.split(secret).join("[redacted]");
/* TEMPORARY: delete after probe. */       }
/* TEMPORARY: delete after probe. */       value = strip(value);
/* TEMPORARY: delete after probe. */       for (const secret of [token, key]) {
/* TEMPORARY: delete after probe. */         if (secret && strip(secret)) value = value.split(strip(secret)).join("[redacted]");
/* TEMPORARY: delete after probe. */       }
/* TEMPORARY: delete after probe. */       return Array.from(value).slice(0, 200).join("");
/* TEMPORARY: delete after probe. */     };
/* TEMPORARY: delete after probe. */     const probe = async (url: string, websocket = false, apiKey?: string) => {
/* TEMPORARY: delete after probe. */       const controller = new AbortController();
/* TEMPORARY: delete after probe. */       // One six-second deadline covers fetch, body/message reading, and socket cleanup.
/* TEMPORARY: delete after probe. */       const timer = setTimeout(() => controller.abort(), 6000);
/* TEMPORARY: delete after probe. */       const started = Date.now();
/* TEMPORARY: delete after probe. */       let socket: WorkerSocket | undefined;
/* TEMPORARY: delete after probe. */       let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
/* TEMPORARY: delete after probe. */       const result: {
/* TEMPORARY: delete after probe. */         url: string; status: number | null; "content-type": string | null;
/* TEMPORARY: delete after probe. */         body: string; headers: Record<string, string>; error?: string;
/* TEMPORARY: delete after probe. */         connected?: boolean; time_to_first_message_ms?: number | null;
/* TEMPORARY: delete after probe. */         first_message?: string | null;
/* TEMPORARY: delete after probe. */       } = { url, status: null, "content-type": null, body: "", headers: {} };
/* TEMPORARY: delete after probe. */       if (websocket) Object.assign(result, { connected: false, time_to_first_message_ms: null, first_message: null });
/* TEMPORARY: delete after probe. */       try {
/* TEMPORARY: delete after probe. */         const headers: Record<string, string> = websocket ? { Upgrade: "websocket" } : { accept: "application/json" };
/* TEMPORARY: delete after probe. */         if (apiKey) headers["X-MBX-APIKEY"] = apiKey;
/* TEMPORARY: delete after probe. */         // Fixed URLs and manual redirects prevent forwarding the API key elsewhere.
/* TEMPORARY: delete after probe. */         const response = await fetch(url, { headers, redirect: "manual", signal: controller.signal });
/* TEMPORARY: delete after probe. */         result.status = response.status;
/* TEMPORARY: delete after probe. */         for (const name of RESPONSE_HEADERS) {
/* TEMPORARY: delete after probe. */           const value = response.headers.get(name);
/* TEMPORARY: delete after probe. */           if (value !== null) {
/* TEMPORARY: delete after probe. */             if (name === "content-type") result["content-type"] = clean(value);
/* TEMPORARY: delete after probe. */             else result.headers[name] = clean(value);
/* TEMPORARY: delete after probe. */           }
/* TEMPORARY: delete after probe. */         }
/* TEMPORARY: delete after probe. */         socket = (response as Response & { webSocket?: WorkerSocket }).webSocket;
/* TEMPORARY: delete after probe. */         if (websocket && socket && response.status === 101) {
/* TEMPORARY: delete after probe. */           const ws = socket;
/* TEMPORARY: delete after probe. */           await new Promise<void>((resolve) => {
/* TEMPORARY: delete after probe. */             const finish = (error?: string) => {
/* TEMPORARY: delete after probe. */               if (error) result.error = error;
/* TEMPORARY: delete after probe. */               ws.removeEventListener("message", message);
/* TEMPORARY: delete after probe. */               ws.removeEventListener("error", failed);
/* TEMPORARY: delete after probe. */               ws.removeEventListener("close", closed);
/* TEMPORARY: delete after probe. */               controller.signal.removeEventListener("abort", aborted);
/* TEMPORARY: delete after probe. */               resolve();
/* TEMPORARY: delete after probe. */             };
/* TEMPORARY: delete after probe. */             const message = (event: MessageEvent) => {
/* TEMPORARY: delete after probe. */               result.time_to_first_message_ms = Date.now() - started;
/* TEMPORARY: delete after probe. */               const text = typeof event.data === "string" ? event.data
/* TEMPORARY: delete after probe. */                 : event.data instanceof ArrayBuffer ? new TextDecoder().decode(event.data) : "[non-text message]";
/* TEMPORARY: delete after probe. */               result.first_message = clean(text);
/* TEMPORARY: delete after probe. */               finish();
/* TEMPORARY: delete after probe. */             };
/* TEMPORARY: delete after probe. */             const failed = () => finish("websocket error");
/* TEMPORARY: delete after probe. */             const closed = () => finish("closed before first message");
/* TEMPORARY: delete after probe. */             const aborted = () => finish("timeout");
/* TEMPORARY: delete after probe. */             ws.addEventListener("message", message);
/* TEMPORARY: delete after probe. */             ws.addEventListener("error", failed);
/* TEMPORARY: delete after probe. */             ws.addEventListener("close", closed);
/* TEMPORARY: delete after probe. */             controller.signal.addEventListener("abort", aborted, { once: true });
/* TEMPORARY: delete after probe. */             try {
/* TEMPORARY: delete after probe. */               ws.accept();
/* TEMPORARY: delete after probe. */               result.connected = true;
/* TEMPORARY: delete after probe. */               if (controller.signal.aborted) aborted();
/* TEMPORARY: delete after probe. */             } catch {
/* TEMPORARY: delete after probe. */               finish("websocket accept failed");
/* TEMPORARY: delete after probe. */             }
/* TEMPORARY: delete after probe. */           });
/* TEMPORARY: delete after probe. */         } else {
/* TEMPORARY: delete after probe. */           // Bound memory while preserving enough text to redact secrets before slicing.
/* TEMPORARY: delete after probe. */           reader = response.body?.getReader();
/* TEMPORARY: delete after probe. */           if (reader) {
/* TEMPORARY: delete after probe. */             const decoder = new TextDecoder();
/* TEMPORARY: delete after probe. */             let text = "";
/* TEMPORARY: delete after probe. */             let bytes = 0;
/* TEMPORARY: delete after probe. */             while (bytes < 65536) {
/* TEMPORARY: delete after probe. */               const chunk = await reader.read();
/* TEMPORARY: delete after probe. */               if (chunk.done) break;
/* TEMPORARY: delete after probe. */               bytes += chunk.value.byteLength;
/* TEMPORARY: delete after probe. */               text += decoder.decode(chunk.value, { stream: true });
/* TEMPORARY: delete after probe. */               if (Array.from(strip(text)).length >= 200 + token.length + (key?.length ?? 0)) break;
/* TEMPORARY: delete after probe. */             }
/* TEMPORARY: delete after probe. */             result.body = clean(text + decoder.decode());
/* TEMPORARY: delete after probe. */           }
/* TEMPORARY: delete after probe. */           if (websocket) result.error = "upgrade rejected";
/* TEMPORARY: delete after probe. */         }
/* TEMPORARY: delete after probe. */       } catch {
/* TEMPORARY: delete after probe. */         // Never expose upstream exception messages, request headers, or credentials.
/* TEMPORARY: delete after probe. */         result.error = controller.signal.aborted ? "timeout" : "request failed";
/* TEMPORARY: delete after probe. */       } finally {
/* TEMPORARY: delete after probe. */         clearTimeout(timer);
/* TEMPORARY: delete after probe. */         controller.abort();
/* TEMPORARY: delete after probe. */         if (reader) void reader.cancel().catch(() => {});
/* TEMPORARY: delete after probe. */         try { socket?.close(1000, "probe complete"); } catch { /* Ignore cleanup failure. */ }
/* TEMPORARY: delete after probe. */       }
/* TEMPORARY: delete after probe. */       return result;
/* TEMPORARY: delete after probe. */     };
/* TEMPORARY: delete after probe. */     // Deliberately sequential: no other app behavior or provider helpers are invoked.
/* TEMPORARY: delete after probe. */     const rest_keyless = {
/* TEMPORARY: delete after probe. */       ping: await probe("https://fapi.binance.com/fapi/v1/ping"),
/* TEMPORARY: delete after probe. */       klines: await probe(KLINES),
/* TEMPORARY: delete after probe. */     };
/* TEMPORARY: delete after probe. */     const rest_keyed = key ? await probe(KLINES, false, key) : { skipped: "no key configured" };
/* TEMPORARY: delete after probe. */     const ws_keyless = [
/* TEMPORARY: delete after probe. */       await probe("https://fstream.binance.com/market/stream?streams=btcusdt@kline_1m", true),
/* TEMPORARY: delete after probe. */       await probe("https://fstream.binance.com/ws/btcusdt@kline_1m", true),
/* TEMPORARY: delete after probe. */     ];
/* TEMPORARY: delete after probe. */     const comparison = {
/* TEMPORARY: delete after probe. */       binance_vision: await probe("https://data-api.binance.vision/api/v3/ping"),
/* TEMPORARY: delete after probe. */       okx: await probe("https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1m&limit=1"),
/* TEMPORARY: delete after probe. */     };
/* TEMPORARY: delete after probe. */     return Response.json({
/* TEMPORARY: delete after probe. */       worker: { colo: request.cf?.colo ? clean(request.cf.colo) : null, country: request.cf?.country ? clean(request.cf.country) : null },
/* TEMPORARY: delete after probe. */       rest_keyless, rest_keyed, ws_keyless, comparison,
/* TEMPORARY: delete after probe. */     }, { headers: noStore });
/* TEMPORARY: delete after probe. */   } catch {
/* TEMPORARY: delete after probe. */     // Fail closed without passing an exception to application logging/error capture.
/* TEMPORARY: delete after probe. */     return new Response(null, { status: 404, headers: noStore });
/* TEMPORARY: delete after probe. */   }
/* TEMPORARY: delete after probe. */ }
/* TEMPORARY: delete after probe. */ export const Route = createFileRoute("/api/diagnostics/binance-probe")({
/* TEMPORARY: delete after probe. */   server: { handlers: {
/* TEMPORARY: delete after probe. */     GET: ({ request }) => handle(request),
/* TEMPORARY: delete after probe. */     HEAD: () => new Response(null, { status: 404, headers: { "cache-control": "no-store" } }),
/* TEMPORARY: delete after probe. */     ANY: () => new Response(null, { status: 404, headers: { "cache-control": "no-store" } }),
/* TEMPORARY: delete after probe. */   } },
/* TEMPORARY: delete after probe. */ });
