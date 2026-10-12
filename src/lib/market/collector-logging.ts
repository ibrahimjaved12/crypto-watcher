/** Keep diagnostics single-line and redact credentials from upstream error messages. */
export function collectorLogMessage(value: unknown): string {
  return (value instanceof Error ? value.message : String(value))
    .replace(/\b(?:https?|wss?|postgres(?:ql)?):\/\/[^\s]+/gi, "[redacted URL]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [redacted]")
    .replace(/((?:api[_-]?key|secret|token|password|authorization)["']?\s*[=:]\s*["']?)[^\s,;]+/gi, "$1[redacted]")
    .replace(/[\r\n]/g, " ");
}
