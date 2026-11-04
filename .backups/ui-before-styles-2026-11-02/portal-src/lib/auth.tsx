import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { IPublicClientApplication } from "@azure/msal-browser";
import { api, configureApi, post } from "./api";

/*
 * Sign-in modes, chosen by the API (GET /api/auth/config):
 *   microsoft  (default) sign in with your Microsoft account: the portal shows a code, you enter it at
 *              microsoft.com/devicelogin and approve with Microsoft Authenticator; the API then sets an HttpOnly
 *              session cookie. No app registration, no tokens in the browser.
 *   entra      MSAL in the browser against an app registration; access tokens are sent as bearer tokens.
 *   dev        automated tests only (the portal offers no way in).
 */

export type AuthMode = "microsoft" | "entra" | "dev";
type AuthState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "signedOut"; mode: AuthMode; notice?: string }
  | { status: "signedIn"; mode: AuthMode };

export interface DeviceFlow { flow_id: string; user_code: string; verification_uri: string; expires_at: number }
export type DevicePoll = { status: "waiting" | "done" | "error" | "expired"; message?: string; bot_connected_to_azure?: boolean };

interface AuthApi {
  state: AuthState;
  /** microsoft mode: ask for a sign-in code. */
  startMicrosoft: () => Promise<DeviceFlow>;
  /** microsoft mode: has the person finished signing in? Signs the portal in when they have. */
  pollMicrosoft: (flowId: string) => Promise<DevicePoll>;
  /** entra mode: redirect to Microsoft. */
  signInRedirect: () => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthApi | null>(null);

const env = import.meta.env;
const ENTRA = {
  clientId: env.VITE_ENTRA_CLIENT_ID as string | undefined,
  tenantId: env.VITE_ENTRA_TENANT_ID as string | undefined,
  scope: env.VITE_ENTRA_API_SCOPE as string | undefined,
};

let msal: IPublicClientApplication | null = null;

async function getMsal(): Promise<IPublicClientApplication> {
  if (msal) return msal;
  if (!ENTRA.clientId || !ENTRA.tenantId || !ENTRA.scope) {
    throw new Error("Microsoft sign-in is not configured: set VITE_ENTRA_CLIENT_ID, VITE_ENTRA_TENANT_ID and VITE_ENTRA_API_SCOPE.");
  }
  const { PublicClientApplication } = await import("@azure/msal-browser");
  const app = new PublicClientApplication({
    auth: { clientId: ENTRA.clientId, authority: `https://login.microsoftonline.com/${ENTRA.tenantId}`, redirectUri: window.location.origin },
    cache: { cacheLocation: "sessionStorage" },
  });
  await app.initialize();
  const result = await app.handleRedirectPromise();
  if (result?.account) app.setActiveAccount(result.account);
  else if (!app.getActiveAccount() && app.getAllAccounts()[0]) app.setActiveAccount(app.getAllAccounts()[0]!);
  msal = app;
  return app;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [mode, setMode] = useState<AuthMode>("microsoft");

  useEffect(() => {
    configureApi(
      async () => {
        if (mode !== "entra") return null; // microsoft: the session cookie travels on its own
        const app = msal;
        const account = app?.getActiveAccount();
        if (!app || !account || !ENTRA.scope) return null;
        try {
          return (await app.acquireTokenSilent({ scopes: [ENTRA.scope], account })).accessToken;
        } catch {
          await app.acquireTokenRedirect({ scopes: [ENTRA.scope], account });
          return null;
        }
      },
      (detail) => setState({ status: "signedOut", mode, notice: detail.startsWith("mfa_required")
        ? "This portal needs Microsoft Authenticator. Sign in again and approve the prompt."
        : detail === "not signed in" ? undefined : "Your session has ended. Sign in again." }),
    );
  }, [mode]);

  useEffect(() => {
    (async () => {
      try {
        const cfg = await api<{ mode: AuthMode }>("/api/auth/config");
        setMode(cfg.mode);
        if (cfg.mode === "entra") {
          const app = await getMsal();
          setState(app.getActiveAccount() ? { status: "signedIn", mode: "entra" } : { status: "signedOut", mode: "entra" });
          return;
        }
        if (cfg.mode === "dev") {
          setState({ status: "signedOut", mode: "dev" });
          return;
        }
        try {
          await api("/api/me");
          setState({ status: "signedIn", mode: "microsoft" });
        } catch {
          setState({ status: "signedOut", mode: "microsoft" });
        }
      } catch (e) {
        setState({ status: "error", message: (e as Error).message || "The API is not reachable." });
      }
    })();
  }, []);

  const value = useMemo<AuthApi>(() => ({
    state,
    startMicrosoft: () => post<DeviceFlow>("/api/auth/device/start"),
    pollMicrosoft: async (flowId) => {
      const r = await api<DevicePoll>(`/api/auth/device/${encodeURIComponent(flowId)}`);
      if (r.status === "done") setState({ status: "signedIn", mode: "microsoft" });
      return r;
    },
    signInRedirect: async () => {
      const app = await getMsal();
      await app.loginRedirect({ scopes: ENTRA.scope ? [ENTRA.scope] : [] });
    },
    signOut: () => {
      if (mode === "entra" && msal) {
        void msal.logoutRedirect();
        return;
      }
      void post("/api/auth/logout").finally(() => setState({ status: "signedOut", mode }));
    },
  }), [state, mode]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
