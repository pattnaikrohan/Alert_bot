import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-lg text-sm font-medium transition-[background-color,color,box-shadow,border-color,transform,filter] duration-150 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-45 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink shadow-[inset_0_1px_0_rgb(255_255_255/0.22),0_1px_2px_rgb(0_0_0/0.25),0_0_0_1px_var(--accent)] hover:brightness-110",
        secondary: "bg-surface-2 text-ink shadow-[inset_0_0_0_1px_var(--line-strong),0_1px_2px_rgb(0_0_0/0.12)] hover:bg-surface-3 light:bg-surface light:hover:bg-surface-2",
        ghost: "text-ink-2 hover:bg-surface-3 hover:text-ink",
        outline: "text-ink shadow-[inset_0_0_0_1px_var(--line-strong)] hover:bg-surface-3",
        danger: "bg-critical text-white shadow-[inset_0_1px_0_rgb(255_255_255/0.15)] hover:brightness-110",
      },
      size: { sm: "h-8 px-2.5 text-xs", md: "h-9 px-3.5", lg: "h-10 px-5", icon: "size-9", "icon-sm": "size-8" },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, asChild, ...props }, ref) => {
  const Comp = asChild ? Slot : "button";
  return <Comp ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
});
Button.displayName = "Button";
