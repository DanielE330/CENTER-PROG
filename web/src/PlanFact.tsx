import { useEffect, useMemo, useState } from "react";
import { api, chartRub, dateRu, rub } from "./api";
import Chart from "./Chart";

type PF = {
  as_of: string; missed: number; note: string;
  weeks: { week_start: string; plan_in: string; plan_out: string; fact_in: string; fact_out: string }[];
  items: { date: string; title: string; source: string; plan: string; fact: string | null; fact_date: string | null; diff: string | null; status: string }[];
};
const ST: Record<string, string> = { paid: "оплачено", deviation: "отклонение", missed: "не пришло", pending: "ожидается" };

export function PlanFact({ base, rev }: { base: string; rev: number }) {
  const [d, setD] = useState<PF | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.get<PF>(`${base}/plan-fact?days=90`).then(setD).catch((e) => setError(e.message)); }, [base, rev]);
  const option = useMemo(() => {
    if (!d) return {};
    const x = d.weeks.map((w) => w.week_start);
    return {
      grid: { left: 70, right: 20, top: 30, bottom: 50 },
      tooltip: { trigger: "axis", valueFormatter: (v: number) => `${v.toLocaleString("ru-RU")} ₽` },
      legend: { bottom: 0 },
      xAxis: { type: "category", data: x, axisLabel: { formatter: (v: string) => dateRu(v) } },
      yAxis: { type: "value", axisLabel: { formatter: (v: number) => `${Math.round(v / 1000)} тыс.` } },
      series: [
        { name: "План: поступления", type: "bar", data: d.weeks.map((w) => chartRub(w.plan_in)), itemStyle: { color: "#93c5fd" } },
        { name: "Факт: поступления", type: "bar", data: d.weeks.map((w) => chartRub(w.fact_in)), itemStyle: { color: "#16a34a" } },
        { name: "План: платежи", type: "bar", data: d.weeks.map((w) => -chartRub(w.plan_out)), itemStyle: { color: "#fdba74" } },
        { name: "Факт: платежи", type: "bar", data: d.weeks.map((w) => -chartRub(w.fact_out)), itemStyle: { color: "#dc2626" } },
      ],
    };
  }, [d]);
  if (error) return <div className="alert high">{error}</div>;
  if (!d) return <div className="muted">Загрузка…</div>;
  return (
    <>
      {d.missed > 0 && <div className="alert medium"><b>{d.missed}</b> ожидавшихся платежей не пришли в срок. Прогноз учитывает их как просроченные.</div>}
      <section className="card"><h3>План и факт по неделям</h3><Chart option={option} /><p className="small muted">{d.note}</p></section>
      <section className="card">
        <h3>Платежи</h3>
        <table>
          <thead><tr><th>Дата</th><th>Платёж</th><th className="num">План</th><th className="num">Факт</th><th>Статус</th></tr></thead>
          <tbody>{[...d.items].reverse().map((i, k) => (
            <tr key={k} className={i.status === "missed" ? "row-bad" : ""}>
              <td>{dateRu(i.date)}</td><td>{i.title}</td><td className="num">{rub(i.plan, { sign: true })}</td>
              <td className="num">{i.fact ? `${rub(i.fact, { sign: true })} (${dateRu(i.fact_date!)})` : "—"}</td><td><span className={"chip " + i.status}>{ST[i.status]}</span></td>
            </tr>
          ))}</tbody>
        </table>
      </section>
    </>
  );
}
