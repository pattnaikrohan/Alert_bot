// Suspicious values, column by column: what the rules bot understood each column to hold (anyone can correct it, and
// the bot remembers), the rules it made from that (anyone can edit, delete or add), how many rows break each, and, for
// the person who connected the database, the values themselves (fetched on demand, shown to them only, kept nowhere).
import {
  Bot, Brain, Check, ChevronDown, Eye, EyeOff, FileWarning, Loader2, Lock, Pencil, Plus, RefreshCw, RotateCcw, Search, ShieldAlert, Sparkles,
  Table2, Trash2, Type, User, Wand2, X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Notice } from "@/components/Cards";
import { Modal } from "@/components/Databases";
import { Button } from "@/components/ui/button";
import { EmptyState, Input, Segmented, Select, Skeleton } from "@/components/ui/primitives";
import { api, del, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

type ColType = "text" | "number" | "date";
type Kind = "no_digits" | "no_symbols" | "trimmed" | "length" | "uppercase" | "placeholder" | "format" | "range" | "date_sane"
  | "pattern" | "forbid" | "allowed_values" | "lowercase";
type CharClass = "digits" | "letters" | "symbols" | "spaces";
interface Rule {
  id: string; column: string; kind: Kind; min: number | null; max: number | null; format: string | null; min_year: number | null; future_days: number | null;
  shapes: string[] | null; chars: CharClass[] | null; allowed: string[] | null;
  reason: string; confidence: "high" | "medium" | "low"; source: "ai" | "names" | "person"; enabled: boolean; label: string;
  count: number | null; count_today: number | null;
}
interface Meaning { text: string; by: "ai" | "names" | "person"; at: string; who?: string }
interface TableT {
  id: number; schema: string; table: string; pipeline: string; database: string; connection: string; kind: string; checked: boolean; owner: boolean;
  sensitive: string[]; rules: Rule[]; generated_at: string | null; by: "ai" | "names" | "person" | null; model: string | null; note: string | null;
  dropped: string[]; checked_at: string | null; rows: number | null; rows_today: number | null; date_column: string | null; columns: Record<string, ColType>;
  error: string | null; suspicious: number; suspicious_today: number; rules_hit: number;
  change_note: string | null; columns_at: string | null; meanings: Record<string, Meaning>; removed: number;
  column_note: { column: string; by: string; rules: number; at: string; note: string | null } | null;
}
interface ListT { tables: TableT[]; kinds: Record<Kind, ColType[]>; formats: Record<string, string>; max_values: number }
interface Values { values: { value: string; count: number; length: number }[]; scope: string; limit: number; note: string }

const nf = new Intl.NumberFormat("en-US");
const KIND_LABEL: Record<Kind, string> = {
  no_digits: "No digits", no_symbols: "No symbols (@ # $ % * _)", trimmed: "No spaces at the start or end", uppercase: "In capitals",
  placeholder: "Not a placeholder (N/A, NULL, TBD…)", length: "Length (characters)", format: "A known format", range: "Between two numbers",
  date_sane: "A believable date", pattern: "Has a shape (like LL999)", forbid: "Never has certain characters",
  allowed_values: "Is one of a list of values", lowercase: "In lower case",
};
const SOURCE: Record<Rule["source"], { label: string; icon: typeof Bot; tip: string }> = {
  ai: { label: "rules bot", icon: Sparkles, tip: "Made by the rules bot from what it understood the column to hold" },
  names: { label: "from the name", icon: Type, tip: "From the column's name alone (the rules bot's fallback)" },
  person: { label: "the team's", icon: User, tip: "Added or changed by someone on the team: the bot never replaces it" },
};
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export function SuspiciousValues({ projectKey }: { projectKey: string }) {
  const { data, error, reload, setData } = useApi<ListT>(`/api/projects/${projectKey}/suspicious`, { refreshMs: 60000 });
  if (error && !data) return <Notice tone="warn">Couldn't load suspicious values: {error.message} <button className="ml-2 underline" onClick={reload}>Retry</button></Notice>;
  if (!data) return <div className="space-y-4"><Skeleton className="h-28 w-full" /><Skeleton className="h-72 w-full" /></div>;
  const replace = (t: TableT) => setData({ ...data, tables: data.tables.map((x) => (x.id === t.id ? t : x)) });
  const total = data.tables.reduce((a, t) => a + t.suspicious, 0);
  const hit = data.tables.filter((t) => t.suspicious > 0).length;
  if (!data.tables.length) {
    return (
      <EmptyState icon={Table2} action={<Button asChild variant="primary"><Link to={`/p/${projectKey}/tables?tab=quality`}>Choose tables to check</Link></Button>}>
        No table is checked in this project yet. Once a table is checked, the rules bot works out what each column holds and what its values should look like, and anything that doesn't fit shows up here.
      </EmptyState>
    );
  }
  return (
    <div className="space-y-5">
      <section className="sv-hero card-in rounded-[1.5rem] p-5 sm:p-6">
        <div className="flex flex-wrap items-start gap-4">
          <span className="flex size-11 shrink-0 items-center justify-center rounded-2xl bg-[var(--warning-soft)] text-[var(--warning)]"><FileWarning className="size-5" aria-hidden /></span>
          <div className="min-w-0 flex-1">
            <h2 className="display text-[1.375rem] font-bold leading-tight text-ink">
              {total ? <>{nf.format(total)} suspicious value{total === 1 ? "" : "s"} <span className="font-semibold text-ink-2">in {hit} of {data.tables.length} table{data.tables.length === 1 ? "" : "s"}</span></> : "No suspicious values"}
            </h2>
            <p className="mt-1 max-w-3xl text-[0.875rem] leading-6 text-ink-2">Values that don't look right for their column, like a digit in a port name. Every column, every table: the rules bot reads what each one holds and writes rules from that, and you can correct both.</p>
          </div>
        </div>
        <ol className="mt-4 grid gap-2 sm:grid-cols-3">
          <Step n={1} icon={Brain} title="It reads each column">From the table's and column's names only, the bot says what the column holds. Wrong? Edit the meaning: the bot remembers it.</Step>
          <Step n={2} icon={Wand2} title="It writes rules">From that meaning: "no digits", "looks like SGSIN", "is IMPORT or EXPORT"… Edit, delete or add any rule.</Step>
          <Step n={3} icon={ShieldAlert} title="It counts what breaks them">Everyone sees the counts. The person who connected the database can open the values; they're never kept or sent to AI.</Step>
        </ol>
      </section>
      {data.tables.map((t, i) => <TableCard key={t.id} t={t} i={i} projectKey={projectKey} meta={data} onChange={replace} />)}
    </div>
  );
}

const Step = ({ n, icon: I, title, children }: { n: number; icon: typeof Bot; title: string; children: ReactNode }) => (
  <li className="flex gap-2.5 rounded-xl bg-surface/70 p-3 shadow-[inset_0_0_0_1px_var(--line)]">
    <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent"><I className="size-4" aria-hidden /></span>
    <div className="min-w-0"><p className="text-[0.8125rem] font-semibold text-ink"><span className="text-ink-3">{n}.</span> {title}</p><p className="text-[0.75rem] leading-[1.125rem] text-ink-3">{children}</p></div>
  </li>
);

// ── one table ────────────────────────────────────────────────────────────────
type Show = "suspicious" | "rules" | "all";

function TableCard({ t, i, projectKey, meta, onChange }: { t: TableT; i: number; projectKey: string; meta: ListT; onChange: (t: TableT) => void }) {
  const [busy, setBusy] = useState<"gen" | "check" | null>(null);
  const [open, setOpen] = useState(i === 0 || t.suspicious > 0);
  const [show, setShow] = useState<Show>(t.suspicious ? "suspicious" : "rules");
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<{ rule?: Rule; column?: string } | null>(null);
  const base = `/api/projects/${projectKey}/suspicious/${t.id}`;
  const run = async (what: "gen" | "check") => {
    setBusy(what);
    try {
      const r = what === "gen" ? await post<TableT>(`${base}/rules`, { ai: true }) : await post<TableT>(`${base}/check`);
      onChange(r);
      toast.success(what === "gen" ? (r.by === "ai" ? `The rules bot read ${Object.keys(r.meanings).length} columns and set ${r.rules.length} rules` : `${r.rules.length} rules from the column names`) : "Checked",
        { description: r.suspicious ? `${nf.format(r.suspicious)} suspicious value${r.suspicious === 1 ? "" : "s"} found` : "Nothing suspicious" });
    } catch (e) { toast.error("Couldn't do that", { description: (e as Error).message }); } finally { setBusy(null); }
  };
  const byCol = useMemo(() => {
    const m = new Map<string, Rule[]>();
    for (const r of t.rules) m.set(r.column, [...(m.get(r.column) ?? []), r]);
    return m;
  }, [t.rules]);
  const colSusp = (c: string) => (byCol.get(c) ?? []).filter((r) => r.enabled).reduce((a, r) => a + (r.count ?? 0), 0);
  const cols = useMemo(() => {
    const names = [...new Set([...Object.keys(t.columns), ...byCol.keys()])];
    const needle = q.trim().toLowerCase();
    return names
      .filter((c) => !needle || c.toLowerCase().includes(needle) || (t.meanings[c]?.text ?? "").toLowerCase().includes(needle))
      .filter((c) => show === "all" || (show === "rules" ? (byCol.get(c)?.length ?? 0) > 0 : colSusp(c) > 0))
      .sort((a, b) => colSusp(b) - colSusp(a) || (byCol.get(b)?.length ?? 0) - (byCol.get(a)?.length ?? 0) || a.localeCompare(b));
  }, [t, byCol, q, show]); // eslint-disable-line react-hooks/exhaustive-deps
  const withRules = [...byCol.keys()].length;
  const total = Object.keys(t.columns).length;
  return (
    <section className="card-in overflow-hidden rounded-[1.5rem] border border-line bg-surface shadow-[var(--elev-1)]" style={{ animationDelay: `${i * 50}ms` }}>
      <header className="flex flex-wrap items-start gap-3 px-5 py-4 sm:px-6">
        <button className="flex min-w-0 flex-1 items-start gap-3 text-left" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className={cn("flex size-10 shrink-0 items-center justify-center rounded-xl", t.suspicious ? "bg-[var(--warning-soft)] text-[var(--warning)]" : "bg-[var(--good-soft)] text-[var(--good)]")}>
            {t.suspicious ? <FileWarning className="size-5" aria-hidden /> : <Check className="size-5" aria-hidden />}
          </span>
          <span className="min-w-0 flex-1">
            <span className="display flex flex-wrap items-center gap-2 text-[1.0625rem] font-semibold text-ink">
              <span className="[overflow-wrap:anywhere]"><span className="font-normal text-ink-3">{t.schema}.</span>{t.table}</span>
              {t.suspicious > 0 ? <span className="sv-count">{nf.format(t.suspicious)} suspicious</span> : t.checked_at ? <span className="ai-chip good">all values fit</span> : null}
              {!t.checked && <span className="ai-chip">not checked now</span>}
            </span>
            <span className="mt-0.5 block text-[0.8125rem] text-ink-3">
              {t.database} on {t.connection} · {t.rows != null ? `${nf.format(t.rows)} rows · ` : ""}{withRules} of {total} columns have rules
              {t.checked_at ? ` · checked ${ago(t.checked_at)}` : " · not checked yet"}
            </span>
          </span>
          <ChevronDown className={cn("mt-2 size-5 shrink-0 text-ink-3 transition-transform", open && "rotate-180")} aria-hidden />
        </button>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => void run("check")} disabled={busy != null || !t.rules.length}>{busy === "check" ? <Loader2 className="animate-spin" /> : <RefreshCw />}Check now</Button>
          <Button size="sm" variant="secondary" onClick={() => void run("gen")} disabled={busy != null} title="The bot reads every column again (your meanings and your own rules stay)">
            {busy === "gen" ? <Loader2 className="animate-spin" /> : <Bot />}{t.generated_at ? "Re-read all columns" : "Ask the rules bot"}
          </Button>
        </div>
      </header>
      {open && (
        <div className="border-t border-line">
          {t.note && <Notice className="mx-5 mt-4 sm:mx-6">{t.note}</Notice>}
          {t.change_note && <Notice className="mx-5 mt-4 sm:mx-6"><Sparkles className="mr-1 inline size-3.5 text-accent" />{t.change_note}</Notice>}
          {t.error && <Notice tone="warn" className="mx-5 mt-4 sm:mx-6">The last check didn't finish: {t.error}. The last good counts are shown.</Notice>}
          <div className="flex flex-wrap items-center gap-2 px-5 pt-4 sm:px-6">
            <Segmented<Show> size="sm" label="Which columns" value={show} onChange={setShow}
              options={[{ value: "suspicious", label: "With suspicious values" }, { value: "rules", label: "With rules" }, { value: "all", label: `All ${total} columns` }]} />
            <label className="relative ml-auto w-full sm:w-64">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
              <Input className="h-9 w-full pl-8 text-[0.875rem]" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a column or meaning" aria-label="Find a column" />
            </label>
          </div>
          {!t.generated_at && !t.rules.length ? (
            <div className="px-5 py-10 text-center text-[0.9375rem] text-ink-3 sm:px-6">The rules bot hasn't read this table yet: it does within a minute, or ask it now.</div>
          ) : cols.length === 0 ? (
            <div className="px-5 py-10 text-center text-[0.9375rem] text-ink-3 sm:px-6">
              {show === "suspicious" ? <span className="inline-flex items-center gap-2"><Check className="size-4 text-[var(--good)]" />No column has suspicious values. <button className="text-accent underline-offset-2 hover:underline" onClick={() => setShow("all")}>See every column</button></span> : "No column matches."}
            </div>
          ) : (
            <ul className="mt-3 divide-y divide-[var(--line)] border-t border-line">
              {cols.map((c) => (
                <ColumnBlock key={c} column={c} type={t.columns[c]} t={t} rules={byCol.get(c) ?? []} base={base} max={meta.max_values} onChange={onChange}
                  onAdd={() => setEditing({ column: c })} onEdit={(r) => setEditing({ rule: r })} />
              ))}
            </ul>
          )}
          {(t.dropped.length > 0 || t.removed > 0) && (
            <p className="border-t border-line px-5 py-3 text-[0.75rem] text-ink-3 sm:px-6">
              {t.removed > 0 && <>{t.removed} rule{t.removed === 1 ? "" : "s"} the team removed won't be suggested again. </>}
              {t.dropped.length > 0 && <details className="inline"><summary className="inline cursor-pointer">{t.dropped.length} of the bot's ideas were left out</summary>
                <ul className="mt-1.5 list-disc space-y-0.5 pl-5">{t.dropped.map((d) => <li key={d}>{d}</li>)}</ul></details>}
            </p>
          )}
        </div>
      )}
      <RuleModal open={editing != null} rule={editing?.rule} column={editing?.column} t={t} meta={meta} base={base} onClose={() => setEditing(null)}
        onDone={(x) => { onChange(x); setEditing(null); }} />
    </section>
  );
}

// ── one column: what it means, its rules ─────────────────────────────────────
function ColumnBlock({ column, type, t, rules, base, max, onChange, onAdd, onEdit }: {
  column: string; type: ColType | undefined; t: TableT; rules: Rule[]; base: string; max: number; onChange: (t: TableT) => void; onAdd: () => void; onEdit: (r: Rule) => void;
}) {
  const meaning = t.meanings[column];
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(meaning?.text ?? "");
  const [busy, setBusy] = useState<"save" | "rethink" | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (!editing) setText(meaning?.text ?? ""); }, [meaning?.text, editing]);
  useEffect(() => { if (editing) ref.current?.focus(); }, [editing]);
  const susp = rules.filter((r) => r.enabled).reduce((a, r) => a + (r.count ?? 0), 0);
  const on = rules.filter((r) => r.enabled), off = rules.filter((r) => !r.enabled);
  const saveMeaning = async (rethink: boolean, clear = false) => {
    setBusy(rethink ? "rethink" : "save");
    try {
      const r = await api<TableT>(`${base}/meaning`, { method: "PUT", body: JSON.stringify({ column, text: clear ? "" : text.trim(), rethink }) });
      onChange(r);
      setEditing(false);
      const n = r.rules.filter((x) => x.column === column).length;
      toast.success(clear ? "Back to the bot's own reading" : "Meaning saved: the bot will remember it",
        { description: rethink ? `The bot thought again: ${n} rule${n === 1 ? "" : "s"} for ${column}` : undefined });
    } catch (e) { toast.error("Couldn't save the meaning", { description: (e as Error).message }); } finally { setBusy(null); }
  };
  const rethink = async () => {
    setBusy("rethink");
    try {
      const r = await post<TableT>(`${base}/rethink`, { column });
      onChange(r);
      const n = r.rules.filter((x) => x.column === column).length;
      toast.success(`The bot thought again about ${column}`, { description: `${n} rule${n === 1 ? "" : "s"} now${r.column_note?.note ? ` · ${r.column_note.note}` : ""}` });
    } catch (e) { toast.error("Couldn't ask the bot", { description: (e as Error).message }); } finally { setBusy(null); }
  };
  const toggle = async (r: Rule) => {
    try { onChange(await api<TableT>(`${base}/rule/${r.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !r.enabled }) })); }
    catch (e) { toast.error("Couldn't change the rule", { description: (e as Error).message }); }
  };
  const remove = async (r: Rule) => {
    try {
      onChange(await del<TableT>(`${base}/rule/${r.id}`));
      toast.success("Rule deleted", { description: r.source === "person" ? undefined : "The bot won't suggest it again." });
    } catch (e) { toast.error("Couldn't delete the rule", { description: (e as Error).message }); }
  };
  const who = meaning?.by === "person" ? `set by ${meaning.who ?? "the team"}` : meaning?.by === "ai" ? "the rules bot's reading" : meaning ? "guessed from the name" : null;
  return (
    <li className={cn("sv-col", susp > 0 && "is-bad")}>
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[0.9375rem] font-semibold text-ink [overflow-wrap:anywhere]">{column}</span>
            {type && <span className="ai-chip">{type}</span>}
            {susp > 0 ? <span className="sv-count">{nf.format(susp)} suspicious</span> : rules.some((r) => r.enabled && r.count === 0) ? <span className="ai-chip good">values fit</span> : null}
          </p>
          {!editing ? (
            <div className="sv-meaning mt-1.5">
              <Brain className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="text-[0.875rem] text-ink">{meaning ? <>Holds <b className="font-semibold">{meaning.text}</b></> : <span className="text-ink-3">The bot hasn't said what this column holds yet.</span>}</p>
                {who && <p className="text-[0.71875rem] text-ink-3">{meaning?.by === "person" ? <User className="mr-1 inline size-3" /> : <Sparkles className="mr-1 inline size-3" />}{who}{meaning?.at ? ` · ${ago(meaning.at)}` : ""}</p>}
              </div>
              <Button size="sm" variant="ghost" onClick={() => setEditing(true)}><Pencil />{meaning ? "Correct it" : "Say what it holds"}</Button>
            </div>
          ) : (
            <div className="sv-meaning is-editing mt-1.5">
              <div className="min-w-0 flex-1">
                <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor={`m-${t.id}-${column}`}>What does {column} hold? The bot will take your word for it.</label>
                <textarea id={`m-${t.id}-${column}`} ref={ref} className="sv-area" rows={2} maxLength={200} value={text} onChange={(e) => setText(e.target.value)}
                  placeholder="e.g. the UN/LOCODE of the port of loading, like SGSIN" onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && text.trim()) void saveMeaning(true); if (e.key === "Escape") setEditing(false); }} />
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="primary" onClick={() => void saveMeaning(true)} disabled={busy != null || !text.trim()}>{busy === "rethink" ? <Loader2 className="animate-spin" /> : <Wand2 />}Save and rethink the rules</Button>
                  <Button size="sm" variant="secondary" onClick={() => void saveMeaning(false)} disabled={busy != null || !text.trim()}>{busy === "save" ? <Loader2 className="animate-spin" /> : <Check />}Save only</Button>
                  {meaning?.by === "person" && <Button size="sm" variant="ghost" onClick={() => void saveMeaning(true, true)} disabled={busy != null}><RotateCcw />Back to the bot's reading</Button>}
                  <Button size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={busy != null}><X />Cancel</Button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {rules.length > 0 ? (
        <ul className="mt-3 space-y-1.5">
          {on.map((r) => <RuleRow key={r.id} r={r} t={t} open={open === r.id} onOpen={() => setOpen(open === r.id ? null : r.id)} onToggle={() => void toggle(r)}
            onRemove={() => void remove(r)} onEdit={() => onEdit(r)} base={base} max={max} />)}
          {off.map((r) => <RuleRow key={r.id} r={r} t={t} open={false} onOpen={() => undefined} onToggle={() => void toggle(r)} onRemove={() => void remove(r)} onEdit={() => onEdit(r)} base={base} max={max} />)}
        </ul>
      ) : (
        <p className="mt-3 rounded-xl bg-surface-2 px-3 py-2.5 text-[0.8125rem] text-ink-3 shadow-[inset_0_0_0_1px_var(--line)]">
          No rule yet: nothing about this column was certain. Tell the bot what it holds, or add a rule yourself.
        </p>
      )}
      <div className="mt-2.5 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" onClick={onAdd} disabled={!type}><Plus />Add a rule</Button>
        <Button size="sm" variant="ghost" onClick={() => void rethink()} disabled={busy != null || !type} title="The bot thinks again about this column (your meaning and your own rules stay)">
          {busy === "rethink" && !editing ? <Loader2 className="animate-spin" /> : <Wand2 />}Rethink the rules
        </Button>
      </div>
    </li>
  );
}

function RuleRow({ r, t, open, onOpen, onToggle, onRemove, onEdit, base, max }: {
  r: Rule; t: TableT; open: boolean; onOpen: () => void; onToggle: () => void; onRemove: () => void; onEdit: () => void; base: string; max: number;
}) {
  const S = SOURCE[r.source];
  const bad = (r.count ?? 0) > 0;
  const [confirm, setConfirm] = useState(false);
  useEffect(() => { if (!confirm) return; const id = setTimeout(() => setConfirm(false), 3500); return () => clearTimeout(id); }, [confirm]);
  return (
    <li className={cn("sv-rule", bad && r.enabled && "is-bad", !r.enabled && "is-off", open && "is-open")}>
      <div className="flex flex-wrap items-start gap-3">
        <button role="switch" aria-checked={r.enabled} onClick={onToggle} className={cn("sv-switch", r.enabled && "on")} title={r.enabled ? "Switch this rule off" : "Switch this rule on"}
          aria-label={`${r.enabled ? "Switch off" : "Switch on"}: ${r.column} ${r.label}`}><span /></button>
        <div className="min-w-0 flex-1">
          <p className="text-[0.875rem] font-semibold text-ink">{cap(r.label)}{!r.enabled && <span className="ml-2 text-[0.75rem] font-normal text-ink-3">off</span>}</p>
          {r.reason && <p className="mt-0.5 text-[0.8125rem] leading-5 text-ink-2">{r.reason}</p>}
          <p className="mt-1 flex flex-wrap items-center gap-1.5 text-[0.71875rem] text-ink-3">
            <span className="inline-flex items-center gap-1" title={S.tip}><S.icon className="size-3" />{S.label}</span>
            {r.confidence !== "high" && <span>· {r.confidence} confidence</span>}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          <div className="mr-1 text-right">
            <p className={cn("text-[1rem] font-bold tabular-nums", bad && r.enabled ? "text-[var(--warning)]" : "text-ink-2")}>
              {r.count == null ? "–" : bad ? nf.format(r.count) : <span className="inline-flex items-center gap-1 text-[0.8125rem] font-semibold text-[var(--good)]"><Check className="size-3.5" />none</span>}
            </p>
            {t.date_column && r.count_today != null && bad && <p className="text-[0.6875rem] text-ink-3">{nf.format(r.count_today)} today</p>}
          </div>
          {r.enabled && bad && (t.owner
            ? <Button size="sm" variant={open ? "secondary" : "outline"} onClick={onOpen} aria-expanded={open}>{open ? <EyeOff /> : <Eye />}{open ? "Hide" : "Show values"}</Button>
            : <span className="inline-flex items-center gap-1 text-[0.71875rem] text-ink-3" title={`Only the person who connected ${t.connection} can see its values`}><Lock className="size-3.5" />owner only</span>)}
          <Button size="icon-sm" variant="ghost" onClick={onEdit} aria-label="Edit this rule" title="Edit this rule"><Pencil /></Button>
          {confirm
            ? <Button size="sm" variant="danger" onClick={onRemove}><Trash2 />Delete?</Button>
            : <Button size="icon-sm" variant="ghost" onClick={() => setConfirm(true)} aria-label="Delete this rule" title={r.source === "person" ? "Delete this rule" : "Delete this rule (the bot won't suggest it again)"}><Trash2 /></Button>}
        </div>
      </div>
      {open && <ValuesPanel r={r} t={t} base={base} max={max} />}
    </li>
  );
}

function ValuesPanel({ r, t, base, max }: { r: Rule; t: TableT; base: string; max: number }) {
  const [scope, setScope] = useState<"all" | "today">("all");
  const [data, setData] = useState<Values | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setData(null); setErr(null);
    api<Values>(`${base}/rule/${r.id}/values?scope=${scope}`).then((v) => { if (live) setData(v); }).catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [base, r.id, scope]);
  return (
    <div className="sv-values">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-[0.75rem] text-ink-3"><Lock className="size-3.5" />Fetched just now for you only. Not kept, not logged, never sent to AI.</p>
        {t.date_column && <Segmented<"all" | "today"> size="sm" label="Which rows" value={scope} onChange={setScope} options={[{ value: "all", label: "All rows" }, { value: "today", label: "Today's" }]} />}
      </div>
      {err ? <p className="mt-2 text-[0.8125rem] text-[var(--critical)]">{err}</p> : !data ? <Skeleton className="mt-2 h-24 w-full" /> : !data.values.length ? (
        <p className="mt-2 text-[0.8125rem] text-ink-2">Nothing breaks this rule {scope === "today" ? "in today's rows" : "any more"}.</p>
      ) : (
        <>
          <ul className="mt-2 grid gap-1.5 sm:grid-cols-2 xl:grid-cols-3">
            {data.values.map((v) => (
              <li key={v.value + v.count} className="sv-val">
                <code className="min-w-0 flex-1 [overflow-wrap:anywhere]"><Marked value={v.value} kind={r.kind} chars={r.chars} /></code>
                <span className="shrink-0 tabular-nums text-ink-3">×{nf.format(v.count)}</span>
              </li>
            ))}
          </ul>
          {data.values.length >= max && <p className="mt-2 text-[0.75rem] text-ink-3">The {max} most common shown.</p>}
        </>
      )}
    </div>
  );
}

/** The value, with what breaks the rule marked: digits, symbols, letters, spaces, or the spaces at its ends. */
function Marked({ value, kind, chars }: { value: string; kind: Kind; chars?: CharClass[] | null }) {
  if (kind === "trimmed") {
    const lead = value.length - value.trimStart().length, trail = value.length - value.trimEnd().length;
    return <>{lead ? <mark className="sv-mark">{"␣".repeat(lead)}</mark> : null}{value.trim()}{trail ? <mark className="sv-mark">{"␣".repeat(trail)}</mark> : null}</>;
  }
  const classes: Record<CharClass, string> = { digits: "0-9", letters: "A-Za-z", symbols: "@#$%\\^*=+<>?!~|{}_", spaces: " " };  // inside [ ]: ^ escaped, ranges kept
  const re = kind === "no_digits" ? /[0-9]/ : kind === "no_symbols" ? /[@#$%^*=+<>?!~|{}_]/
    : kind === "forbid" && chars?.length ? new RegExp(`[${chars.map((c) => classes[c]).join("")}]`) : null;
  if (!re) return <>{value === "" ? <span className="text-ink-3">(empty)</span> : value}</>;
  return <>{[...value].map((ch, k) => (re.test(ch) ? <mark key={k} className="sv-mark">{ch === " " ? "␣" : ch}</mark> : <span key={k}>{ch}</span>))}</>;
}

// ── add or edit a rule ───────────────────────────────────────────────────────
function RuleModal({ open, rule, column: fixedColumn, t, meta, base, onClose, onDone }: {
  open: boolean; rule?: Rule; column?: string; t: TableT; meta: ListT; base: string; onClose: () => void; onDone: (t: TableT) => void;
}) {
  const cols = Object.entries(t.columns);
  const [column, setColumn] = useState(cols[0]?.[0] ?? "");
  const type = t.columns[column];
  const kinds = (Object.keys(meta.kinds) as Kind[]).filter((k) => type && meta.kinds[k].includes(type));
  const [kind, setKind] = useState<Kind>("no_digits");
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [format, setFormat] = useState(Object.keys(meta.formats)[0] ?? "");
  const [reason, setReason] = useState("");
  const [shapes, setShapes] = useState("");
  const [chars, setChars] = useState<CharClass[]>(["digits"]);
  const [allowed, setAllowed] = useState("");
  const [busy, setBusy] = useState(false);
  const str = (v: number | null | undefined) => (v == null ? "" : String(v));
  useEffect(() => {  // open: start from the rule being edited, or a blank one on the chosen column
    if (!open) return;
    const r = rule;
    setColumn(r?.column ?? fixedColumn ?? cols[0]?.[0] ?? "");
    setKind(r?.kind ?? "no_digits");
    setA(r ? (r.kind === "date_sane" ? str(r.min_year) : str(r.min)) : "");
    setB(r ? (r.kind === "date_sane" ? str(r.future_days) : str(r.max)) : "");
    setFormat(r?.format ?? Object.keys(meta.formats)[0] ?? "");
    setReason(r?.source === "person" ? r.reason : "");
    setShapes((r?.shapes ?? []).join("\n"));
    setChars(r?.chars ?? ["digits"]);
    setAllowed((r?.allowed ?? []).join("\n"));
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (open && kinds.length && !kinds.includes(kind)) setKind(kinds[0]!); }, [column]); // eslint-disable-line react-hooks/exhaustive-deps
  const num = (s: string) => (s.trim() === "" ? null : Number(s));
  const list = (txt: string) => txt.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    const body: Record<string, unknown> = { column, kind, reason: reason.trim() };
    if (kind === "length" || kind === "range") { body.min = num(a); body.max = num(b); }
    if (kind === "format") body.format = format;
    if (kind === "date_sane") { body.min_year = num(a); body.future_days = num(b); }
    if (kind === "pattern") body.shapes = list(shapes);
    if (kind === "forbid") body.chars = chars;
    if (kind === "allowed_values") body.allowed = list(allowed);
    setBusy(true);
    try {
      const r = rule ? await api<TableT>(`${base}/rule/${rule.id}`, { method: "PUT", body: JSON.stringify(body) }) : await post<TableT>(`${base}/rule`, body);
      onDone(r);
      toast.success(rule ? "Rule changed and checked" : "Rule added and checked", { description: rule && rule.source !== "person" ? "It's the team's own now: the bot won't replace it." : undefined });
    } catch (e) { toast.error(rule ? "Couldn't change the rule" : "Couldn't add the rule", { description: (e as Error).message }); } finally { setBusy(false); }
  };
  const meaning = t.meanings[column]?.text;
  return (
    <Modal open={open} onClose={onClose} icon={rule ? Pencil : Plus} title={rule ? `Edit a rule on ${rule.column}` : `Add a rule${fixedColumn ? ` to ${fixedColumn}` : ""}`}
      subtitle={rule ? "Changes are checked straight away. A rule the bot made becomes the team's own." : "Pick what a correct value must look like. It's checked straight away."}
      footer={<><Button variant="ghost" onClick={onClose}><X />Cancel</Button><Button variant="primary" onClick={() => void save()} disabled={busy || !column}>{busy ? <Loader2 className="animate-spin" /> : <Check />}{rule ? "Save the rule" : "Add the rule"}</Button></>}>
      {meaning && <p className="mb-3 flex items-start gap-2 rounded-xl bg-accent-soft/60 px-3 py-2 text-[0.8125rem] text-ink"><Brain className="mt-0.5 size-4 shrink-0 text-accent" />{column} holds {meaning}</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block"><span className="mb-1 block text-xs font-medium text-ink-2">Column</span>
          <Select className="w-full" value={column} onChange={(e) => setColumn(e.target.value)} disabled={Boolean(rule || fixedColumn)}>{cols.map(([c, ty]) => <option key={c} value={c}>{c} ({ty})</option>)}</Select></label>
        <label className="block"><span className="mb-1 block text-xs font-medium text-ink-2">A correct value…</span>
          <Select className="w-full" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>{kinds.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}</Select></label>
        {(kind === "length" || kind === "range") && <>
          <label className="block"><span className="mb-1 block text-xs font-medium text-ink-2">At least</span><Input className="w-full" inputMode="decimal" value={a} onChange={(e) => setA(e.target.value)} placeholder="no minimum" /></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-ink-2">At most</span><Input className="w-full" inputMode="decimal" value={b} onChange={(e) => setB(e.target.value)} placeholder="no maximum" /></label>
        </>}
        {kind === "format" && <label className="block sm:col-span-2"><span className="mb-1 block text-xs font-medium text-ink-2">Format</span>
          <Select className="w-full" value={format} onChange={(e) => setFormat(e.target.value)}>{Object.entries(meta.formats).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></label>}
        {kind === "date_sane" && <>
          <label className="block"><span className="mb-1 block text-xs font-medium text-ink-2">Not before the year</span><Input className="w-full" inputMode="numeric" value={a} onChange={(e) => setA(e.target.value)} placeholder="e.g. 2000" /></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-ink-2">At most this many days ahead</span><Input className="w-full" inputMode="numeric" value={b} onChange={(e) => setB(e.target.value)} placeholder="1 = not in the future" /></label>
        </>}
        {kind === "pattern" && <label className="block sm:col-span-2"><span className="mb-1 block text-xs font-medium text-ink-2">Shapes (one per line, or separated by commas)</span>
          <textarea className="sv-area" rows={3} value={shapes} onChange={(e) => setShapes(e.target.value)} placeholder={"{L2}{A3}\nIMO{D7}"} />
          <span className="mt-1 block text-[0.71875rem] leading-4 text-ink-3"><code>{"{L}"}</code> a letter, <code>{"{D}"}</code> a digit, <code>{"{A}"}</code> either, with a count: <code>{"{L2}{A3}"}</code> is SGSIN, <code>{"{D4}-{D2}-{D2}"}</code> is 2026-10-22. Letters, digits, spaces and - / . : stand for themselves.</span></label>}
        {kind === "forbid" && <fieldset className="sm:col-span-2"><legend className="mb-1 block text-xs font-medium text-ink-2">It never contains</legend>
          <div className="flex flex-wrap gap-3">{(["digits", "letters", "symbols", "spaces"] as CharClass[]).map((c) => (
            <label key={c} className="inline-flex items-center gap-1.5 text-[0.875rem] text-ink"><input type="checkbox" className="wh-check" checked={chars.includes(c)}
              onChange={(e) => setChars((x) => (e.target.checked ? [...x, c] : x.filter((y) => y !== c)))} />{c}</label>))}</div></fieldset>}
        {kind === "allowed_values" && <label className="block sm:col-span-2"><span className="mb-1 block text-xs font-medium text-ink-2">Allowed values (one per line, or separated by commas; capitals don't matter)</span>
          <textarea className="sv-area" rows={3} value={allowed} onChange={(e) => setAllowed(e.target.value)} placeholder={"IMPORT\nEXPORT"} /></label>}
        <label className="block sm:col-span-2"><span className="mb-1 block text-xs font-medium text-ink-2">Why (optional, the team sees it)</span>
          <Input className="w-full" value={reason} maxLength={240} onChange={(e) => setReason(e.target.value)} placeholder="e.g. terminal codes are always in capitals" /></label>
      </div>
      {t.sensitive.length > 0 && <p className="mt-3 text-[0.75rem] text-ink-3">Sensitive columns ({t.sensitive.join(", ")}) are never checked, so they're not listed.</p>}
    </Modal>
  );
}
