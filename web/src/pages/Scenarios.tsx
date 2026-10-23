import { useMemo, useState } from "react";
import { api, dateRu, pct, rub } from "../api";
import Chart from "../Chart";
import { forecastOption } from "../charts";
import { useApp } from "../ctx";
import { Icon } from "../icons";
import type { Result, Run } from "../types";
import { Badge, Card, Empty, Field, Kpi, Loading, Notice, PageHeader, type Tone } from "../ui";

type Params = {
  delay_days: number; delay_counterparty: string; revenue_drop_pct: number; fixed_cost_growth_pct: number;
  extra_expense: string; extra_expense_date: string; extra_income: string; extra_income_date: string;
};
type Saved = { id: string; name: string; params: Params };
type Out = { base: Run; scenario: Run; delta: { shortfall_probability: number; max_deficit_p90: string } };

const ZERO: Params = {
  delay_days: 0, delay_counterparty: "", revenue_drop_pct: 0, fixed_cost_growth_pct: 0,
  extra_expense: "", extra_expense_date: "", extra_income: "", extra_income_date: "",
};
const PRESETS: { name: string; p: Partial<Params> }[] = [
  { name: "Клиент платит на 3 недели позже", p: { delay_days: 21 } },
  { name: "Выручка −20%", p: { revenue_drop_pct: 20 } },
  { name: "Постоянные расходы +15%", p: { fixed_cost_growth_pct: 15 } },
  { name: "Внеплановый расход", p: { extra_expense: "300000" } },
];
const KEY = "ci1.scenarios";

function loadSaved(): Saved[] {
  try { return JSON.parse(localStorage.getItem(KEY) ?? "[]") as Saved[]; } catch { return []; }
}
function storeSaved(v: Saved[]) {
  try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* без хранилища страница работает */ }
}

const toKop = (s: string): number | null => {
  if (!s.trim()) return null;
  const v = Math.round(parseFloat(s.replace(/\s/g, "").replace(",", ".")) * 100);
  return Number.isFinite(v) && v > 0 ? v : NaN;
};

function minPoint(r: Result) {
  let best = r.days[0];
  for (const d of r.days) if (BigInt(d.p50) < BigInt(best.p50)) best = d;
  return best;
}

function conclusion(b: Result, s: Result): { tone: Tone; text: string } {
  const pb = b.risk.shortfall_probability, ps = s.risk.shortfall_probability;
  const gap = s.risk.possible_break_date ?? s.risk.first_break_date;
  if (ps < 0.05) return { tone: "ok", text: "Даже в этом сценарии кассовый разрыв маловероятен: остатка хватает на все обязательные платежи." };
  const delta = Math.round((ps - pb) * 100);
  const when = gap ? ` Вероятная дата — ${dateRu(gap)}, в плохом случае не хватит ${rub(s.risk.max_deficit_p90)}.` : "";
  return {
    tone: ps >= 0.5 ? "risk" : "warn",
    text: `Вероятность кассового разрыва ${delta >= 0 ? "вырастает" : "снижается"} с ${pct(pb)} до ${pct(ps)}.${when} Заранее продумайте, какие платежи можно перенести или чем покрыть дефицит.`,
  };
}

export default function Scenarios() {
  const { base, run, runState, horizon, openImport } = useApp();
  const [p, setP] = useState<Params>(ZERO);
  const [name, setName] = useState("");
  const [saved, setSaved] = useState<Saved[]>(loadSaved);
  const [out, setOut] = useState<Out | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const rules = run?.result.rules ?? [];
  const cp = rules.find((r) => r.counterparty === p.delay_counterparty);
  const set = <K extends keyof Params>(k: K, v: Params[K]) => setP((x) => ({ ...x, [k]: v }));

  async function calc(params: Params) {
    setError("");
    const eK = toKop(params.extra_expense), iK = toKop(params.extra_income);
    if (Number.isNaN(eK) || Number.isNaN(iK)) return setError("Суммы должны быть положительными числами");
    if (eK && !params.extra_expense_date) return setError("Укажите дату внепланового расхода");
    if (iK && !params.extra_income_date) return setError("Укажите дату дополнительного поступления");
    setBusy(true);
    try {
      setOut(await api.post<Out>(`${base}/scenarios`, {
        horizon_days: horizon,
        scenario: {
          delay_days: params.delay_days, delay_counterparty: params.delay_counterparty || null,
          revenue_drop_pct: params.revenue_drop_pct, fixed_cost_growth_pct: params.fixed_cost_growth_pct,
          extra_expense_kop: eK, extra_expense_date: eK ? params.extra_expense_date : null,
          extra_income_kop: iK, extra_income_date: iK ? params.extra_income_date : null,
        },
      }));
    } catch (e) {
      const m = (e as Error).message;
      setError(m);
    } finally { setBusy(false); }
  }

  function preset(x: Partial<Params>) {
    const v = { ...ZERO, ...x };
    if (x.extra_expense && run) v.extra_expense_date = run.result.days[Math.min(9, run.result.days.length - 1)].date;
    setP(v); setName(""); calc(v);
  }
  function save() {
    const n = name.trim() || `Сценарий ${saved.length + 1}`;
    const next = [...saved, { id: crypto.randomUUID(), name: n, params: p }];
    setSaved(next); storeSaved(next); setName("");
  }
  function remove(id: string) {
    const next = saved.filter((s) => s.id !== id);
    setSaved(next); storeSaved(next);
  }

  const option = useMemo(() => (out ? forecastOption(out.base.result, { name: "Сценарий", r: out.scenario.result }) : {}), [out]);
  const num = (k: "delay_days" | "revenue_drop_pct" | "fixed_cost_growth_pct") => (e: React.ChangeEvent<HTMLInputElement>) => set(k, Number(e.target.value));

  if (runState === "loading") return <Loading />;
  if (runState !== "ok" || !run) {
    return (
      <>
        <PageHeader title="Сценарии" subtitle="Что, если дела пойдут хуже?" />
        <Card><Empty title="Нет базового прогноза" text="Загрузите банковскую выписку — сценарии строятся поверх базового прогноза."
          action={<button className="btn" onClick={openImport}><Icon name="upload" size={16} />Загрузить выписку</button>} /></Card>
      </>
    );
  }

  const b = out?.base.result, s = out?.scenario.result;
  const bMin = b && minPoint(b), sMin = s && minPoint(s);
  const concl = b && s ? conclusion(b, s) : null;
  const gapTone = (x: Result): Tone => (x.risk.level === "high" ? "risk" : x.risk.level === "none" ? "ok" : "warn");
  const LEVEL = { none: "Низкий риск", low: "Небольшой риск", medium: "Заметный риск", high: "Высокий риск" } as const;

  return (
    <>
      <PageHeader title="Сценарии" subtitle="Проверьте, как неблагоприятное событие изменит остаток и дату кассового разрыва" />
      <div className="stack">
        <Card title="Готовые сценарии">
          <div className="chips">
            {PRESETS.map((x) => <button key={x.name} className="chip" disabled={busy} onClick={() => preset(x.p)}>{x.name}</button>)}
          </div>
          {saved.length > 0 && (
            <>
              <h3 className="small muted" style={{ margin: "16px 0 8px" }}>Сохранённые</h3>
              <div className="chips">
                {saved.map((x) => (
                  <span key={x.id} className="row" style={{ gap: 2 }}>
                    <button className="chip" disabled={busy} onClick={() => { setP(x.params); calc(x.params); }}>{x.name}</button>
                    <button className="btn ghost sm" aria-label={`Удалить сценарий ${x.name}`} onClick={() => remove(x.id)}><Icon name="trash" size={14} /></button>
                  </span>
                ))}
              </div>
            </>
          )}
        </Card>

        <Card title="Параметры сценария">
          <form onSubmit={(e) => { e.preventDefault(); calc(p); }} className="stack">
            <div className="form-grid">
              <Field label="Платёж контрагента (пусто — самый крупный клиент)" hint="Для исходящих платежей сдвиг означает перенос оплаты на более поздний срок">
                <select value={p.delay_counterparty} onChange={(e) => set("delay_counterparty", e.target.value)}>
                  <option value="">Самый крупный клиент</option>
                  {rules.map((r) => <option key={r.key} value={r.counterparty}>{r.counterparty || r.key} · {rub(r.amount, { sign: true })}</option>)}
                </select>
              </Field>
              <Field label="Сдвинуть платёж на, дней" hint={cp ? (cp.direction === "in" ? "Клиент заплатит позже" : "Вы заплатите позже") : "Клиент заплатит позже"}>
                <input type="number" min={0} max={90} value={p.delay_days} onChange={num("delay_days")} />
              </Field>
              <Field label="Падение выручки, %"><input type="number" min={0} max={100} value={p.revenue_drop_pct} onChange={num("revenue_drop_pct")} /></Field>
              <Field label="Рост постоянных расходов, %"><input type="number" min={0} max={300} value={p.fixed_cost_growth_pct} onChange={num("fixed_cost_growth_pct")} /></Field>
            </div>
            <div className="form-grid">
              <Field label="Внеплановый расход, ₽"><input inputMode="decimal" value={p.extra_expense} onChange={(e) => set("extra_expense", e.target.value)} placeholder="Ремонт, штраф…" /></Field>
              <Field label="Дата расхода"><input type="date" value={p.extra_expense_date} onChange={(e) => set("extra_expense_date", e.target.value)} /></Field>
              <Field label="Дополнительное поступление, ₽"><input inputMode="decimal" value={p.extra_income} onChange={(e) => set("extra_income", e.target.value)} placeholder="Новый клиент, возврат…" /></Field>
              <Field label="Дата поступления"><input type="date" value={p.extra_income_date} onChange={(e) => set("extra_income_date", e.target.value)} /></Field>
            </div>
            <div className="toolbar">
              <button className="btn" disabled={busy}>{busy ? "Считаю…" : "Рассчитать сценарий"}</button>
              <button type="button" className="btn ghost" onClick={() => { setP(ZERO); setOut(null); setError(""); }}>Сбросить</button>
              <div className="grow" />
              <input placeholder="Название сценария" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} aria-label="Название сценария" />
              <button type="button" className="btn secondary" onClick={save}><Icon name="plus" size={16} />Сохранить</button>
            </div>
            {error && <Notice tone="risk">{error}</Notice>}
          </form>
        </Card>

        {out && b && s && bMin && sMin && concl && (
          <>
            <Notice tone={concl.tone}>{concl.text}</Notice>
            <div className="grid kpis">
              <Kpi label="Минимальный остаток (база)" value={rub(bMin.p50)} hint={`на ${dateRu(bMin.date)}`} tone={BigInt(bMin.p50) < 0n ? "risk" : "neutral"} />
              <Kpi label="Минимальный остаток (сценарий)" value={rub(sMin.p50)} hint={`на ${dateRu(sMin.date)}`} tone={BigInt(sMin.p50) < 0n ? "risk" : BigInt(sMin.p50) < BigInt(bMin.p50) ? "warn" : "neutral"} />
              <Kpi label="Вероятность разрыва" value={`${pct(b.risk.shortfall_probability)} → ${pct(s.risk.shortfall_probability)}`}
                hint={<Badge tone={gapTone(s)}>{LEVEL[s.risk.level]}</Badge>} tone={gapTone(s)} />
              <Kpi label="Дефицит в плохом случае (p90)" value={rub(s.risk.max_deficit_p90)}
                hint={`база ${rub(b.risk.max_deficit_p90)} · разница ${rub(out.delta.max_deficit_p90, { sign: true })}`} tone={BigInt(s.risk.max_deficit_p90) > 0n ? "risk" : "ok"} />
            </div>
            <Card title="Базовый прогноз и сценарий"
              actions={<span className="small muted">Дата разрыва: база {b.risk.possible_break_date ? dateRu(b.risk.possible_break_date) : "нет"} · сценарий {s.risk.possible_break_date ? dateRu(s.risk.possible_break_date) : "нет"}</span>}>
              <Chart option={option} height={360} />
              <p className="small muted">Базовый расчёт и сценарий используют один набор случайных чисел, поэтому разница объясняется только заданными изменениями.</p>
            </Card>
          </>
        )}
      </div>
    </>
  );
}
