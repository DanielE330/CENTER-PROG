import { useEffect, useMemo, useState } from "react";
import { ApiError, api, chartRub, dateRu, rub } from "../api";
import Chart from "../Chart";
import { COLORS, axisBase, kRub } from "../charts";
import { useApp } from "../ctx";
import { Icon } from "../icons";
import { Badge, Card, Empty, Kpi, Loading, Notice, PageHeader, type Tone } from "../ui";

type PF = {
  as_of: string; missed: number; note: string;
  weeks: { week_start: string; plan_in: string; plan_out: string; fact_in: string; fact_out: string }[];
  items: { date: string; title: string; source: string; plan: string; fact: string | null; fact_date: string | null; diff: string | null; status: string }[];
};
type BT = { series_14?: { date: string; predicted: string; actual: string }[] };

const ST: Record<string, { label: string; tone: Tone }> = {
  paid: { label: "Оплачено", tone: "ok" }, deviation: { label: "Отклонение", tone: "warn" },
  missed: { label: "Не пришло", tone: "risk" }, pending: { label: "Ожидается", tone: "neutral" },
};

/** Отклонение в % от плана (BigInt, без потери точности). */
function devPct(diff: string | null, plan: string): string {
  if (diff == null) return "—";
  const p = BigInt(plan);
  if (p === 0n) return "—";
  const v = Number((BigInt(diff) * 1000n) / (p < 0n ? -p : p)) / 10;
  return `${v > 0 ? "+" : ""}${v.toFixed(1).replace(".", ",")}%`;
}

const sum = (xs: string[]) => xs.reduce((a, x) => a + BigInt(x), 0n).toString();

export default function PlanFact() {
  const { base, rev, runState, openImport } = useApp();
  const [d, setD] = useState<PF | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "empty" | "error">("loading");
  const [error, setError] = useState("");
  const [bt, setBt] = useState<BT | null>(null);
  const [btMsg, setBtMsg] = useState("");

  useEffect(() => {
    let alive = true;
    setState("loading");
    api.get<PF>(`${base}/plan-fact?days=90`)
      .then((r) => { if (alive) { setD(r); setState("ok"); } })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && (e.code === "no_data" || e.status === 422)) setState("empty");
        else { setError((e as Error).message); setState("error"); }
      });
    setBt(null); setBtMsg("");
    api.get<BT>(`${base}/backtest`)
      .then((r) => { if (alive) setBt(r); })
      .catch((e) => { if (alive) setBtMsg(e instanceof ApiError && e.code === "not_enough_history" ? "Для сравнения прогнозного и фактического остатка нужно не менее 5 месяцев истории." : "Не удалось рассчитать прогнозный остаток."); });
    return () => { alive = false; };
  }, [base, rev]);

  const weekly = useMemo(() => {
    if (!d) return {};
    return {
      animation: false,
      grid: { left: 64, right: 16, top: 24, bottom: 56 },
      tooltip: { trigger: "axis", backgroundColor: "#fff", borderColor: COLORS.line, textStyle: { color: COLORS.text }, valueFormatter: (v: number) => `${v.toLocaleString("ru-RU")} ₽` },
      legend: { bottom: 0, textStyle: { color: COLORS.muted } },
      xAxis: { type: "category", data: d.weeks.map((w) => w.week_start), ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: (v: string) => dateRu(v) } },
      yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: kRub } },
      series: [
        { name: "План: поступления", type: "bar", data: d.weeks.map((w) => chartRub(w.plan_in)), itemStyle: { color: "#a9d4bb" } },
        { name: "Факт: поступления", type: "bar", data: d.weeks.map((w) => chartRub(w.fact_in)), itemStyle: { color: COLORS.green } },
        { name: "План: списания", type: "bar", data: d.weeks.map((w) => -chartRub(w.plan_out)), itemStyle: { color: "#f1c48b" } },
        { name: "Факт: списания", type: "bar", data: d.weeks.map((w) => -chartRub(w.fact_out)), itemStyle: { color: COLORS.warn } },
      ],
    };
  }, [d]);

  const balance = useMemo(() => {
    const s = bt?.series_14;
    if (!s || s.length === 0) return null;
    return {
      animation: false,
      grid: { left: 64, right: 16, top: 24, bottom: 56 },
      tooltip: { trigger: "axis", backgroundColor: "#fff", borderColor: COLORS.line, textStyle: { color: COLORS.text }, valueFormatter: (v: number) => `${v.toLocaleString("ru-RU")} ₽` },
      legend: { bottom: 0, textStyle: { color: COLORS.muted } },
      xAxis: { type: "category", data: s.map((x) => x.date), ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: (v: string) => dateRu(v) } },
      yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: kRub } },
      series: [
        { name: "Прогнозный остаток", type: "line", data: s.map((x) => chartRub(x.predicted)), symbol: "none", lineStyle: { width: 2, color: COLORS.warn, type: "dashed" } },
        { name: "Фактический остаток", type: "line", data: s.map((x) => chartRub(x.actual)), symbol: "none", lineStyle: { width: 2.5, color: COLORS.green } },
      ],
    };
  }, [bt]);

  if (runState === "loading" || state === "loading") return <Loading />;
  if (state === "error") return (<><PageHeader title="План-факт" /><Notice tone="risk">{error}</Notice></>);
  if (state === "empty" || !d) {
    return (
      <>
        <PageHeader title="План-факт" subtitle="Сравнение ожидаемых и реальных платежей" />
        <Card><Empty title="Нет данных для сравнения" text="Загрузите банковскую выписку, чтобы сравнить план с фактом."
          action={<button className="btn" onClick={openImport}><Icon name="upload" size={16} />Загрузить выписку</button>} /></Card>
      </>
    );
  }

  const pIn = sum(d.weeks.map((w) => w.plan_in)), fIn = sum(d.weeks.map((w) => w.fact_in));
  const pOut = sum(d.weeks.map((w) => w.plan_out)), fOut = sum(d.weeks.map((w) => w.fact_out));
  const dIn = (BigInt(fIn) - BigInt(pIn)).toString();
  const dOut = (BigInt(fOut) - BigInt(pOut)).toString();

  return (
    <>
      <PageHeader title="План-факт" subtitle={`Последние 90 дней до ${dateRu(d.as_of)}: что ожидали и что произошло`} />
      <div className="stack">
        {d.missed > 0 && (
          <Notice tone="warn">
            <b>Не пришло в срок: {d.missed}.</b> Такие платежи прогноз считает просроченными: поступления переносятся на ближайшие дни с учётом типичной задержки, расходы — на завтра. Остаток и риск разрыва пересчитаны с учётом этого.
          </Notice>
        )}
        <div className="grid kpis">
          <Kpi label="Плановые поступления" value={rub(pIn)} hint="по регулярным платежам и событиям" />
          <Kpi label="Фактические поступления" value={rub(fIn)} hint={<>Отклонение {rub(dIn, { sign: true })} {BigInt(dIn) < 0n ? <Badge tone="warn">меньше плана</Badge> : <Badge tone="ok">в норме</Badge>}</>} tone={BigInt(dIn) < 0n ? "warn" : "ok"} />
          <Kpi label="Плановые списания" value={rub(pOut)} hint="по регулярным платежам и событиям" />
          <Kpi label="Фактические списания" value={rub(fOut)} hint={<>Отклонение {rub(dOut, { sign: true })} {BigInt(dOut) > 0n ? <Badge tone="warn">больше плана</Badge> : <Badge tone="ok">в норме</Badge>}</>} tone={BigInt(dOut) > 0n ? "warn" : "ok"} />
        </div>

        <Card title="Поступления и списания по неделям">
          {d.weeks.length === 0 ? <Empty title="Нет плановых платежей в периоде" /> : <Chart option={weekly} height={340} />}
          <p className="small muted">{d.note}</p>
        </Card>

        <Card title="Прогнозный и фактический остаток">
          {balance ? (
            <>
              <Chart option={balance} height={320} />
              <p className="small muted">Прогноз на 14 дней, сделанный на основе только прошлых данных, против реального остатка на целевую дату.</p>
            </>
          ) : btMsg ? <Empty title="Сравнение остатков недоступно" text={btMsg} /> : <Loading text="Считаю прогноз по прошлым периодам…" />}
        </Card>

        <Card title="Платежи: план и факт" pad={false}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Дата</th><th>Платёж</th><th className="num">План</th><th className="num">Факт</th><th className="num">Отклонение</th><th className="num">%</th><th>Статус</th></tr>
              </thead>
              <tbody>
                {[...d.items].reverse().map((i, k) => {
                  const st = ST[i.status] ?? { label: i.status, tone: "neutral" as Tone };
                  const dev = i.diff != null ? BigInt(i.diff) : null;
                  const bad = dev != null && dev !== 0n && st.tone !== "ok";
                  return (
                    <tr key={k} className={i.status === "missed" ? "row-risk" : ""}>
                      <td>{dateRu(i.date)}</td>
                      <td>{i.title}</td>
                      <td className="num money">{rub(i.plan, { sign: true })}</td>
                      <td className="num money">{i.fact ? <>{rub(i.fact, { sign: true })}<div className="small muted">{i.fact_date ? dateRu(i.fact_date) : ""}</div></> : "—"}</td>
                      <td className={`num money ${bad ? "neg" : ""}`}>{i.diff != null ? rub(i.diff, { sign: true }) : "—"}</td>
                      <td className={`num money ${bad ? "neg" : ""}`}>{devPct(i.diff, i.plan)}</td>
                      <td><Badge tone={st.tone}>{st.label}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </>
  );
}
