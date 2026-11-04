// Sign-in. People sign in with their own Microsoft account: a code here, entered at microsoft.com/devicelogin and approved
// in Microsoft Authenticator. Three visible steps, so nobody wonders what to do next. Follows the chosen theme.
import {
  ArrowRight, Check, CheckCircle2, ChevronDown, Copy, Database, ExternalLink, KeyRound, Loader2, Lock, RotateCcw, ShieldCheck,
  Smartphone, Sparkles, TriangleAlert, Workflow,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { BrandLockup } from "@/components/BrandMark";
import { ThemeButton } from "@/components/ThemePicker";
import { Button } from "@/components/ui/button";
import { useAuth, type DeviceFlow } from "@/lib/auth";
import { cn } from "@/lib/utils";

type Phase = "idle" | "starting" | "waiting" | "done" | "error" | "expired";

function Steps({ at }: { at: 0 | 1 | 2 | 3 }) {
  const steps = [
    { icon: KeyRound, label: "Get a code" },
    { icon: ExternalLink, label: "Enter it at Microsoft" },
    { icon: Smartphone, label: "Approve on your phone" },
  ];
  return (
    <ol className="auth-steps mt-6 grid grid-cols-3 gap-2" aria-label="Sign-in steps">
      {steps.map((s, i) => {
        const state = i < at ? "done" : i === at ? "now" : "next";
        return (
          <li key={s.label} className={cn("auth-step", state)} aria-current={state === "now" ? "step" : undefined}>
            <span className="auth-step-dot">{state === "done" ? <Check className="size-3.5" /> : <s.icon className="size-3.5" />}</span>
            <span className="auth-step-label">{s.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function Help() {
  return (
    <details className="auth-help group mt-5 rounded-xl px-3.5 py-2.5 text-[0.8125rem]">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-medium text-[var(--h-ink-2)]">
        Having trouble signing in? <ChevronDown className="size-4 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <ul className="mt-2 space-y-1.5 text-[var(--h-ink-2)]">
        <li>• <b>Wrong account?</b> At Microsoft, choose "Use another account" and pick your work account.</li>
        <li>• <b>No phone prompt?</b> Open Microsoft Authenticator; the request waits there. Multi-factor sign-in is required.</li>
        <li>• <b>Code expired?</b> Codes last 15 minutes. Press "Get a new code".</li>
        <li>• <b>Signed in but see nothing?</b> The bot shows what your Azure access covers; ask for Reader access on the pipelines you need.</li>
      </ul>
    </details>
  );
}

function MicrosoftSignIn({ notice }: { notice?: string }) {
  const { startMicrosoft, pollMicrosoft } = useAuth();
  const [flow, setFlow] = useState<DeviceFlow | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState(notice ?? "");
  const [copied, setCopied] = useState(false);
  const [opened, setOpened] = useState(false);
  const [left, setLeft] = useState(0);
  const [total, setTotal] = useState(900);
  const timer = useRef(0);

  const start = useCallback(async () => {
    setPhase("starting");
    setMessage("");
    setCopied(false);
    setOpened(false);
    try {
      const f = await startMicrosoft();
      setFlow(f);
      const secs = Math.max(1, Math.round(f.expires_at - Date.now() / 1000));
      setLeft(secs);
      setTotal(secs);
      setPhase("waiting");
    } catch (e) {
      setPhase("error");
      setMessage((e as Error).message);
    }
  }, [startMicrosoft]);

  // Wait for the person to finish at Microsoft; then the portal signs in by itself.
  useEffect(() => {
    if (phase !== "waiting" || !flow) return;
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      setLeft(Math.max(0, Math.round(flow.expires_at - Date.now() / 1000)));
      try {
        const r = await pollMicrosoft(flow.flow_id);
        if (!alive) return;
        if (r.status === "done") return setPhase("done");
        if (r.status === "expired") return setPhase("expired");
        if (r.status === "error") {
          setPhase("error");
          return setMessage(r.message?.startsWith("mfa_required")
            ? "This portal needs Microsoft Authenticator (multi-factor) sign-in, and that sign-in didn't include it. Set up Authenticator for your account, then try again."
            : r.message || "Sign-in didn't complete.");
        }
      } catch (e) {
        if (!alive) return;
        setPhase("error");
        return setMessage((e as Error).message);
      }
      timer.current = window.setTimeout(tick, 2000);
    };
    void tick();
    return () => { alive = false; window.clearTimeout(timer.current); };
  }, [phase, flow, pollMicrosoft]);

  const copy = async () => {
    if (!flow) return;
    try {
      await navigator.clipboard.writeText(flow.user_code);
      setCopied(true);
    } catch {
      /* clipboard blocked: the code is on screen */
    }
  };
  const copyAndOpen = async () => {
    await copy();
    setOpened(true);
    window.open(flow!.verification_uri, "_blank", "noopener,noreferrer");
  };

  if (phase === "done") {
    return (
      <div className="mt-8 text-center" aria-live="polite">
        <span className="auth-success mx-auto flex size-14 items-center justify-center rounded-full"><CheckCircle2 className="size-7" /></span>
        <h3 className="display mt-4 text-xl font-bold">You're signed in</h3>
        <p className="mt-1.5 text-sm text-[var(--h-ink-2)]">Opening your dashboard and finding the pipelines your Azure account can see…</p>
        <Loader2 className="mx-auto mt-4 size-5 animate-spin text-[var(--h-ink-3)]" />
      </div>
    );
  }

  if (phase === "waiting" && flow) {
    return (
      <div aria-live="polite">
        <Steps at={opened ? 2 : 1} />
        <div className="mt-6">
          <p className="text-sm font-medium text-[var(--h-ink-2)]">Your sign-in code</p>
          <div className="mt-2 flex items-stretch gap-2">
            <code className="auth-code flex-1 select-all rounded-2xl px-4 py-3.5 text-center font-mono text-[1.875rem] font-bold tracking-[0.3em]">{flow.user_code}</code>
            <Button variant="secondary" className="h-auto w-14" aria-label={copied ? "Code copied" : "Copy the code"} onClick={() => void copy()}>
              {copied ? <Check /> : <Copy />}
            </Button>
          </div>
          <p className="mt-1.5 h-4 text-xs text-[var(--h-ink-3)]">{copied ? "Copied. Paste it on the Microsoft page." : "Select it or press copy."}</p>
        </div>
        <Button variant="primary" size="lg" className="mt-4 w-full" onClick={() => void copyAndOpen()}>
          <ExternalLink /> {opened ? "Open the Microsoft page again" : "Copy code & open Microsoft sign-in"}
        </Button>
        {opened && (
          <p className="auth-callout mt-4 flex items-start gap-2.5 rounded-xl px-3.5 py-3 text-sm">
            <Smartphone className="mt-0.5 size-4 shrink-0" />
            <span>Paste the code, choose your work account, then <b>approve the prompt in Microsoft Authenticator</b>. This page continues on its own.</span>
          </p>
        )}
        <div className="mt-5">
          <div className="flex items-center justify-between text-xs text-[var(--h-ink-3)]">
            <span className="flex items-center gap-1.5"><Loader2 className="size-3.5 animate-spin" /> Waiting for you at Microsoft</span>
            <span className="tabular-nums">{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</span>
          </div>
          <div className="auth-progress mt-1.5 h-1 overflow-hidden rounded-full"><span style={{ width: `${Math.min(100, (left / total) * 100)}%` }} /></div>
        </div>
        <button type="button" onClick={() => void start()} className="mx-auto mt-3 flex items-center gap-1 text-xs text-[var(--h-ink-3)] hover:text-[var(--h-ink)]">
          <RotateCcw className="size-3" /> Get a new code
        </button>
        <Help />
      </div>
    );
  }

  return (
    <>
      <Steps at={0} />
      {(phase === "error" || phase === "expired" || message) && (
        <p role="alert" className="auth-warn mt-5 flex items-start gap-2 rounded-xl px-3.5 py-3 text-sm">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" /> {phase === "expired" ? "The code expired before sign-in finished. Get a new one." : message}
        </p>
      )}
      <Button variant="primary" size="lg" className="mt-6 h-12 w-full text-[0.9375rem]" disabled={phase === "starting"} onClick={() => void start()}>
        {phase === "starting" ? <Loader2 className="animate-spin" /> : <MicrosoftLogo />} {phase === "error" || phase === "expired" ? "Try again" : "Sign in with Microsoft"}
        {phase !== "starting" && <ArrowRight className="ml-auto" />}
      </Button>
      <ul className="mt-5 space-y-2 text-[0.8125rem] text-[var(--h-ink-2)]">
        <li className="flex gap-2"><span className="auth-bullet">1</span> We show you a short code.</li>
        <li className="flex gap-2"><span className="auth-bullet">2</span> You enter it on Microsoft's sign-in page with your work account.</li>
        <li className="flex gap-2"><span className="auth-bullet">3</span> You approve it in Microsoft Authenticator, and you're in.</li>
      </ul>
      <Help />
    </>
  );
}

function MicrosoftLogo() {
  return (
    <svg viewBox="0 0 21 21" className="size-4" aria-hidden>
      <rect x="1" y="1" width="9" height="9" fill="#f25022" /><rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" /><rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}

function Feature({ icon: Icon, title, children }: { icon: typeof Workflow; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-3.5">
      <span className="auth-feature-icon flex size-10 shrink-0 items-center justify-center rounded-xl"><Icon className="size-[1.125rem]" /></span>
      <span>
        <span className="block text-[0.9375rem] font-semibold">{title}</span>
        <span className="mt-0.5 block text-sm leading-6 text-[var(--h-ink-2)]">{children}</span>
      </span>
    </li>
  );
}

export function SignIn() {
  const { state, signInRedirect } = useAuth();

  return (
    <div className="auth-root relative min-h-screen overflow-hidden">
      <div className="auth-blob b1" aria-hidden />
      <div className="auth-blob b2" aria-hidden />
      <div className="auth-blob b3" aria-hidden />

      <header className="relative z-10 mx-auto flex max-w-[88rem] items-center justify-between px-6 pt-6 lg:px-12">
        <BrandLockup size="lg" />
        <ThemeButton labelled />
      </header>

      <div className="relative mx-auto grid max-w-[88rem] items-center gap-10 px-6 pb-12 pt-8 lg:min-h-[calc(100vh-5.5rem)] lg:grid-cols-[1.1fr_minmax(0,29rem)] lg:px-12 lg:pt-0">
        <section className="hidden lg:block">
          <p className="auth-eyebrow inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold"><Sparkles className="size-3.5" /> For Azure data teams</p>
          <h1 className="display mt-5 max-w-xl text-[3.25rem] font-extrabold leading-[1.04] tracking-[-0.035em]">
            Know when your data breaks, <span className="auth-gradient-text">and why.</span>
          </h1>
          <p className="mt-5 max-w-lg text-[1.0625rem] leading-7 text-[var(--h-ink-2)]">
            One place to see every pipeline you're responsible for, whether today's data has arrived, and what to do when it hasn't.
          </p>
          <ul className="mt-8 max-w-md space-y-5">
            <Feature icon={Workflow} title="Every pipeline you can see">Data Factory, Synapse pipelines and Logic Apps, found with your own Microsoft access. Nothing to set up.</Feature>
            <Feature icon={Database} title="The data inside the tables">Rows inserted per table, empty values, duplicates and freshness, against what's normal.</Feature>
            <Feature icon={ShieldCheck} title="Problems explained, not just flagged">The failing step, the likely cause, the impact and the next step, sent to Teams.</Feature>
          </ul>
        </section>

        <section className="auth-card rounded-[1.75rem] p-7 sm:p-9">
          {state.status === "error" ? (
            <div className="space-y-3">
              <h2 className="display text-2xl font-bold">The service isn't reachable</h2>
              <p className="flex items-start gap-2 text-sm text-[var(--h-ink-2)]"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" /> {state.message}</p>
              <p className="text-sm text-[var(--h-ink-3)]">Start it with <code className="font-mono text-[var(--h-ink)]">uv run alertbot serve</code>, then reload this page.</p>
              <Button variant="secondary" onClick={() => window.location.reload()}><RotateCcw /> Reload</Button>
            </div>
          ) : state.status === "signedOut" ? (
            <>
              <h2 className="display text-[1.875rem] font-bold tracking-[-0.02em]">Sign in</h2>
              <p className="mt-1.5 text-sm text-[var(--h-ink-2)]">Use your work Microsoft account. It takes about 30 seconds.</p>
              {state.mode === "microsoft" ? (
                <MicrosoftSignIn notice={state.notice} />
              ) : state.mode === "entra" ? (
                <Button variant="primary" size="lg" className="mt-6 h-12 w-full" onClick={() => void signInRedirect()}><MicrosoftLogo /> Sign in with Microsoft</Button>
              ) : (
                <p className="mt-4 text-sm text-[var(--h-ink-2)]">
                  This server runs in test mode, which has no sign-in. Set <code className="font-mono">ALERTBOT_AUTH_MODE=microsoft</code> and restart it.
                </p>
              )}
              <div className="auth-divider my-6 h-px" />
              <ul className="space-y-2 text-[0.75rem] leading-5 text-[var(--h-ink-3)]">
                <li className="flex items-start gap-2"><Lock className="mt-0.5 size-3.5 shrink-0" /> You only see what your own Azure access covers, checked on every request.</li>
                <li className="flex items-start gap-2"><ShieldCheck className="mt-0.5 size-3.5 shrink-0" /> Read-only: the bot never changes your pipelines or data.</li>
              </ul>
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}
