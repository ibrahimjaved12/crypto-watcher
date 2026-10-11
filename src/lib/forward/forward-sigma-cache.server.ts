import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { existsSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { validateSigmaState, type SigmaStateRecord } from "./forward-contract";

export type HostedCheckpoint = { checksum: string; as_of_ms: number; checkpointed_at: number | null } |
  { checksum: null; as_of_ms: null; checkpointed_at: null };
export type SigmaCacheEntry = { state: SigmaStateRecord; hosted: HostedCheckpoint | null };
export type LoadedSigma = { state: SigmaStateRecord | null; hosted: HostedCheckpoint | null;
  source: "local" | "restored" | "rebuilt" };
export interface SigmaCache {
  load(symbol: string, restore: () => Promise<{ state: unknown; checkpointed_at: number } | null>): Promise<LoadedSigma>;
  save(entry: SigmaCacheEntry): Promise<boolean>;
  checkpointed(state: SigmaStateRecord, at: number): Promise<void>;
  conflict(symbol: string): Promise<void>;
}

let directoryLogged = false;
function defaultDirectory(): string {
  if (process.env.FORWARD_STATE_DIR) return process.env.FORWARD_STATE_DIR;
  let root = process.cwd();
  for (;;) {
    if (existsSync(resolve(root, "package.json")) && existsSync(resolve(root, "supabase")) &&
        statSync(resolve(root, "supabase")).isDirectory()) return resolve(root, ".forward-state");
    const parent = dirname(root);
    if (parent === root) throw new Error("Cannot find repository root; set FORWARD_STATE_DIR");
    root = parent;
  }
}

/** One atomic file per symbol. Lock directories serialize writers across worker processes. */
export function createSigmaCache(directory?: string, now = () => Date.now()): SigmaCache {
  const cacheDirectory = resolve(directory ?? defaultDirectory());
  if (!directoryLogged) { console.log(`[forward-sigma] cache dir=${cacheDirectory}`); directoryLogged = true; }
  function path(symbol: string) {
    if (!/^[A-Z0-9]{5,16}$/.test(symbol)) throw new Error("Invalid sigma cache symbol");
    return resolve(cacheDirectory, `${symbol}.json`);
  }
  async function read(symbol: string): Promise<SigmaCacheEntry | null> {
    let entry: SigmaCacheEntry;
    try { entry = JSON.parse(await readFile(path(symbol), "utf8")); }
    catch (error) {
      if (error instanceof SyntaxError || (error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
    try {
      const state = await validateSigmaState(entry.state, symbol);
      if (!hostedValid(entry.hosted) || !(state.as_of_ms <= now())) return null;
      return { state, hosted: entry.hosted };
    } catch { return null; }
  }
  async function locked<T>(symbol: string, operation: () => Promise<T>): Promise<T> {
    await mkdir(cacheDirectory, { recursive: true, mode: 0o700 });
    const lock = `${path(symbol)}.lock`;
    const started = Date.now();
    for (;;) {
      try { await mkdir(lock); break; }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        // Only one contender may reclaim a dead writer. Otherwise two reclaimers could
        // remove a newly acquired live lock and allow an older state to win a write race.
        const recovery = `${lock}.recover`;
        let recovering = false;
        try {
          try { await mkdir(recovery); recovering = true; }
          catch (probe) { if ((probe as NodeJS.ErrnoException).code !== "EEXIST") throw probe; }
          if (recovering) {
            const owner = await readFile(resolve(lock, "pid"), "utf8").catch((probe: NodeJS.ErrnoException) => {
              if (probe.code === "ENOENT") return "";
              throw probe;
            });
            const pid = Number(owner);
            if (Number.isSafeInteger(pid) && pid > 0) {
              try { process.kill(pid, 0); }
              catch (probe) {
                if ((probe as NodeJS.ErrnoException).code === "ESRCH") await rm(lock, { recursive: true, force: true });
              }
            } else {
              const info = await stat(lock).catch(() => null);
              if (info && Date.now() - info.mtimeMs > 30_000) await rm(lock, { recursive: true, force: true });
            }
          }
        } finally {
          if (recovering) await rm(recovery, { recursive: true, force: true });
        }
        if (Date.now() - started > 10_000) throw new Error(`Sigma cache lock busy: ${symbol}`);
        await new Promise((done) => setTimeout(done, 25));
      }
    }
    try {
      await writeFile(resolve(lock, "pid"), String(process.pid), { mode: 0o600 });
      return await operation();
    } finally { await rm(lock, { recursive: true, force: true }); }
  }
  async function atomic(symbol: string, entry: SigmaCacheEntry) {
    const target = path(symbol), temporary = `${target}.${crypto.randomUUID()}.tmp`;
    try {
      await writeFile(temporary, JSON.stringify(entry), { flag: "wx", mode: 0o600 });
      await rename(temporary, target);
    } finally { await rm(temporary, { force: true }); }
  }
  function hostedValid(value: HostedCheckpoint | null): boolean {
    if (value === null) return true;
    if (typeof value !== "object") return false;
    if (value.checksum === null || value.as_of_ms === null)
      return value.checksum === null && value.as_of_ms === null && value.checkpointed_at === null;
    return typeof value.checksum === "string" && /^[0-9a-f]{64}$/.test(value.checksum) &&
      Number.isSafeInteger(value.as_of_ms) && value.as_of_ms >= 0 &&
      (value.checkpointed_at === null || (Number.isSafeInteger(value.checkpointed_at) && value.checkpointed_at >= 0));
  }
  return {
    async load(symbol, restore) {
      const entry = await read(symbol);
      if (entry && entry.hosted?.checkpointed_at !== null)
        return { state: entry.state, hosted: entry.hosted, source: "local" };
      const restored = await restore();
      // Invalid hosted data fails validation; only a missing row permits a rebuild.
      if (!restored) return { state: null, hosted: null, source: "rebuilt" };
      const state = await validateSigmaState(restored.state, symbol);
      const hosted = { checksum: state.checksum, as_of_ms: state.as_of_ms, checkpointed_at: restored.checkpointed_at };
      await locked(symbol, async () => {
        const current = await read(symbol);
        // A conflict restore may be older than the local file. Keep the disk cursor monotonic,
        // but return the hosted state for evaluation; null marks a restore still awaiting commit.
        const newer = (current?.state?.as_of_ms ?? -1) > state.as_of_ms;
        await atomic(symbol, { state: newer ? current!.state : state,
          hosted: newer ? { ...hosted, checkpointed_at: null } : hosted });
      });
      return { state, hosted, source: "restored" };
    },
    async save(entry) {
      await validateSigmaState(entry.state, entry.state.symbol);
      return locked(entry.state.symbol, async () => {
        const current = await read(entry.state.symbol);
        if ((current?.state?.as_of_ms ?? -1) > entry.state.as_of_ms) return false;
        await atomic(entry.state.symbol, entry);
        return true;
      });
    },
    async checkpointed(state, at) {
      await locked(state.symbol, async () => {
        const current = await read(state.symbol);
        if (!current) return;
        if (current.hosted?.as_of_ms != null && current.hosted.as_of_ms > state.as_of_ms) return;
        await atomic(state.symbol, { state: current.state,
          hosted: { checksum: state.checksum, as_of_ms: state.as_of_ms, checkpointed_at: at } });
      });
    },
    async conflict(symbol) {
      await locked(symbol, async () => {
        const current = await read(symbol);
        if (!current) return;
        // Durable restore marker without discarding or rolling back state.as_of_ms.
        await atomic(symbol, { state: current.state, hosted: current.hosted ? { ...current.hosted, checkpointed_at: null }
          : { checksum: null, as_of_ms: null, checkpointed_at: null } });
      });
    },
  };
}
