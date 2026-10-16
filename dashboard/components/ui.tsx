import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`hud-panel flex min-h-0 flex-col ${className}`}>
      <header className="flex items-center justify-between gap-2 border-b border-slate-800/80 px-3 py-1.5">
        <h2 className="text-[10px] font-semibold uppercase tracking-[0.22em] text-slate-400">{title}</h2>
        {right}
      </header>
      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

const TONES = {
  cyan: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300",
  emerald: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  amber: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  red: "border-rose-500/50 bg-rose-500/10 text-rose-300",
  violet: "border-violet-500/40 bg-violet-500/10 text-violet-300",
  slate: "border-slate-600/60 bg-slate-800/40 text-slate-300",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({ tone = "slate", children, pulse = false }: { tone?: Tone; children: ReactNode; pulse?: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded border px-2 py-0.5 text-[10px] font-semibold tracking-wider ${TONES[tone]}`}
    >
      {pulse && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-[11px]">
      <span className="text-slate-500">{label}</span>
      <span className={`text-right tabular-nums ${tone ?? "text-slate-200"}`}>{value}</span>
    </div>
  );
}

export function HudButton({
  children,
  onClick,
  active = false,
  disabled = false,
  tone = "cyan",
  className = "",
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  active?: boolean;
  disabled?: boolean;
  tone?: Tone;
  className?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={`rounded border px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-colors disabled:cursor-not-allowed disabled:opacity-35 ${
        active ? TONES[tone] : "border-slate-700 bg-slate-900/60 text-slate-300 hover:border-slate-500 hover:text-white"
      } ${className}`}
    >
      {children}
    </button>
  );
}
