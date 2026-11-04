// Which database a page is about: a dropdown of the person's connected databases, with "All databases" when the page
// can add them up. SQL Server and Snowflake are told apart by their icon.
import { Check, ChevronDown, Database, Layers, Snowflake } from "lucide-react";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger } from "@/components/ui/menu";
import { cn } from "@/lib/utils";

export interface DbOption { id: number; name: string; kind: string; note?: string }

const iconOf = (kind: string) => (kind === "snowflake" ? Snowflake : Database);

export function DbPicker({ options, value, onChange, all = "All databases", label = "Database", className }: {
  options: DbOption[]; value: number | null; onChange: (id: number | null) => void; all?: string | false; label?: string; className?: string;
}) {
  const cur = options.find((o) => o.id === value);
  const Icon = cur ? iconOf(cur.kind) : Layers;
  return (
    <Menu>
      <MenuTrigger asChild>
        <button className={cn("db-picker", className)} aria-label={`${label}: ${cur?.name ?? (all || "")}`}>
          <Icon className="size-4 shrink-0 text-accent" aria-hidden />
          <span className="min-w-0 text-left leading-tight">
            <span className="block text-[0.6875rem] font-medium text-ink-3">{label}</span>
            <span className="block truncate text-[0.875rem] font-semibold text-ink">{cur?.name ?? (all || "Choose one")}</span>
          </span>
          <ChevronDown className="size-4 shrink-0 text-ink-3" aria-hidden />
        </button>
      </MenuTrigger>
      <MenuContent align="start" className="min-w-[16rem]">
        <MenuLabel>{label}</MenuLabel>
        {all && (
          <MenuItem onSelect={() => onChange(null)}>
            <Layers className="size-4 text-ink-3" /> <span className="flex-1">{all}</span>{value === null && <Check className="size-4 text-accent" />}
          </MenuItem>
        )}
        {options.map((o) => {
          const I = iconOf(o.kind);
          return (
            <MenuItem key={o.id} onSelect={() => onChange(o.id)}>
              <I className="size-4 text-ink-3" />
              <span className="min-w-0 flex-1">
                <span className="block truncate">{o.name}</span>
                {o.note && <span className="block truncate text-[0.6875rem] text-ink-3">{o.note}</span>}
              </span>
              {value === o.id && <Check className="size-4 text-accent" />}
            </MenuItem>
          );
        })}
      </MenuContent>
    </Menu>
  );
}
