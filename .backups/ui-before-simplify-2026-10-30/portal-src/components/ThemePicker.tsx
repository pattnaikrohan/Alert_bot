// Appearance: light or dark, and a shade per theme. One picker for the app header and the sign-in page.
import { Check, Moon, Palette, Sun } from "lucide-react";
import { Menu, MenuContent, MenuLabel, MenuSeparator, MenuTrigger, Tip } from "@/components/ui/menu";
import { applyShade, applyTheme, LIGHT_SHADES, SHADES, useAppearance } from "@/lib/theme";
import { cn } from "@/lib/utils";

export function ThemePanel() {
  const { theme, shade } = useAppearance();
  return (
    <>
      <MenuLabel>Theme</MenuLabel>
      {/* light / dark: one segmented control */}
      <div className="mx-2 mb-2 grid grid-cols-2 gap-1 rounded-xl bg-surface-2 p-1 shadow-[inset_0_0_0_1px_var(--line)]" role="radiogroup" aria-label="Theme">
        {(["light", "dark"] as const).map((t) => (
          <button key={t} type="button" role="radio" aria-checked={theme === t} onClick={() => applyTheme(t)}
            className={cn("flex h-9 items-center justify-center gap-2 rounded-[0.625rem] text-[0.8125rem] font-medium transition-all",
              theme === t ? "bg-surface text-ink shadow-[0_1px_2px_rgb(0_0_0/0.12),inset_0_0_0_1px_var(--line-strong)]" : "text-ink-3 hover:text-ink")}>
            {t === "light" ? <Sun className="size-4" aria-hidden /> : <Moon className="size-4" aria-hidden />}{t === "light" ? "Light" : "Dark"}
          </button>
        ))}
      </div>
      <MenuSeparator />
      <MenuLabel>{theme === "light" ? "Light shades" : "Dark shades"}</MenuLabel>
      {/* one row per shade: a tiny preview of the page and its accent, the name, what it looks like */}
      <div className="flex flex-col gap-0.5 px-1.5 pb-1.5" role="radiogroup" aria-label="Shade">
        {(theme === "light" ? LIGHT_SHADES : SHADES).map((s) => {
          const on = shade === s.id;
          return (
            <button key={s.id} type="button" role="radio" aria-checked={on} onClick={() => applyShade(s.id)}
              className={cn("flex w-full items-center gap-3 rounded-xl px-2 py-1.5 text-left transition-colors",
                on ? "bg-accent-soft" : "hover:bg-surface-3")}>
              <span className="relative h-8 w-11 shrink-0 overflow-hidden rounded-lg shadow-[inset_0_0_0_1px_rgb(0_0_0/0.14)]" style={{ background: s.swatch[0] }} aria-hidden>
                <span className="absolute inset-y-0 left-0 w-2.5" style={{ background: s.swatch[1], opacity: 0.9 }} />
                <span className="absolute left-4 right-1.5 top-2 h-1 rounded-full" style={{ background: s.swatch[1] }} />
                <span className="absolute left-4 right-3 top-4 h-1 rounded-full opacity-35" style={{ background: s.swatch[1] }} />
              </span>
              <span className="min-w-0 flex-1">
                <span className={cn("block text-[0.8125rem] leading-tight", on ? "font-semibold text-ink" : "font-medium text-ink")}>{s.label}</span>
                <span className="block truncate text-[0.6875rem] leading-snug text-ink-3">{s.hint}</span>
              </span>
              <span className={cn("flex size-5 shrink-0 items-center justify-center rounded-full transition-all",
                on ? "bg-accent text-[var(--accent-ink)]" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]")} aria-hidden>
                {on && <Check className="size-3" strokeWidth={3} />}
              </span>
            </button>
          );
        })}
      </div>
      <p className="border-t border-line px-3 pb-1 pt-2 text-[0.6875rem] text-ink-3">Saved on this device.</p>
    </>
  );
}

/** A button that opens the appearance picker. `labelled` shows the word "Theme" next to the icon (sign-in page). */
export function ThemeButton({ labelled = false, className }: { labelled?: boolean; className?: string }) {
  const { theme } = useAppearance();
  const Icon = labelled ? Palette : theme === "dark" ? Moon : Sun;
  return (
    <Menu>
      <Tip content="Appearance: light, dark and colour shades">
        <MenuTrigger aria-label={`Appearance: ${theme} theme`}
          className={cn("flex items-center justify-center gap-2 rounded-xl text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink",
            labelled ? "h-9 border border-line bg-surface/60 px-3 text-sm backdrop-blur" : "size-9", className)}>
          <Icon className="size-4" aria-hidden />{labelled && <span>Theme</span>}
        </MenuTrigger>
      </Tip>
      <MenuContent className="w-80" align="end"><ThemePanel /></MenuContent>
    </Menu>
  );
}
