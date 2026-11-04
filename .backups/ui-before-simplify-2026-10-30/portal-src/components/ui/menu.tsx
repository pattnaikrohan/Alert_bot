import * as M from "@radix-ui/react-dropdown-menu";
import * as T from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export const Menu = M.Root;
export const MenuTrigger = M.Trigger;
export function MenuContent({ children, align = "end", side = "bottom", className, onCloseAutoFocus }: {
  children: ReactNode; align?: "start" | "end" | "center"; side?: "top" | "bottom" | "left" | "right"; className?: string;
  onCloseAutoFocus?: (e: Event) => void;
}) {
  return (
    <M.Portal>
      <M.Content align={align} side={side} onCloseAutoFocus={onCloseAutoFocus} sideOffset={side === "top" ? 14 : 6} collisionPadding={12} className={cn("anim-pop glass z-[60] min-w-56 rounded-xl border border-line-strong p-1 shadow-float", className)}>
        {children}
      </M.Content>
    </M.Portal>
  );
}
const item = "flex cursor-pointer select-none items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-ink outline-none transition-colors data-[disabled]:opacity-40 data-[highlighted]:bg-surface-3";
export function MenuItem({ children, onSelect, className, disabled }: { children: ReactNode; onSelect?: () => void; className?: string; disabled?: boolean }) {
  return (
    <M.Item disabled={disabled} onSelect={onSelect} className={cn(item, className)}>
      {children}
    </M.Item>
  );
}
export const MenuLabel = ({ children }: { children: ReactNode }) => <M.Label className="eyebrow px-2 pb-1 pt-1.5 text-ink-3">{children}</M.Label>;
export const MenuSeparator = () => <M.Separator className="my-1 h-px bg-line" />;

export const TooltipProvider = T.Provider;
export function Tip({ content, children, side = "top", className }: { content: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right"; className?: string }) {
  return (
    <T.Root delayDuration={250}>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content side={side} sideOffset={6} collisionPadding={8} className={cn("anim-fade z-[70] max-w-xs rounded-lg border border-line-strong bg-tooltip px-2.5 py-1.5 text-xs text-ink shadow-float", className)}>
          {content}
        </T.Content>
      </T.Portal>
    </T.Root>
  );
}
