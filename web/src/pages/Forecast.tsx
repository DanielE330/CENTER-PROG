import { useMemo, useState } from "react";
import { api, chartRub, dateRu, pct, rub } from "../api";
import Chart from "../Chart";
import { COLORS, axisBase, forecastOption, kRub } from "../charts";
import { useApp } from "../ctx";
import { Badge, Card, Empty, Loading, Notice, PageHeader } from "../ui";
import "./echartsExtra";
import "./Forecast.css";

const PERIODS = [7, 14, 30, 60, 90];

type Metric = { horizon: number; mae_model: string; mae_last_value: string; mae_drift: string; model_beats_baselines_share: number; n: number; [k: string]: unknown };
type BT = { origins: number; alarm_precision: number | null; alarm_recall: number | null; actual_gap_windows: number; metrics: Metric[]; note?: string; [k: string]: unknown };

function weekStart(iso: string) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}

function Backtest() {
  const { base } = useApp();
  const [bt, setBt] = useState<BT | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function run() {
    setBusy(true); setError("");
    try { setBt(await api.get<BT>(`${base}/backtest`)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  // покрытие диапазона p10–p90 может прийти в разных полях — читаем осторожно
  const cov = (m: Metric): number | null => {
    for (const k of ["coverage_p10_p90", "band_coverage", "coverage"]) if (typeof m[k] === "number") return m[k] as number;
    return null;
  };
  const hasCov = bt?.metrics.some((m) => cov(m) !== null);
  return (
    <Card title="Качество модели" actions={<button className="btn secondary sm" onClick={run} disabled={busy}>{busy ? "Считаю…" : "Запустить backtest"}</button>}>
      <p className="muted small">Rolling-origin: на каждой контрольной дате модель видит только прошлое, строит прогноз на 7, 14 и 30 дней и сравнивается с фактическим остатком и двумя наивными подходами.</p>
      {error && <Notice tone="risk">{error}</Notice>}
      {bt && (
        <>
          <div className="table-wrap"><table>
            <thead><tr><th>Горизонт</th><th className="num">Ошибка модели</th><th className="num">«Остаток не меняется»</th><th className="num">«Тренд»</th><th className="num">Модель лучше обоих</th>{hasCov && <th className="num">Попадание в 10–90 %</th>}<th className="num">Точек</th></tr></thead>
            <tbody>{bt.metrics.map((m) => (
              <tr key={m.horizon}>
                <td>{m.horizon} дн.</td><td className="num money">{rub(m.mae_model)}</td><td className="num money">{rub(m.mae_last_value)}</td><td className="num money">{rub(m.mae_drift)}</td>
                <td className="num">{pct(m.model_beats_baselines_share)}</td>{hasCov && <td className="num">{cov(m) === null ? "—" : pct(cov(m)!)}</td>}<td className="num">{m.n}</td>
              </tr>
            ))}</tbody>
          </table></div>
          <div className="gap-grid" style={{ marginTop: 14 }}>
            <div className="gap-cell"><b>{bt.origins}</b><span>контрольных дат</span></div>
            <div className="gap-cell"><b>{bt.alarm_precision == null ? "—" : pct(bt.alarm_precision)}</b><span>точность тревог (P ≥ 50 %)</span></div>
            <div className="gap-cell"><b>{bt.alarm_recall == null ? "—" : pct(bt.alarm_recall)}</b><span>полнота ({bt.actual_gap_windows} реальных окон разрыва)</span></div>
          </div>
          <p className="small muted" style={{ marginTop: 10 }}>Ошибка — средняя абсолютная (MAE) по остатку. При малом числе реальных разрывов точность и полнота тревог малопоказательны.</p>
        </>
      )}
    </Card>
  );
}

export default function Forecast() {
  const { run, runState, error, recalc, recalcBusy, openImport } = useApp();
  const [picked, setPicked] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const r = run?.result;
  const option = useMemo(() => (r ? forecastOption(r) : {}), [r]);

  const flowOption = useMemo(() => {
    if (!r) return {};
    const w = new Map<string, { in: bigint; out: bigint }>();
    for (const p of r.planned) {
      const k = weekStart(p.date);
      const v = w.get(k) ?? { in: 0n, out: 0n };
      const a = BigInt(p.amount);
      if (a > 0n) v.in += a; else v.out += a;
      w.set(k, v);
    }
    const keys = [...w.keys()].sort();
    return {
      animation: false,
      grid: { left: 64, right: 16, top: 24, bottom: 44 },
      tooltip: { trigger: "axis", backgroundColor: "#fff", borderColor: COLORS.line, textStyle: { color: COLORS.text }, valueFormatter: (v: number) => `${v.toLocaleString("ru-RU")} ₽` },
      legend: { bottom: 0, textStyle: { color: COLORS.muted } },
      xAxis: { type: "category", data: keys, ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: (v: string) => "нед. " + dateRu(v) } },
      yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: kRub } },
      series: [
        { name: "Поступления", type: "bar", stack: "f", data: keys.map((k) => chartRub(w.get(k)!.in.toString())), itemStyle: { color: COLORS.green } },
        { name: "Списания", type: "bar", stack: "f", data: keys.map((k) => chartRub(w.get(k)!.out.toString())), itemStyle: { color: COLORS.risk } },
      ],
    };
  }, [r]);

  const byDate = useMemo(() => {
    const m = new Map<string, NonNullable<typeof r>["planned"]>();
    r?.planned.forEach((p) => m.set(p.date, [...(m.get(p.date) ?? []), p]));
    return m;
  }, [r]);

  async function setPeriod(h: number) {
    setErr("");
    try { await recalc(h); } catch (e) { setErr((e as Error).message); }
  }

  if (runState === "loading") return <><PageHeader title="Прогноз" /><Loading /></>;
  if (runState === "error") return <><PageHeader title="Прогноз" /><Notice tone="risk">{error}</Notice></>;
  if (runState === "empty" || !r || !run)
    return (
      <>
        <PageHeader title="Прогноз" />
        <Card><Empty title="Прогноза пока нет" text="Загрузите выписку, чтобы построить прогноз." action={<button className="btn" onClick={openImport}>Загрузить выписку</button>} /></Card>
      </>
    );

  const risk = r.risk;
  const gap = risk.possible_break_date ?? risk.first_break_date;

  return (
    <div className="stack">
      <PageHeader
        title="Прогноз"
        subtitle={<>Движение денежных средств · данные на {dateRu(r.as_of)}</>}
        actions={
          <div className="period-bar" role="group" aria-label="Период прогноза">
            {PERIODS.map((h) => (
              <button key={h} className={"chip" + (r.horizon === h ? " on" : "")} aria-pressed={r.horizon === h} disabled={recalcBusy} onClick={() => setPeriod(h)}>{h} дн.</button>
            ))}
            {recalcBusy && <span className="small muted">Считаю…</span>}
          </div>
        }
      />
      {err && <Notice tone="risk">{err}</Notice>}
      {run.stale && <Notice tone="warn" action={<button className="btn sm" disabled={recalcBusy} onClick={() => setPeriod(r.horizon)}>Пересчитать</button>}>Данные изменились после последнего расчёта.</Notice>}

      <Card title="Баланс по дням">
        <Chart option={option} height={360} />
        <p className="small muted">Красная область — период, когда вероятность минуса не ниже 25 % или ожидаемый остаток отрицателен.</p>
      </Card>

      <Card title="Возможные кассовые разрывы" actions={<Badge tone={risk.level === "none" ? "ok" : risk.level === "high" ? "risk" : "warn"}>{risk.level === "none" ? "Разрыва не ожидается" : `Риск: ${pct(risk.shortfall_probability)}`}</Badge>}>
        {risk.level === "none" ? (
          <p className="muted">В {pct(1 - risk.shortfall_probability)} сценариев остаток остаётся положительным весь период.</p>
        ) : (
          <div className="gap-grid">
            <div className="gap-cell"><b>{gap ? dateRu(gap) : "—"}</b><span>вероятная дата (накопленная вероятность ≥ 10 %)</span></div>
            <div className="gap-cell"><b>{risk.first_break_date ? dateRu(risk.first_break_date) : "—"}</b><span>медианная дата первого разрыва</span></div>
            <div className="gap-cell"><b className="neg">{rub(risk.deficit_given_break_median)}</b><span>типичный дефицит, если разрыв случится</span></div>
            <div className="gap-cell"><b className="neg">{rub(risk.max_deficit_p90)}</b><span>дефицит в плохом случае (90-й перцентиль)</span></div>
            <div className="gap-cell"><b>{rub(risk.median_path_deficit)}</b><span>дефицит на медианной траектории</span></div>
          </div>
        )}
        {risk.warnings.filter((w) => w.reasons.length).map((w, i) => (
          <div key={i} style={{ marginTop: 14 }}>
            <div className="small muted">Что к этому приводит:</div>
            <ul className="small">{w.reasons.map((x, j) => <li key={j}>{dateRu(x.date)} — {x.title}: {rub(x.amount)}</li>)}</ul>
          </div>
        ))}
      </Card>

      <Card title="Известные поступления и списания по неделям">
        <Chart option={flowOption} height={280} />
        <p className="small muted">Только запланированные потоки (регулярные платежи, счета, ручные события); нерегулярные операции учтены в диапазоне прогноза, но здесь не показаны.</p>
      </Card>

      <Card title="Детали по датам" pad={false}>
        <div className="scroll-y table-wrap">
          <table>
            <thead><tr><th>Дата</th><th className="num">Ожидаемый остаток</th><th className="num">Диапазон 10–90 %</th><th className="num">Вероятность минуса</th><th>Платежи в этот день</th></tr></thead>
            <tbody>
              {r.days.map((d) => {
                const risky = d.prob_negative >= 0.25;
                const items = byDate.get(d.date) ?? [];
                return (
                  <tr key={d.date} className={(risky ? "row-risk " : "") + (picked === d.date ? "picked " : "") + "clickable"} onClick={() => setPicked(picked === d.date ? null : d.date)} onMouseEnter={() => setPicked(d.date)}>
                    <td>{dateRu(d.date)}</td>
                    <td className={"num money " + (BigInt(d.p50) < 0n ? "neg" : "")}>{rub(d.p50)}</td>
                    <td className="num money">{rub(d.p10)} … {rub(d.p90)}</td>
                    <td className="num">{pct(d.prob_negative)} {risky && <Badge tone="risk">риск</Badge>}</td>
                    <td><div className="day-items">{items.length === 0 ? <span className="muted">—</span> : items.map((p, i) => <span key={i}>{p.title}: <span className={BigInt(p.amount) < 0n ? "neg" : "pos"}>{rub(p.amount, { sign: true })}</span></span>)}</div></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Backtest />

      <Card title={`Регулярные платежи (${r.rules.length})`}>
        {r.rules.length === 0 ? <p className="muted">Регулярные платежи не найдены.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Контрагент</th><th>Периодичность</th><th className="num">Сумма</th><th className="num">Повторов</th></tr></thead>
            <tbody>{r.rules.map((x) => (
              <tr key={x.key}><td>{x.counterparty || x.key}</td><td>{x.kind === "monthly" ? `ежемесячно, ${x.anchors.join(" и ")}-го` : "еженедельно"}</td><td className={"num money " + (BigInt(x.amount) < 0n ? "neg" : "pos")}>{rub(x.amount, { sign: true })}</td><td className="num">{x.occurrences}</td></tr>
            ))}</tbody>
          </table></div>
        )}
        <p className="small muted" style={{ marginTop: 10 }}>Модель {r.model_version} · {r.n_paths} траекторий · seed {r.seed} · качество данных: {r.quality.level} ({r.quality.history_days} дн.)</p>
      </Card>
    </div>
  );
}
