import { useEffect, useMemo, useState } from "react";
import { ApiError, api, chartRub, dateRu, pct, rub } from "./api";
import Chart from "./Chart";
import type { Result, Run } from "./types";

const LEVEL_RU = { none: "Разрыва не ожидается", low: "Небольшой риск", medium: "Заметный риск", high: "Высокий риск разрыва" } as const;

export function forecastOption(r: Result, extra?: { name: string; r: Result }) {
  const dates = r.days.map((d) => d.date);
  const p10 = r.days.map((d) => chartRub(d.p10));
  const p90 = r.days.map((d) => chartRub(d.p90));
  const brk = r.risk.possible_break_date ?? r.risk.first_break_date;
  const series: any[] = [
    { name: "p10", type: "line", data: p10, stack: "band", lineStyle: { opacity: 0 }, symbol: "none", tooltip: { show: false } },
    { name: "Диапазон 10–90%", type: "line", data: p90.map((v, i) => v - p10[i]), stack: "band", lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(37,99,235,.15)" }, symbol: "none" },
    {
      name: "Ожидаемый остаток", type: "line", data: r.days.map((d) => chartRub(d.p50)), symbol: "none", lineStyle: { width: 3, color: "#2563eb" },
      markLine: {
        symbol: "none", silent: true,
        data: [
          { yAxis: 0, lineStyle: { color: "#dc2626", type: "solid" }, label: { formatter: "0 ₽", position: "insideStartTop" } },
          ...(brk ? [{ xAxis: brk, lineStyle: { color: "#dc2626", type: "dashed" }, label: { formatter: "риск разрыва", position: "insideEndTop" } }] : []),
        ],
      },
    },
  ];
  if (extra) series.push({ name: extra.name, type: "line", data: extra.r.days.map((d) => chartRub(d.p50)), symbol: "none", lineStyle: { width: 3, color: "#ea580c", type: "dashed" } });
  return {
    grid: { left: 70, right: 20, top: 30, bottom: 40 },
    tooltip: {
      trigger: "axis",
      formatter: (ps: any[]) => {
        const i = ps[0].dataIndex;
        const d = r.days[i];
        const l = [`<b>${dateRu(d.date)}</b>`, `Ожидаемый: ${rub(d.p50)}`, `Диапазон: ${rub(d.p10)} … ${rub(d.p90)}`, `Вероятность минуса: ${pct(d.prob_negative)}`];
        if (extra) l.push(`Сценарий: ${rub(extra.r.days[i].p50)}`);
        return l.join("<br/>");
      },
    },
    legend: { bottom: 0, data: extra ? ["Ожидаемый остаток", extra.name, "Диапазон 10–90%"] : ["Ожидаемый остаток", "Диапазон 10–90%"] },
    xAxis: { type: "category", data: dates, axisLabel: { formatter: (v: string) => dateRu(v) } },
    yAxis: { type: "value", axisLabel: { formatter: (v: number) => `${Math.round(v / 1000)} тыс.` } },
    series,
  };
}

export function Dashboard({ base, rev, onChange, openImport }: { base: string; rev: number; onChange: () => void; openImport: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [state, setState] = useState<"loading" | "empty" | "ok" | "error">("loading");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setState("loading");
    api.get<Run>(`${base}/forecasts/latest`).then((r) => { setRun(r); setState("ok"); }).catch((e) => {
      if (e instanceof ApiError && e.status === 404) setState("empty");
      else { setError(e.message); setState("error"); }
    });
  }, [base, rev]);

  async function recalc() {
    setBusy(true); setError("");
    try { await api.post(`${base}/forecasts`); onChange(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const r = run?.result;
  const option = useMemo(() => (r ? forecastOption(r) : {}), [r]);

  if (state === "loading") return <div className="muted">Загрузка…</div>;
  if (state === "error") return <div className="alert high">{error}</div>;
  if (state === "empty" || !r)
    return (
      <div className="card empty">
        <h2>Прогноза пока нет</h2>
        <p className="muted">Загрузите банковскую выписку — сервис найдёт регулярные платежи и покажет остаток по дням на 30 дней вперёд.</p>
        <button onClick={openImport}>Загрузить выписку</button>
        <button className="ghost" onClick={recalc} disabled={busy}>Построить по уже загруженным данным</button>
        {error && <div className="alert high">{error}</div>}
      </div>
    );
  const risk = r.risk;
  const gap = risk.possible_break_date ?? risk.first_break_date;
  return (
    <>
      {run!.stale && <div className="alert medium">Данные изменились после последнего расчёта. <button className="link" onClick={recalc} disabled={busy}>Пересчитать</button></div>}
      <section className={`card hero ${risk.level}`}>
        <div>
          <div className="small muted">Прогноз на {r.horizon} дн. от {dateRu(r.as_of)}</div>
          <h2>{LEVEL_RU[risk.level]}</h2>
          {gap ? (
            <p>С вероятностью <b>{pct(risk.shortfall_probability)}</b> денег не хватит. Вероятная дата — <b>{dateRu(gap)}</b>,
              типичный дефицит — <b title="Медиана максимального дефицита среди сценариев, где разрыв случился">{rub(risk.deficit_given_break_median)}</b>,
              в плохом случае (90-й перцентиль) — <b>{rub(risk.max_deficit_p90)}</b>.</p>
          ) : <p>В {pct(1 - risk.shortfall_probability)} сценариев остаток остаётся положительным весь период.</p>}
        </div>
        <div className="stats">
          <div className="stat"><b>{rub(r.opening_balance)}</b><span>остаток на {dateRu(r.as_of)}</span></div>
          <div className="stat"><b>{pct(risk.shortfall_probability)}</b><span>вероятность разрыва</span></div>
          <div className="stat"><b>{rub(r.days[r.days.length - 1].p50)}</b><span>ожидаемый остаток в конце</span></div>
        </div>
      </section>

      <section className="card">
        <div className="row"><h3>Остаток по дням</h3><div className="grow" /><button className="ghost" onClick={recalc} disabled={busy}>{busy ? "Считаю…" : "Пересчитать"}</button></div>
        <Chart option={option} />
        <p className="small muted">Линия — ожидаемый остаток, полоса — диапазон от 10% до 90% сценариев. Это модельная оценка: чем короче история, тем она грубее.</p>
      </section>

      <div className="cols">
        <section className="card">
          <h3>Предупреждения</h3>
          {risk.warnings.length === 0 && <p className="muted">Предупреждений нет.</p>}
          {risk.warnings.map((w, i) => (
            <div key={i} className={`alert ${w.severity}`}>
              <b>{w.message}</b>{w.date && <span className="small"> · {dateRu(w.date)}</span>}
              {w.reasons.length > 0 && <ul>{w.reasons.map((x, j) => <li key={j}>{dateRu(x.date)} — {x.title}: {rub(x.amount)}{x.overdue ? " (просрочено)" : ""}</li>)}</ul>}
            </div>
          ))}
          {r.quality.notes.map((n, i) => <div key={i} className="alert low small">{n}</div>)}
        </section>
        <section className="card">
          <h3>Ближайшие платежи</h3>
          <table><tbody>
            {r.planned.slice(0, 12).map((p, i) => (
              <tr key={i}><td>{dateRu(p.date)}</td><td>{p.title}{p.source !== "recurring" && <span className="chip">{p.source}</span>}</td><td className={"num " + (BigInt(p.amount) < 0n ? "neg" : "pos")}>{rub(p.amount, { sign: true })}</td></tr>
            ))}
          </tbody></table>
        </section>
      </div>

      <section className="card">
        <h3>Найденные регулярные платежи ({r.rules.length})</h3>
        <table>
          <thead><tr><th>Контрагент</th><th>Периодичность</th><th className="num">Сумма</th><th>Повторов</th></tr></thead>
          <tbody>{r.rules.map((x) => (
            <tr key={x.key}><td>{x.counterparty || x.key}</td><td>{x.kind === "monthly" ? `ежемесячно, ${x.anchors.join(" и ")}-го` : "еженедельно"}</td><td className="num">{rub(x.amount, { sign: true })}</td><td>{x.occurrences}</td></tr>
          ))}</tbody>
        </table>
        <p className="small muted">Модель {r.model_version} · {r.n_paths} траекторий · seed {r.seed} · качество данных: {r.quality.level} ({r.quality.history_days} дн. истории)</p>
      </section>
    </>
  );
}
