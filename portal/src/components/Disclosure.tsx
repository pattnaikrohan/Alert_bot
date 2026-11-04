// "More details": the summary stays on top, the rest folds away until asked for. Its content only renders (and fetches)
// once opened, so a page with everything folded is quick.
import { ChevronDown } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Disclosure({ title, hint, children, defaultOpen = false, className }: {
  title: string; hint?: string; children: ReactNode; defaultOpen?: boolean; className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <section className={cn("disclosure", open && "is-open", className)}>
      <button type="button" className="disclosure-head" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)}>
        <span className="min-w-0 flex-1 text-left">
          <span className="block text-[0.875rem] font-semibold text-ink">{open ? `Hide ${title.charAt(0).toLowerCase()}${title.slice(1)}` : title}</span>
          {hint && <span className="block truncate text-[0.71875rem] text-ink-3">{hint}</span>}
        </span>
        <ChevronDown className="disclosure-caret" aria-hidden />
      </button>
      {open && <div id={id} className="disclosure-body">{children}</div>}
    </section>
  );
}
