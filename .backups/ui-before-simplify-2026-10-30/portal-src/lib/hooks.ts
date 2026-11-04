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
