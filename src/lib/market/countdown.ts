/** Countdown text for the forex market state (#413): truncated, never negative. */
export function formatCountdown(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / 60_000);
  if (minutes < 1) return "< 1 min";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days >= 1) return `${days} d ${hours} h`;
  if (hours >= 1) return `${hours} h ${minutes % 60} min`;
  return `${minutes} min`;
}
