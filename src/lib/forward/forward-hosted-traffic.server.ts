/** JSON body sizes, excluding HTTP headers/compression; count attempted hosted requests. */
export function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value) ?? "").byteLength;
}
export type HostedTrafficSnapshot = { reads: number; read_bytes: number; writes: number; write_bytes: number };

export function createHostedTraffic() {
  let reads = 0, readBytes = 0, writes = 0, writeBytes = 0;
  return {
    reset() { reads = readBytes = writes = writeBytes = 0; },
    read(value: unknown) { reads++; readBytes += jsonBytes(value); },
    write(value: unknown) { writes++; writeBytes += jsonBytes(value); },
    add(value: HostedTrafficSnapshot) {
      reads += value.reads; readBytes += value.read_bytes;
      writes += value.writes; writeBytes += value.write_bytes;
    },
    snapshot() { return { reads, read_bytes: readBytes, writes, write_bytes: writeBytes }; },
    line() {
      return `[forward-hosted] reads=${reads} read_bytes=${readBytes} writes=${writes} write_bytes=${writeBytes}`;
    },
  };
}
export type HostedTraffic = ReturnType<typeof createHostedTraffic>;
