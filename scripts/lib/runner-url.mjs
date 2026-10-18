// Fixed public runner URL for `npm run up` (#308, #431): a Tailscale Funnel address, required by the
// launcher. Pure, no dependencies.
const MESSAGE = "RUNNER_PUBLIC_URL must be an https://<machine>.<tailnet>.ts.net address with no path or port";
const HOST = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\.ts\.net$/;

/**
 * Unset or empty -> null (the caller treats that as missing). Otherwise the normalized origin `https://host`.
 * The error never echoes the value.
 */
export function parseRunnerPublicUrl(value) {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string" || value !== value.trim()) throw new Error(MESSAGE);
  // The URL parser drops a default port, an empty query and an empty hash; check the raw text too.
  if (/[?#]/.test(value) || /^https:\/\/[^/]*[:@]/i.test(value)) throw new Error(MESSAGE);
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(MESSAGE);
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port || url.pathname !== "/" ||
      url.search || url.hash || !HOST.test(url.hostname)) {
    throw new Error(MESSAGE);
  }
  return `https://${url.hostname}`;
}
