import { useMemo, useState } from "react";
import { api, pct, rub, dateRu } from "./api";
import Chart from "./Chart";
import { forecastOption } from "./Dashboard";
import type { Run } from "./types";

type Out = { base: Run; scenario: Run; delta: { shortfall_probability: number; max_deficit_p90: string } };
const PRESETS = [
  { name: "Клиент платит на 3 недели позже", s: { delay_days: 21 } },
  { name: "Выручка −20%", s: { revenue_drop_pct: 20 } },
  { name: "Постоянные расходы +15%", s: { fixed_cost_growth_pct: 15 } },
];

export function Scenarios({ base }: { base: string; rev: number }) {
  const [s, setS] = useState({ delay_days: 0, delay_counterparty: "", revenue_drop_pct: 0, fixed_cost_growth_pct: 0, extra: "", extra_date: "" });
  const [out, setOut] = useState<Out | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(over?: Partial<typeof s>) {
    const v = { ...s, ...over };
    setS(v); setBusy(true); setError("");
    try {
      const extra = v.extra ? Math.round(parseFloat(v.extra.replace(",", ".")) * 100) : null;
      setOut(await api.post<Out>(`${base}/scenarios`, {
        scenario: {
          delay_days: v.delay_days, delay_counterparty: v.delay_counterparty || null, revenue_drop_pct: v.revenue_drop_pct, fixed_cost_growth_pct: v.fixed_cost_growth_pct,
          extra_expense_kop: extra, extra_expense_date: extra ? v.extra_date || null : null,
        },
      }));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const option = useMemo(() => (out ? forecastOption(out.base.result, { name: "Сценарий", r: out.scenario.result }) : {}), [out]);
  const num = (k: "delay_days" | "revenue_drop_pct" | "fixed_cost_growth_pct") => (e: React.ChangeEvent<HTMLInputElement>) => setS({ ...s, [k]: Number(e.target.value) });

  return (
    <>
      <section className="card">
        <h3>Негативные сценарии «что если»</h3>
        <div className="chips">{PRESETS.map((p) => (
          <button key={p.name} className="ghost" disabled={busy} onClick={() => run({ delay_days: 0, revenue_drop_pct: 0, fixed_cost_growth_pct: 0, extra: "", ...p.s })}>{p.name}</button>
        ))}</div>
        <form className="form" onSubmit={(e) => { e.preventDefault(); run(); }}>
          <label>Крупный клиент платит позже, дней<input type="number" min={0} max={90} value={s.delay_days} onChange={num("delay_days")} /></label>
          <label>Клиент (пусто — самый крупный)<input value={s.delay_counterparty} onChange={(e) => setS({ ...s, delay_counterparty: e.target.value })} /></label>
          <label>Падение выручки, %<input type="number" min={0} max={100} value={s.revenue_drop_pct} onChange={num("revenue_drop_pct")} /></label>
          <label>Рост постоянных расходов, %<input type="number" min={0} max={300} value={s.fixed_cost_growth_pct} onChange={num("fixed_cost_growth_pct")} /></label>
          <label>Внеплановый расход, ₽<input inputMode="decimal" value={s.extra} onChange={(e) => setS({ ...s, extra: e.target.value })} /></label>
          <label>Дата расхода<input type="date" value={s.extra_date} onChange={(e) => setS({ ...s, extra_date: e.target.value })} /></label>
          <button disabled={busy}>{busy ? "Считаю…" : "Рассчитать сценарий"}</button>
        </form>
        {error && <div className="alert high" role="alert">{error}</div>}
      </section>
      {out && (
        <section className="card">
          <h3>База и сценарий</h3>
          <div className="grid3">
            <div className="stat"><b>{pct(out.base.result.risk.shortfall_probability)} → {pct(out.scenario.result.risk.shortfall_probability)}</b><span>вероятность разрыва</span></div>
            <div className="stat"><b>{out.scenario.result.risk.possible_break_date ? dateRu(out.scenario.result.risk.possible_break_date) : "—"}</b><span>вероятная дата разрыва в сценарии{out.base.result.risk.possible_break_date ? ` (в базе ${dateRu(out.base.result.risk.possible_break_date)})` : ""}</span></div>
            <div className="stat"><b>{rub(out.scenario.result.risk.max_deficit_p90)}</b><span>дефицит в плохом случае (база: {rub(out.base.result.risk.max_deficit_p90)})</span></div>
          </div>
          <Chart option={option} />
          <p className="small muted">Базовый и сценарный расчёты используют один набор случайных чисел, поэтому разница объясняется только сценарием.</p>
        </section>
      )}
    </>
  );
}
