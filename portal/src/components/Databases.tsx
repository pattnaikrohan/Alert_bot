// Databases people connect (SQL Server, Snowflake) and the mapping of which pipeline loads which table.
// Shared by the Databases page and the project's Data quality tab. The bot only ever reads counts and percentages;
// passwords are write-only (sent once, stored encrypted, never shown again).
import { CircleAlert, CircleCheck, Database, KeyRound, Loader2, Lock, Plus, ShieldCheck, Snowflake, Trash2, X } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input, Segmented, Select } from "@/components/ui/primitives";
import { api, patch, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------- types
export type DbKind = "sqlserver" | "snowflake" | "sqlite";
export interface DbConn {
  id: number; name: string; kind: DbKind; host: string; port: number | null; database: string; auth: string; username: string;
  options: { encrypt?: boolean; trust_server_certificate?: boolean; warehouse?: string; role?: string };
  status: "ok" | "error" | "new"; can_write: boolean | null; last_error: string | null; last_ok_at: string | null;
  last_used_at: string | null; created_at: string;
  mappings: { project: string; project_name: string; pipeline: string; schema: string; table: string; mapping_id: number;
    enabled?: boolean; automatic?: boolean; retired?: boolean; retired_reason?: string | null; retired_at?: string | null }[];
}
export interface Suggestion {
  project: string; project_name: string; pipeline: string; connection_id: number; connection: string; schema: string; table: string;
  object: string; match: "exact" | "database";
}
export interface Rule { name: string; column: string; op: string; values: (string | number)[]; severity?: "warning" | "critical" }
export interface MappingSettings {
  key?: string[]; date_column?: string | null; required?: string[]; sensitive?: string[]; stats?: string[]; distribution?: string[] | null;
  ranges?: { column: string; min: number | null; max: number | null }[]; typed?: { column: string; type: "int" | "decimal" | "date" }[];
  rules?: Rule[]; freshness_hours?: number | null; every_hours?: number;
}
export interface Mapping {
  id: number; pipeline: string; schema: string; table: string; object: string; connection_id: number; connection: string;
  connection_kind: DbKind; connection_status: string; owner_id: number; settings: MappingSettings; enabled: boolean; automatic?: boolean;
  /** retired: Azure's definitions no longer show this pipeline loading this table (renamed, deleted or changed) */
  retired?: boolean; retired_reason?: string | null; retired_at?: string | null; follow_azure?: boolean;
}
export interface Column { name: string; type: string; kind: "number" | "text" | "date" | "other" }

export const KIND_LABEL: Record<DbKind, string> = { sqlserver: "SQL Server", snowflake: "Snowflake", sqlite: "SQLite" };
export const KindIcon = ({ kind, className }: { kind: DbKind; className?: string }) =>
  kind === "snowflake" ? <Snowflake className={className} aria-hidden /> : <Database className={className} aria-hidden />;

/** Column names that usually hold personal or confidential values: pre-marked sensitive (only counted as empty / not). */
export const LOOKS_SENSITIVE = /(EMAIL|E_?MAIL|PHONE|MOBILE|NAME|ADDRESS|STREET|POSTCODE|ZIP|SSN|NATIONAL|PASSPORT|DOB|BIRTH|SALARY|WAGE|IBAN|ACCOUNT_?NO|CARD|PASSWORD|TOKEN|SECRET|IP_?ADDR)/i;

// ---------------------------------------------------------------- small pieces
export function ConnStatus({ c, compact }: { c: Pick<DbConn, "status" | "last_ok_at" | "last_error">; compact?: boolean }) {
  const ok = c.status === "ok";
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5 text-xs font-medium", ok ? "text-[var(--good-ink)]" : c.status === "error" ? "text-[var(--critical-ink)]" : "text-ink-3")}
      title={c.last_error ?? undefined}>
      <span className={cn("relative size-2 shrink-0 rounded-full", ok ? "sb-live bg-good" : c.status === "error" ? "bg-critical" : "bg-[var(--ink-3)]")} aria-hidden />
      <span className="truncate">{ok ? (compact ? "Connected" : `Connected${c.last_ok_at ? ` · ${ago(c.last_ok_at)}` : ""}`) : c.status === "error" ? "Can't connect" : "Not checked yet"}</span>
    </span>
  );
}

export function Modal({ open, onClose, title, subtitle, icon: Icon = Database, children, footer, wide }: {
  open: boolean; onClose: () => void; title: ReactNode; subtitle?: ReactNode; icon?: typeof Database; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    requestAnimationFrame(() => box.current?.querySelector<HTMLElement>("input, select, textarea, button:not([data-close])")?.focus());
    return () => { window.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="modal-backdrop fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-6" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={box} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}
        className={cn("modal-card flex max-h-[min(92vh,52rem)] w-full flex-col overflow-hidden rounded-t-[1.5rem] border border-line-strong bg-surface shadow-[var(--elev-3)] sm:rounded-[1.5rem]",
          wide ? "sm:max-w-3xl" : "sm:max-w-xl")}>
        <header className="flex items-start gap-3 border-b border-line px-6 py-4">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><Icon className="size-[1.125rem]" aria-hidden /></span>
          <div className="min-w-0 flex-1">
            <h2 className="display text-base font-semibold text-ink">{title}</h2>
            {subtitle && <p className="text-[0.78125rem] leading-5 text-ink-3">{subtitle}</p>}
          </div>
          <button data-close onClick={onClose} className="flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="Close"><X className="size-4" /></button>
        </header>
        <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-6 py-5">{children}</div>
        {footer && <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-line bg-surface-2/50 px-6 py-3.5">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}

const Field = ({ label, hint, children, className }: { label: string; hint?: ReactNode; children: ReactNode; className?: string }) => (
  <label className={cn("block min-w-0", className)}>
    <span className="mb-1 block text-xs font-medium text-ink-2">{label}</span>
    {children}
    {hint && <span className="mt-1 block text-[0.6875rem] leading-4 text-ink-3">{hint}</span>}
  </label>
);

export function PrivacyPromise({ className, compact }: { className?: string; compact?: boolean }) {
  const items: [typeof Lock, string, string][] = [
    [ShieldCheck, "Counts, never rows", "Checks run inside the database and only numbers come back. One exception: suspicious values you open yourself, shown to you only, kept nowhere, never sent to AI."],
    [Lock, "Password locked away", "Stored encrypted, used only to connect, never shown again or written to a log."],
    [KeyRound, "Read-only, always", "Every query is a fixed SELECT that's rolled back, time-limited and checked before it runs."],
  ];
  if (compact) {
    return (
      <ul className={cn("space-y-1.5 rounded-xl bg-surface-2 px-3.5 py-3 shadow-[inset_0_0_0_1px_var(--line)]", className)}>
        {items.map(([Icon, title, text]) => (
          <li key={title} className="flex gap-2 text-[0.71875rem] leading-4 text-ink-3">
            <Icon className="mt-px size-3.5 shrink-0 text-[var(--good)]" aria-hidden /><span><b className="font-semibold text-ink-2">{title}.</b> {text}</span>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className={cn("grid gap-2 sm:grid-cols-3", className)}>
      {items.map(([Icon, title, text]) => (
        <div key={title} className="flex gap-2.5 rounded-xl bg-surface-2 p-3 shadow-[inset_0_0_0_1px_var(--line)]">
          <Icon className="mt-0.5 size-4 shrink-0 text-[var(--good)]" aria-hidden />
          <div className="min-w-0"><p className="text-[0.8125rem] font-semibold text-ink">{title}</p><p className="text-[0.71875rem] leading-[1.05rem] text-ink-3">{text}</p></div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- connect / edit a database
interface ConnPreview {
  kind: DbKind; host: string; port: number | null; database: string; auth: string; username: string; has_password: boolean;
  options: { encrypt?: boolean; trust_server_certificate?: boolean; warehouse?: string; role?: string }; ignored: string[];
}

/** What the bot read from a pasted string, before connecting: so a typo shows up here, not as a failed sign-in. */
function PreviewCard({ p }: { p: ConnPreview }) {
  const rows: [string, ReactNode][] = [
    ["Type", <span className="inline-flex items-center gap-1.5"><KindIcon kind={p.kind} className="size-3.5" />{KIND_LABEL[p.kind]}</span>],
    [p.kind === "snowflake" ? "Account" : "Server", <span className="font-mono">{p.host}{p.port ? `:${p.port}` : ""}</span>],
    ["Database", p.database],
    ["User", p.username || <span className="text-[var(--critical-ink)]">missing</span>],
    ["Password", p.has_password ? <span className="text-[var(--good-ink)]">included (kept encrypted)</span> : <span className="text-[var(--critical-ink)]">missing</span>],
  ];
  if (p.kind === "sqlserver") rows.push(["Encryption", p.options.encrypt === false ? <span className="text-[var(--warning-ink)]">off</span>
    : p.options.trust_server_certificate ? <span className="text-[var(--warning-ink)]">on, server certificate not verified</span> : "on, certificate verified"]);
  if (p.options.warehouse) rows.push(["Warehouse", p.options.warehouse]);
  if (p.options.role) rows.push(["Role", p.options.role]);
  return (
    <div className="rounded-xl bg-surface-2 px-3.5 py-3 shadow-[inset_0_0_0_1px_var(--line)]">
      <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-ink"><CircleCheck className="size-3.5 text-[var(--good)]" />The bot will connect to</p>
      <dl className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        {rows.map(([k, v]) => <Fragment key={k}><dt className="text-ink-3">{k}</dt><dd className="min-w-0 truncate text-ink">{v}</dd></Fragment>)}
      </dl>
      {p.ignored.length > 0 && <p className="mt-2 text-[0.6875rem] text-ink-3">Not used by the bot: {p.ignored.join(", ")}. Its own settings (read-only, time limits) always apply.</p>}
    </div>
  );
}

export function ConnectDialog({ open, onClose, onSaved, editing }: {
  open: boolean; onClose: () => void; onSaved: (c: DbConn, suggestions: Suggestion[]) => void; editing?: DbConn | null;
}) {
  const [kind, setKind] = useState<DbKind>("sqlserver");
  const [f, setF] = useState({ name: "", host: "", port: "", database: "", auth: "sql", username: "", secret: "", warehouse: "", role: "",
    encrypt: true, trust: false });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<"fields" | "string">("fields");
  const [cs, setCs] = useState("");
  const [showCs, setShowCs] = useState(false);
  const [preview, setPreview] = useState<ConnPreview | null>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setErr(null); setCs(""); setPreview(null); setPreviewErr(null); setShowCs(false);
    if (editing) {
      setKind(editing.kind);
      setF({ name: editing.name, host: editing.host, port: editing.port ? String(editing.port) : "", database: editing.database, auth: editing.auth,
        username: editing.username, secret: "", warehouse: editing.options.warehouse ?? "", role: editing.options.role ?? "",
        encrypt: editing.options.encrypt ?? true, trust: editing.options.trust_server_certificate ?? false });
    } else {
      setF((x) => ({ ...x, name: "", host: "", port: "", database: "", username: "", secret: "", warehouse: "", role: "", auth: kind === "snowflake" ? "password" : "sql" }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));
  const sf = kind === "snowflake";
  const byString = !editing && mode === "string";

  useEffect(() => {  // what the pasted string says (the server parses it; the password never comes back)
    if (!byString || cs.trim().length < 8) { setPreview(null); setPreviewErr(null); return; }
    let live = true;
    const t = setTimeout(() => {
      post<ConnPreview>("/api/databases/parse", { connection_string: cs })
        .then((r) => { if (live) { setPreview(r); setPreviewErr(null); } })
        .catch((e) => { if (live) { setPreview(null); { const m = (e as Error).message.replace(/^Couldn't read the connection string: /, ""); setPreviewErr(m.charAt(0).toUpperCase() + m.slice(1)); } } });
    }, 350);
    return () => { live = false; clearTimeout(t); };
  }, [cs, byString]);

  const save = async () => {
    setBusy(true);
    setErr(null);
    const options = sf ? { warehouse: f.warehouse || undefined, role: f.role || undefined } : { encrypt: f.encrypt, trust_server_certificate: f.trust };
    try {
      if (editing) {
        const r = await patch<{ connection: DbConn; ok: boolean; error: string | null }>(`/api/databases/${editing.id}`,
          { name: f.name, port: f.port ? Number(f.port) : null, username: f.username, secret: f.secret, options });
        if (!r.ok) { setErr(r.error ?? "Couldn't connect with these details."); onSaved(r.connection, []); return; }
        toast.success("Reconnected", { description: r.connection.name });
        onSaved(r.connection, []);
      } else {
        const r = byString
          ? await post<{ connection: DbConn; can_write: boolean | null; suggestions: Suggestion[] }>("/api/databases/from-string", { connection_string: cs, name: f.name })
          : await post<{ connection: DbConn; can_write: boolean | null; suggestions: Suggestion[] }>("/api/databases", {
            kind, name: f.name, host: f.host.trim(), port: f.port ? Number(f.port) : null, database: f.database.trim(), auth: f.auth,
            username: f.username.trim(), secret: f.secret, options });
        toast.success(`Connected to ${r.connection.database}`, {
          description: r.suggestions.length ? `Your pipelines load ${r.suggestions.length} table${r.suggestions.length > 1 ? "s" : ""} here: map them to start checking.` : "It stays connected: the bot reconnects whenever you sign in." });
        onSaved(r.connection, r.suggestions);
      }
      onClose();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
      setF((x) => ({ ...x, secret: "" }));
    }
  };
  const ready = editing ? true : byString ? Boolean(preview && !previewErr) : Boolean(f.host.trim() && f.database.trim() && (f.auth === "entra" || (f.username.trim() && f.secret)));

  return (
    <Modal open={open} onClose={onClose} icon={sf ? Snowflake : Database}
      title={editing ? `Edit ${editing.name}` : "Connect a database"}
      subtitle={editing ? "Change the sign-in or options. Leave the password empty to keep the current one." : "Once connected it stays connected: the bot reconnects every time you sign in."}
      footer={<>
        {err && <p className="mr-auto flex min-w-0 items-start gap-1.5 text-xs text-[var(--critical-ink)]"><CircleAlert className="mt-0.5 size-3.5 shrink-0" />{err}</p>}
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!ready || busy} onClick={() => void save()}>
          {busy ? <Loader2 className="animate-spin" /> : <Plus />}{busy ? "Connecting…" : editing ? "Save and reconnect" : "Test and connect"}
        </Button>
      </>}>
      <div className="space-y-4">
        {!editing && (
          <Segmented label="How to connect" value={mode} onChange={setMode}
            options={[{ value: "fields" as const, label: "Enter the details" }, { value: "string" as const, label: "Paste a connection string" }]} />
        )}
        {byString && (
          <>
            <Field label="Connection string" hint="SQL Server (ADO.NET, ODBC, JDBC or mssql://) or Snowflake (snowflake://, JDBC or account=…). Only its parts are kept, the password encrypted; the string itself isn't stored.">
              <div className="relative">
                <textarea value={cs} onChange={(e) => setCs(e.target.value)} rows={4} spellCheck={false} autoComplete="off" aria-label="Connection string"
                  className={cn("w-full resize-y rounded-lg border border-line-strong bg-surface-2 px-2.5 py-2 pr-16 font-mono text-xs leading-5 text-ink light:bg-surface", !showCs && "masked")}
                  placeholder="Server=tcp:myserver,1433;Initial Catalog=SalesDB;User ID=reader;Password=…;Encrypt=True;" />
                <button type="button" onClick={() => setShowCs((x) => !x)} className="absolute right-2 top-2 rounded-md px-2 py-1 text-[0.6875rem] font-medium text-ink-3 hover:bg-surface-3 hover:text-ink">
                  {showCs ? "Hide" : "Show"}
                </button>
              </div>
            </Field>
            {previewErr && <p className="flex items-start gap-1.5 rounded-xl bg-[var(--critical-soft)] px-3 py-2 text-xs text-ink"><CircleAlert className="mt-0.5 size-3.5 shrink-0 text-[var(--critical-ink)]" />{previewErr}</p>}
            {preview && <PreviewCard p={preview} />}
            <Field label="Display name" hint="Optional"><Input value={f.name} onChange={set("name")} placeholder={preview ? `${preview.database} on ${preview.host}` : "e.g. Sales reporting"} className="w-full" /></Field>
          </>
        )}
        {!byString && <>
        {!editing && (
          <Segmented label="Database type" value={kind} onChange={(v) => { setKind(v); setF((x) => ({ ...x, auth: v === "snowflake" ? "password" : "sql" })); }}
            options={[{ value: "sqlserver" as DbKind, label: <span className="inline-flex items-center gap-1.5"><Database className="size-3.5" />SQL Server / Azure SQL</span> },
              { value: "snowflake" as DbKind, label: <span className="inline-flex items-center gap-1.5"><Snowflake className="size-3.5" />Snowflake</span> }]} />
        )}
        <div className="grid gap-3 sm:grid-cols-[1fr_7rem]">
          <Field label={sf ? "Account identifier" : "Server"} hint={sf ? "e.g. myorg-myaccount (no .snowflakecomputing.com)" : "Host name or IP address, e.g. myserver.database.windows.net"}>
            <Input value={f.host} onChange={set("host")} disabled={Boolean(editing)} placeholder={sf ? "e.g. myorg-account" : "e.g. sales-db.database.windows.net"} className="w-full" autoComplete="off" spellCheck={false} />
          </Field>
          {!sf && <Field label="Port"><Input value={f.port} onChange={set("port")} placeholder="1433 (default)" inputMode="numeric" className="w-full" /></Field>}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Database"><Input value={f.database} onChange={set("database")} disabled={Boolean(editing)} placeholder={sf ? "e.g. DB_PROD" : "e.g. SalesDB"} className="w-full" spellCheck={false} /></Field>
          <Field label="Display name" hint="Optional"><Input value={f.name} onChange={set("name")} placeholder={f.database ? `${f.database} on ${f.host || "…"}` : "e.g. Sales reporting"} className="w-full" /></Field>
        </div>
        {sf && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Warehouse"><Input value={f.warehouse} onChange={set("warehouse")} placeholder="COMPUTE_WH" className="w-full" /></Field>
            <Field label="Role" hint="Optional: your default role otherwise"><Input value={f.role} onChange={set("role")} placeholder="ANALYST" className="w-full" /></Field>
          </div>
        )}
        <div>
          <span className="mb-1 block text-xs font-medium text-ink-2">Sign-in</span>
          <Segmented label="Sign-in" size="sm" value={f.auth} onChange={(v) => setF((x) => ({ ...x, auth: v }))}
            options={sf ? [{ value: "password", label: "User and password" }, { value: "keypair", label: "Key pair" }]
              : [{ value: "sql", label: "SQL login" }, { value: "entra", label: "My Microsoft account (Azure SQL)" }]} />
        </div>
        {f.auth !== "entra" ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="User name"><Input value={f.username} onChange={set("username")} autoComplete="username" className="w-full" spellCheck={false} /></Field>
            {f.auth === "keypair" ? null : (
              <Field label="Password" hint={editing ? "Leave empty to keep the current one" : undefined}>
                <Input type="password" value={f.secret} onChange={set("secret")} autoComplete="new-password" className="w-full" />
              </Field>
            )}
          </div>
        ) : (
          <p className="rounded-xl bg-surface-2 px-3.5 py-2.5 text-xs text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">
            The bot uses your Microsoft sign-in to this portal. It works for Azure SQL and Azure SQL Managed Instance, not for SQL Server on a VM.
          </p>
        )}
        {f.auth === "keypair" && (
          <Field label="Private key (PEM)" hint="Unencrypted PKCS#8. Stored encrypted; never shown again.">
            <textarea value={f.secret} onChange={set("secret")} rows={4} spellCheck={false}
              className="w-full rounded-lg border border-line-strong bg-surface-2 px-2.5 py-2 font-mono text-xs text-ink light:bg-surface" placeholder="-----BEGIN PRIVATE KEY-----" />
          </Field>
        )}
        {!sf && (
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-ink-2">
            <label className="inline-flex items-center gap-2"><input type="checkbox" checked={f.encrypt} onChange={(e) => setF((x) => ({ ...x, encrypt: e.target.checked }))} className="accent-[var(--accent)]" />Encrypt the connection</label>
            <label className="inline-flex items-center gap-2"><input type="checkbox" checked={f.trust} onChange={(e) => setF((x) => ({ ...x, trust: e.target.checked }))} className="accent-[var(--accent)]" />Trust the server's own certificate</label>
            {f.trust && <span className="basis-full text-[0.6875rem] text-[var(--warning-ink)]">Only for servers with a self-signed certificate: the connection stays encrypted but the server isn't verified.</span>}
          </div>
        )}
        </>}
        <PrivacyPromise compact />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- map a table (and its check settings)
function Chips({ options, value, onChange, empty, tone }: { options: Column[]; value: string[]; onChange: (v: string[]) => void; empty?: string; tone?: "warn" }) {
  if (!options.length) return <p className="text-xs text-ink-3">{empty ?? "No columns of this kind."}</p>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((c) => {
        const on = value.includes(c.name);
        return (
          <button key={c.name} type="button" aria-pressed={on} onClick={() => onChange(on ? value.filter((x) => x !== c.name) : [...value, c.name])}
            title={c.type}
            className={cn("rounded-full px-2.5 py-1 text-[0.71875rem] font-medium transition-colors",
              on ? (tone === "warn" ? "bg-[var(--warning-soft)] text-[var(--warning-ink)] shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--warning)_45%,transparent)]"
                : "bg-accent-soft text-ink shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--accent)_50%,transparent)]")
                : "bg-surface-2 text-ink-2 shadow-[inset_0_0_0_1px_var(--line)] hover:text-ink")}>
            {c.name}
          </button>
        );
      })}
    </div>
  );
}

const Section = ({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) => (
  <section className="border-t border-line pt-4">
    <h3 className="text-[0.8125rem] font-semibold text-ink">{title}</h3>
    {hint && <p className="mb-2 text-[0.71875rem] leading-4 text-ink-3">{hint}</p>}
    {children}
  </section>
);

const OPS: [string, string][] = [[">", "greater than"], [">=", "at least"], ["<", "less than"], ["<=", "at most"], ["=", "equals"], ["<>", "not equal to"],
  ["between", "between"], ["in", "one of"], ["not_in", "none of"]];

export function MappingDialog({ open, onClose, projectKey, pipelines, connections, start, editing, onSaved }: {
  open: boolean; onClose: () => void; projectKey: string; pipelines: string[]; connections: DbConn[];
  start?: Partial<{ pipeline: string; connection_id: number; schema: string; table: string }>; editing?: Mapping | null; onSaved: () => void;
}) {
  const [pipeline, setPipeline] = useState("");
  const [connId, setConnId] = useState<number | "">("");
  const [tables, setTables] = useState<{ schema: string; table: string; kind: string }[] | null>(null);
  const [filter, setFilter] = useState("");
  const [picked, setPicked] = useState<{ schema: string; table: string } | null>(null);
  const [cols, setCols] = useState<Column[] | null>(null);
  const [s, setS] = useState<MappingSettings>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setErr(null); setFilter(""); setCols(null); setTables(null);
    if (editing) {
      setPipeline(editing.pipeline); setConnId(editing.connection_id); setPicked({ schema: editing.schema, table: editing.table }); setS(editing.settings ?? {});
    } else {
      setPipeline(start?.pipeline ?? pipelines[0] ?? ""); setConnId(start?.connection_id ?? connections[0]?.id ?? "");
      setPicked(start?.schema && start?.table ? { schema: start.schema, table: start.table } : null); setS({});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);

  const mine = connections.find((c) => c.id === connId);
  useEffect(() => {  // the tables this login can see
    if (!open || !connId || editing) return;
    let live = true;
    setTables(null);
    api<{ tables: { schema: string; table: string; kind: string }[] }>(`/api/databases/${connId}/tables`)
      .then((r) => live && setTables(r.tables)).catch((e) => live && setErr((e as Error).message));
    return () => { live = false; };
  }, [open, connId, editing]);
  useEffect(() => {  // the picked table's columns (names and types only)
    if (!open || !picked || !connId || !mine) { setCols(null); return; }  // columns come through the owner's own connection
    let live = true;
    setCols(null);
    api<{ schema: string; table: string; columns: Column[] }>(`/api/databases/${connId}/columns?schema=${encodeURIComponent(picked.schema)}&table=${encodeURIComponent(picked.table)}`)
      .then((r) => {
        if (!live) return;
        setCols(r.columns);
        setPicked({ schema: r.schema, table: r.table });
        if (!editing) setS((x) => ({ ...x, sensitive: x.sensitive ?? r.columns.filter((c) => LOOKS_SENSITIVE.test(c.name)).map((c) => c.name) }));
      })
      .catch((e) => live && setErr((e as Error).message));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, connId, picked?.schema, picked?.table]);

  const by = (kind: Column["kind"] | "any") => (cols ?? []).filter((c) => kind === "any" || c.kind === kind);
  const sensitive = s.sensitive ?? [];
  const visible = useMemo(() => (tables ?? []).filter((t) => `${t.schema}.${t.table}`.toLowerCase().includes(filter.toLowerCase())).slice(0, 200), [tables, filter]);
  const upd = (patchS: Partial<MappingSettings>) => setS((x) => ({ ...x, ...patchS }));

  const save = async () => {
    if (!picked || !connId) return;
    setBusy(true);
    setErr(null);
    const clean: MappingSettings = {
      ...s,
      stats: (s.stats ?? []).filter((c) => !sensitive.includes(c)),
      rules: (s.rules ?? []).filter((r) => r.name && r.column && r.values.length && r.values.every((v) => v !== "")),
      ranges: (s.ranges ?? []).filter((r) => r.column && (r.min != null || r.max != null)),
      typed: (s.typed ?? []).filter((t) => t.column),
    };
    try {
      if (editing) await patch(`/api/projects/${projectKey}/mappings/${editing.id}`, { pipeline, settings: clean });
      else await post(`/api/projects/${projectKey}/mappings`, { pipeline, connection_id: connId, schema: picked.schema, table: picked.table, settings: clean });
      toast.success(editing ? "Mapping saved" : `${picked.table} mapped to ${pipeline}`, { description: "Its data is checked after each load, and now with Check now." });
      onSaved();
      onClose();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} wide icon={ShieldCheck}
      title={editing ? `${editing.schema}.${editing.table}` : "Map a table to its pipeline"}
      subtitle={editing ? `Loaded by ${editing.pipeline} · on ${editing.connection}` : "Which pipeline loads which table: the table's data is checked after each of that pipeline's loads."}
      footer={<>
        {err && <p className="mr-auto flex min-w-0 items-start gap-1.5 text-xs text-[var(--critical-ink)]"><CircleAlert className="mt-0.5 size-3.5 shrink-0" />{err}</p>}
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!picked || !pipeline || !connId || busy} onClick={() => void save()}>
          {busy ? <Loader2 className="animate-spin" /> : <CircleCheck />}{editing ? "Save" : "Map table"}
        </Button>
      </>}>
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Pipeline">
            <Select value={pipeline} onChange={(e) => setPipeline(e.target.value)} className="w-full">
              {!pipelines.includes(pipeline) && pipeline && <option value={pipeline}>{pipeline}</option>}
              {pipelines.map((p) => <option key={p}>{p}</option>)}
            </Select>
          </Field>
          <Field label="Database">
            <Select value={connId} onChange={(e) => { setConnId(Number(e.target.value)); setPicked(null); }} disabled={Boolean(editing)} className="w-full">
              {editing && !mine && <option value={editing.connection_id}>{editing.connection}</option>}
              {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({KIND_LABEL[c.kind]})</option>)}
            </Select>
          </Field>
        </div>
        {!editing && (
          <Field label="Table" hint={picked ? <>Selected <b className="text-ink">{picked.schema}.{picked.table}</b></> : "Pick the table this pipeline writes."}>
            <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={tables ? `Filter ${tables.length} tables and views…` : "Loading tables…"} className="mb-1.5 w-full" />
            <div className="scrollbar-thin max-h-44 overflow-y-auto rounded-xl bg-surface-2 p-1 shadow-[inset_0_0_0_1px_var(--line)]" role="listbox" aria-label="Tables">
              {!tables ? <p className="flex items-center gap-2 px-2 py-2 text-xs text-ink-3"><Loader2 className="size-3.5 animate-spin" />Reading the table list (names only)…</p>
                : !visible.length ? <p className="px-2 py-2 text-xs text-ink-3">No table matches.</p>
                : visible.map((t) => {
                  const on = picked?.schema === t.schema && picked?.table === t.table;
                  return (
                    <button key={`${t.schema}.${t.table}`} type="button" role="option" aria-selected={on} onClick={() => setPicked({ schema: t.schema, table: t.table })}
                      className={cn("flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs", on ? "bg-accent-soft text-ink" : "text-ink-2 hover:bg-surface-3 hover:text-ink")}>
                      <span className="text-ink-3">{t.schema}.</span><span className="font-medium">{t.table}</span>
                      {t.kind === "view" && <span className="ml-auto rounded bg-surface-3 px-1 text-[0.625rem] text-ink-3">view</span>}
                    </button>
                  );
                })}
            </div>
          </Field>
        )}

        {editing && !mine && <p className="rounded-xl bg-surface-2 px-3.5 py-2.5 text-xs text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">Only the person who connected {editing.connection} can change this table's checks; you can still pause or remove the mapping.</p>}
        {picked && mine && !cols && !err && <p className="flex items-center gap-2 text-xs text-ink-3"><Loader2 className="size-3.5 animate-spin" />Reading {picked.table}'s columns (names and types only)…</p>}
        {cols && (
          <>
            <Section title="Sensitive columns" hint="Only ever counted as empty or filled: no statistics, no value mix. Columns that look personal are marked for you.">
              <Chips options={by("any")} value={sensitive} onChange={(v) => upd({ sensitive: v })} tone="warn" />
            </Section>
            <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
              <Section title="Business key" hint="Duplicates are rows sharing these values (empty keys are counted separately).">
                <Chips options={by("any").filter((c) => c.kind !== "other")} value={s.key ?? []} onChange={(v) => upd({ key: v })} />
              </Section>
              <Section title="Load date column" hint="Decides which rows are today's. Found automatically if left on Automatic.">
                <Select value={s.date_column ?? ""} onChange={(e) => upd({ date_column: e.target.value || null })} className="w-full">
                  <option value="">Automatic</option>
                  {by("date").map((c) => <option key={c.name}>{c.name}</option>)}
                </Select>
              </Section>
              <Section title="Should never be empty" hint="An empty value here is flagged even before the bot has learned what's normal.">
                <Chips options={by("any")} value={s.required ?? []} onChange={(v) => upd({ required: v })} />
              </Section>
              <Section title="Statistics allowed" hint="Min, max, average and spread, for numbers that aren't sensitive (and only with 10+ rows).">
                <Chips options={by("number").filter((c) => !sensitive.includes(c.name))} value={(s.stats ?? []).filter((c) => !sensitive.includes(c))} onChange={(v) => upd({ stats: v })} empty="No number columns." />
              </Section>
            </div>
            <Section title="Business rules" hint="A condition every row should meet. Values are passed safely; they're never part of the query text.">
              <div className="space-y-2">
                {(s.rules ?? []).map((r, i) => (
                  <div key={i} className="grid grid-cols-[1fr_1fr_8rem_1fr_auto] items-center gap-2">
                    <Input value={r.name} placeholder="Rule name" onChange={(e) => upd({ rules: (s.rules ?? []).map((x, j) => j === i ? { ...x, name: e.target.value } : x) })} />
                    <Select value={r.column} onChange={(e) => upd({ rules: (s.rules ?? []).map((x, j) => j === i ? { ...x, column: e.target.value } : x) })}>
                      <option value="">Column…</option>{by("any").filter((c) => c.kind !== "other").map((c) => <option key={c.name}>{c.name}</option>)}
                    </Select>
                    <Select value={r.op} onChange={(e) => upd({ rules: (s.rules ?? []).map((x, j) => j === i ? { ...x, op: e.target.value } : x) })}>
                      {OPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                    </Select>
                    <Input value={r.values.join(", ")} placeholder={r.op === "between" ? "0, 100" : r.op.includes("in") ? "A, B, C" : "0"}
                      onChange={(e) => upd({ rules: (s.rules ?? []).map((x, j) => j === i ? { ...x, values: e.target.value.split(",").map((v) => v.trim()).filter((v, k, a) => v !== "" || k < a.length - 1).map((v) => (v !== "" && !Number.isNaN(Number(v)) ? Number(v) : v)) } : x) })} />
                    <button type="button" onClick={() => upd({ rules: (s.rules ?? []).filter((_, j) => j !== i) })} className="flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="Remove rule"><Trash2 className="size-3.5" /></button>
                  </div>
                ))}
                <Button size="sm" variant="ghost" onClick={() => upd({ rules: [...(s.rules ?? []), { name: "", column: "", op: ">", values: [] }] })}><Plus />Add a rule</Button>
              </div>
            </Section>
            <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
              <Section title="Allowed ranges" hint="Rows outside them are counted.">
                <div className="space-y-2">
                  {(s.ranges ?? []).map((r, i) => (
                    <div key={i} className="grid grid-cols-[1fr_4.5rem_4.5rem_auto] items-center gap-2">
                      <Select value={r.column} onChange={(e) => upd({ ranges: (s.ranges ?? []).map((x, j) => j === i ? { ...x, column: e.target.value } : x) })}>
                        <option value="">Column…</option>{by("number").map((c) => <option key={c.name}>{c.name}</option>)}
                      </Select>
                      <Input value={r.min ?? ""} placeholder="min" inputMode="decimal" onChange={(e) => upd({ ranges: (s.ranges ?? []).map((x, j) => j === i ? { ...x, min: e.target.value === "" ? null : Number(e.target.value) } : x) })} />
                      <Input value={r.max ?? ""} placeholder="max" inputMode="decimal" onChange={(e) => upd({ ranges: (s.ranges ?? []).map((x, j) => j === i ? { ...x, max: e.target.value === "" ? null : Number(e.target.value) } : x) })} />
                      <button type="button" onClick={() => upd({ ranges: (s.ranges ?? []).filter((_, j) => j !== i) })} className="flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-3" aria-label="Remove range"><Trash2 className="size-3.5" /></button>
                    </div>
                  ))}
                  <Button size="sm" variant="ghost" onClick={() => upd({ ranges: [...(s.ranges ?? []), { column: "", min: null, max: null }] })}><Plus />Add a range</Button>
                </div>
              </Section>
              <Section title="Text that must be a type" hint="Staging columns stored as text that must read as a number or a date.">
                <div className="space-y-2">
                  {(s.typed ?? []).map((t, i) => (
                    <div key={i} className="grid grid-cols-[1fr_6.5rem_auto] items-center gap-2">
                      <Select value={t.column} onChange={(e) => upd({ typed: (s.typed ?? []).map((x, j) => j === i ? { ...x, column: e.target.value } : x) })}>
                        <option value="">Column…</option>{by("text").map((c) => <option key={c.name}>{c.name}</option>)}
                      </Select>
                      <Select value={t.type} onChange={(e) => upd({ typed: (s.typed ?? []).map((x, j) => j === i ? { ...x, type: e.target.value as "int" } : x) })}>
                        <option value="int">whole number</option><option value="decimal">decimal</option><option value="date">date</option>
                      </Select>
                      <button type="button" onClick={() => upd({ typed: (s.typed ?? []).filter((_, j) => j !== i) })} className="flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-3" aria-label="Remove"><Trash2 className="size-3.5" /></button>
                    </div>
                  ))}
                  <Button size="sm" variant="ghost" onClick={() => upd({ typed: [...(s.typed ?? []), { column: "", type: "decimal" }] })}><Plus />Add a column</Button>
                </div>
              </Section>
            </div>
            <Section title="Timing">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Newest row may be at most (hours)" hint="Empty: learned from how often new rows arrive.">
                  <Input value={s.freshness_hours ?? ""} inputMode="decimal" placeholder="learned" onChange={(e) => upd({ freshness_hours: e.target.value === "" ? null : Number(e.target.value) })} className="w-full" />
                </Field>
                <Field label="Also re-check every (hours)" hint="It's always checked after each new load.">
                  <Input value={s.every_hours ?? 6} inputMode="decimal" onChange={(e) => upd({ every_hours: Number(e.target.value) || 6 })} className="w-full" />
                </Field>
              </div>
            </Section>
          </>
        )}
      </div>
    </Modal>
  );
}

/** One line per mapped table's settings, for lists. */
export function settingsSummary(s: MappingSettings): string[] {
  const out: string[] = [];
  if (s.key?.length) out.push(`key ${s.key.join(", ")}`);
  if (s.rules?.length) out.push(`${s.rules.length} rule${s.rules.length > 1 ? "s" : ""}`);
  if (s.ranges?.length) out.push(`${s.ranges.length} range${s.ranges.length > 1 ? "s" : ""}`);
  if (s.typed?.length) out.push(`${s.typed.length} typed`);
  if (s.sensitive?.length) out.push(`${s.sensitive.length} sensitive`);
  return out;
}
