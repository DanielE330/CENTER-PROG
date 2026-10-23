import type { ReactNode } from "react";
import { Icon } from "./icons";

export type Tone = "ok" | "warn" | "risk" | "neutral";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Card({ title, actions, children, className = "", pad = true }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          <div className="grow" />
          {actions}
        </div>
      )}
      <div className={pad ? "card-body" : ""}>{children}</div>
    </section>
  );
}

export function Kpi({ label, value, hint, tone = "neutral" }: { label: string; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  return (
    <div className={`kpi tone-${tone}`}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value money">{value}</div>
      {hint && <div className="kpi-hint">{hint}</div>}
    </div>
  );
}

const TONE_ICON = { ok: "check", warn: "alert", risk: "alert", neutral: undefined } as const;

/** Цвет всегда дублируется текстом; для warn/risk/ok добавляется иконка. */
export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  const ic = TONE_ICON[tone];
  return (
    <span className={`badge tone-${tone}`}>
      {ic && <Icon name={ic} size={12} />}
      {children}
    </span>
  );
}

export function Notice({ tone = "warn", children, action }: { tone?: Tone; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`notice tone-${tone}`} role={tone === "risk" ? "alert" : "status"}>
      <Icon name={tone === "ok" ? "check" : "alert"} size={18} />
      <div className="grow">{children}</div>
      {action}
    </div>
  );
}

export function Empty({ title, text, action }: { title: string; text?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h2>{title}</h2>
      {text && <p className="muted">{text}</p>}
      {action}
    </div>
  );
}

export const Loading = ({ text = "Загрузка…" }: { text?: string }) => <div className="muted loading">{text}</div>;

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
