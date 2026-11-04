// Thin fetch wrapper. The auth provider registers how to get a bearer token;
// every call goes through here so the token is always attached.

type TokenProvider = () => Promise<string | null>;
let tokenProvider: TokenProvider = async () => null;
let onUnauthorized: (detail: string) => void = () => {};

export function configureApi(provider: TokenProvider, unauthorized: (detail: string) => void) {
  tokenProvider = provider;
  onUnauthorized = unauthorized;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await tokenProvider();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("X-AlertBot", "1"); // the API refuses cookie-authenticated actions without it (cross-site protection)
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) {
    let detail = "";
    try {
      detail = String((await res.json()).detail ?? "");
    } catch {
      /* not JSON */
    }
    onUnauthorized(detail);
    throw new ApiError(401, detail.startsWith("mfa_required")
      ? "Approve the Microsoft Authenticator prompt to continue."
      : "Your session has ended. Sign in again.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  const out = (await res.json()) as T;
  if (init.method && init.method !== "GET" && CHANGES.test(path)) setTimeout(() => window.dispatchEvent(new Event("alertbot:refresh")), 0);
  return out;
}

/** Actions that change what other pages show: after one, every mounted view reloads (the dock, Home, lists…). */
const CHANGES = /\/(incidents|mappings|databases|mutes|remediat\w*|actions)\b/;

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });

export const patch = <T,>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
