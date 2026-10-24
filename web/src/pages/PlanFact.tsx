import { useQuery } from "@tanstack/react-query";
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api } from "../api";
import { day, dayShort, money, shortMoney } from "../format";

interface PlanFactData {
  as_of: string;
  plan_as_of: string | null;
  points: { date: string; plan_p10_kop: number; plan_p50_kop: number; plan_p90_kop: number; fact_kop: number | null }[];
  weeks: { week: string; plan_in_kop: number; plan_out_kop: number; fact_in_kop: number; fact_out_kop: number; deviation_kop: number; flag: boolean }[];
}

export function PlanFact() {
  const q = useQuery({ queryKey: ["planfact"], queryFn: () => api.get<PlanFactData>("/api/v1/plan-fact") });
  const d = q.data;
  if (!d) return <div className="card">Загрузка…</div>;
  if (!d.plan_as_of)
    return <div className="card">Сравнивать пока не с чем: нужен прогноз, сделанный раньше. В демо нажмите «Прошла неделя» на главной.</div>;
  const data = d.points.map((p) => ({
    date: p.date,
    band: [p.plan_p10_kop / 100, p.plan_p90_kop / 100],
    plan: p.plan_p50_kop / 100,
    fact: p.fact_kop === null ? null : p.fact_kop / 100,
  }));
  return (
    <div className="stack">
      <div className="card">
        <div className="muted">
          Прогноз от {day(d.plan_as_of)} против того, что произошло до {day(d.as_of)}.
        </div>
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={data} margin={{ top: 10, right: 16, left: 8, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
            <XAxis dataKey="date" tickFormatter={dayShort} minTickGap={24} />
            <YAxis tickFormatter={(v: number) => shortMoney(v * 100)} width={70} />
            <Tooltip labelFormatter={(l) => day(String(l))} formatter={(v) => (Array.isArray(v) ? `${money(Number(v[0]) * 100)} … ${money(Number(v[1]) * 100)}` : money(Number(v) * 100))} />
            <Legend formatter={(v) => ({ band: "коридор плана", plan: "план", fact: "факт" })[String(v)] ?? String(v)} />
            <Area dataKey="band" stroke="none" fill="var(--accent)" fillOpacity={0.15} isAnimationActive={false} />
            <Line dataKey="plan" stroke="var(--accent)" dot={false} strokeDasharray="5 4" isAnimationActive={false} />
            <Line dataKey="fact" stroke="var(--ink)" dot={false} strokeWidth={2} connectNulls isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Неделя</th>
              <th>План: приход</th>
              <th>Факт: приход</th>
              <th>План: расход</th>
              <th>Факт: расход</th>
              <th>Отклонение</th>
            </tr>
          </thead>
          <tbody>
            {d.weeks.map((w) => (
              <tr key={w.week} className={w.flag ? "overdue" : ""}>
                <td>с {day(w.week)}</td>
                <td className="num">{money(w.plan_in_kop)}</td>
                <td className="num">{money(w.fact_in_kop)}</td>
                <td className="num">{money(w.plan_out_kop)}</td>
                <td className="num">{money(w.fact_out_kop)}</td>
                <td className={w.deviation_kop >= 0 ? "pos num" : "neg num"}>{money(w.deviation_kop)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="muted">Подсвечены недели, где факт разошёлся с планом больше чем на 20%.</div>
      </div>
    </div>
  );
}
