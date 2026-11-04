const nf = new Intl.NumberFormat();
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const dtf = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const tf = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const df = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });

export const num = (v: number | null | undefined) => (v == null ? "–" : nf.format(Math.round(v)));
export const compactNum = (v: number | null | undefined) => (v == null ? "–" : v < 10000 ? nf.format(Math.round(v)) : compact.format(v));

export function pct(v: number | null | undefined, digits?: number) {
  if (v == null) return "–";
  const p = v * 100;
  const d = digits ?? (p < 1 && p > 0 ? 2 : p < 10 ? 1 : 0);
  return `${p.toFixed(d)}%`;
}

export function duration(seconds: number | null | undefined) {
  if (seconds == null) return "–";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = seconds / 60;
  if (m < 90) return `${Math.round(m)} min`;
  return `${(m / 60).toFixed(1)} h`;
}

export function ago(iso: string | null | undefined) {
  if (!iso) return "never";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return "just now";
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}

export const dateTime = (iso: string | null | undefined) => (iso ? dtf.format(new Date(iso)) : "–");
export const time = (iso: string | null | undefined) => (iso ? tf.format(new Date(iso)) : "–");
export const day = (iso: string) => df.format(new Date(iso));

export function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]!.toUpperCase())
    .join("");
}

export const checkLabel = (c: string) =>
  ({
    stuck_trigger: "Stuck trigger",
    run_failed: "Run failed",
    run_too_slow: "Run too slow",
    row_count: "Row count",
    empty_site: "Empty site",
    null_spike: "Null spike",
    duplicates: "Duplicates",
    freshness: "Freshness",
    schema_drift: "Schema drift",
    mismatch: "Mismatch",
  })[c] ?? c.replace(/_/g, " ");

const fdf = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
/** For records: the date with its year, and the time on its own, e.g. "14 Oct 2026" · "13:10". */
export const fullDate = (iso: string | null | undefined) => (iso ? fdf.format(new Date(iso)) : "–");
export function between(a: string | null | undefined, b: string | null | undefined) {
  if (!a || !b) return "–";
  const s = (new Date(b).getTime() - new Date(a).getTime()) / 1000;
  return s >= 48 * 3600 ? `${(s / 86400).toFixed(1)} days` : duration(s);
}
