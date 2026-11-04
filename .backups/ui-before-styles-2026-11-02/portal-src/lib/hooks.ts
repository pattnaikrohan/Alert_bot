import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** Fetch JSON, keep the last good data while refreshing, optionally poll. */
export function useApi<T>(path: string | null, opts: { refreshMs?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const current = useRef(path);
  current.current = path;

  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      const result = await api<T>(path);
      if (current.current === path) {
        setData(result);
        setError(null);
      }
    } catch (e) {
      if (current.current === path) setError(e as Error);
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setData(null);
    void load();
  }, [load]);

  // the top bar's refresh button reloads every mounted query
  useEffect(() => {
    const on = () => void load();
    window.addEventListener("alertbot:refresh", on);
    return () => window.removeEventListener("alertbot:refresh", on);
  }, [load]);

  useEffect(() => {
    if (!opts.refreshMs || !path) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, opts.refreshMs);
    return () => window.clearInterval(id);
  }, [load, opts.refreshMs, path]);

  return { data, error, loading, reload: load, setData };
}

export const refreshAll = () => window.dispatchEvent(new Event("alertbot:refresh"));

/** The same question asked of several places at once (e.g. every project's incidents): answers in the same order, a
 *  failed one as null (the rest still show). Reloads with the top bar's refresh and, optionally, on a timer. */
export function useApiEach<T>(paths: string[], opts: { refreshMs?: number } = {}) {
  const [data, setData] = useState<(T | null)[] | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(paths.length > 0);
  const key = paths.join("\n");
  const current = useRef(key);
  current.current = key;

  const load = useCallback(async () => {
    const list = key ? key.split("\n") : [];
    setLoading(true);
    const settled = await Promise.allSettled(list.map((p) => api<T>(p)));
    if (current.current !== key) return;
    const failed = settled.filter((s) => s.status === "rejected") as PromiseRejectedResult[];
    setData(settled.map((s) => (s.status === "fulfilled" ? s.value : null)));
    setError(failed.length && failed.length === settled.length ? (failed[0]!.reason as Error) : null);
    setLoading(false);
  }, [key]);

  useEffect(() => { setData(null); void load(); }, [load]);
  useEffect(() => {
    const on = () => void load();
    window.addEventListener("alertbot:refresh", on);
    return () => window.removeEventListener("alertbot:refresh", on);
  }, [load]);
  useEffect(() => {
    if (!opts.refreshMs || !key) return;
    const id = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, opts.refreshMs);
    return () => window.clearInterval(id);
  }, [load, key, opts.refreshMs]);

  return { data, error, loading, reload: load };
}

export function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const stamped = document.documentElement.dataset.theme;
    if (stamped === "light" || stamped === "dark") return stamped;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* storage unavailable */
    }
    setTheme(next);
  };
  return { theme, toggle };
}

/** Re-render periodically so relative times ("3 min ago") stay fresh. */
export function useNow(intervalMs = 30000) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
}
