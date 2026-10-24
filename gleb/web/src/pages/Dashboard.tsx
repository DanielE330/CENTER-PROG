import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { type Alert, api, type Forecast, type Gap, type Me } from "../api";
import { day, dayShort, likelihood, money, shortMoney, WARNINGS } from "../format";
import { Upload } from "./Upload";

interface DemoStatus {
  replay: boolean;
  company?: string;
  as_of?: string;
  data_until?: string;
}

function headline(f: Forecast): { tone: string; text: string } {
  const gap = f.gaps[0];
  if (!gap) return { tone: "ok", text: `Разрывов в ближайшие ${f.horizon} дней не видно` };
  return {
    tone: gap.level === "critical" ? "bad" : "warn",
    text: `${likelihood(gap.probability)[0].toUpperCase()}${likelihood(gap.probability).slice(1)} ${day(gap.start_date)} не хватит около ${money(gap.shortfall_p50_kop)}`,
  };
}

export function ForecastChart({ forecast, height = 320 }: { forecast: Forecast; height?: number }) {
  const data = forecast.points.map((p) => ({ date: p.date, band: [p.p10_kop / 100, p.p90_kop / 100], p50: p.p50_kop / 100 }));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 10, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
        <XAxis dataKey="date" tickFormatter={dayShort} minTickGap={24} />
        <YAxis tickFormatter={(v: number) => shortMoney(v * 100)} width={70} />
        <Tooltip
          labelFormatter={(l) => day(String(l))}
          formatter={(v, name) => (Array.isArray(v) ? [`${money(Number(v[0]) * 100)} … ${money(Number(v[1]) * 100)}`, "коридор P10–P90"] : [money(Number(v) * 100), name === "p50" ? "медиана" : String(name)])}
        />
        {forecast.gaps.map((g) => (
          <ReferenceArea key={g.start_date} x1={g.start_date} x2={g.end_date} fill={g.level === "critical" ? "var(--bad)" : "var(--warn)"} fillOpacity={0.15} />
        ))}
        <ReferenceLine y={0} stroke="var(--bad)" strokeDasharray="4 4" />
        <Area dataKey="band" stroke="none" fill="var(--accent)" fillOpacity={0.18} isAnimationActive={false} />
        <Line dataKey="p50" stroke="var(--accent)" strokeWidth={2} dot={false} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

function GapCard({ gap }: { gap: Gap }) {
  return (
    <div className={`card gap ${gap.level}`}>
      <div className="gap-head">
        <strong>{day(gap.start_date)}</strong>
        <span className="pill">
          {likelihood(gap.probability)} · {Math.round(gap.probability * 100)}%
        </span>
      </div>
      <div>
        Не хватит около <b>{money(gap.shortfall_p50_kop)}</b>, в плохом случае до {money(gap.shortfall_p90_kop)}.
        {gap.level === "critical" && " Это за пределами лимита овердрафта."}
      </div>
      {gap.drivers.length > 0 && (
        <div className="drivers">
          <div className="muted">Что приходится на эти дни:</div>
          <ul>
            {gap.drivers.map((d, i) => (
              <li key={i}>
                {day(d.date)} — {d.label}: {money(d.amount_kop)}
              </li>
            ))}
          </ul>
        </div>
      )}
      <a href="#advisor">Спросить консультанта, что делать →</a>
    </div>
  );
}

export function Dashboard({ me }: { me: Me }) {
  const qc = useQueryClient();
  const [horizon, setHorizon] = useState(30);
  const forecast = useQuery({ queryKey: ["forecast", horizon], queryFn: () => api.get<Forecast>(`/api/v1/forecast?horizon=${horizon}`), retry: false });
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: () => api.get<Alert[]>("/api/v1/alerts?status=open") });
  const demo = useQuery({ queryKey: ["demo"], queryFn: () => api.get<DemoStatus>("/api/v1/demo/status") });
  const advance = useMutation({
    mutationFn: (days: number) => api.post<{ rows: number; as_of: string; open_invoices: number }>("/api/v1/demo/advance", { days }),
    onSuccess: () => qc.invalidateQueries(),
  });
  const ack = useMutation({ mutationFn: (id: string) => api.post(`/api/v1/alerts/${id}/ack`), onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }) });
  const canWrite = me.roles.some((r) => r === "owner" || r === "accountant");

  if (forecast.isError) {
    return (
      <div className="stack">
        <div className="card">Данных для прогноза пока нет. Загрузите выписку.</div>
        {canWrite && <Upload />}
      </div>
    );
  }
  const f = forecast.data;
  const h = f ? headline(f) : null;
  return (
    <div className="stack">
      {demo.data?.replay && (
        <div className="card demo">
          <div>
            Демо-режим: компания {demo.data.company}, «сегодня» — <b>{day(demo.data.as_of ?? "")}</b> (данные до {day(demo.data.data_until ?? "")}).
          </div>
          {canWrite && (
            <div className="row">
              <button disabled={advance.isPending} onClick={() => advance.mutate(7)}>
                Прошла неделя
              </button>
              <button className="secondary" disabled={advance.isPending} onClick={() => advance.mutate(1)}>
                +1 день
              </button>
              {advance.data && (
                <span className="muted">
                  +{advance.data.rows} операций, открытых счетов: {advance.data.open_invoices}
                </span>
              )}
              {advance.isError && <span className="error">{String(advance.error)}</span>}
            </div>
          )}
        </div>
      )}
      {f && h && (
        <>
          <div className={`headline ${h.tone}`}>{h.text}</div>
          <div className="muted">
            Сейчас на счёте {money(f.opening_kop)} · прогноз на {day(f.as_of)} · модель {f.model}
            {f.timing_ms !== undefined && ` · расчёт ${f.timing_ms} мс${f.refit === false ? " без переобучения" : ""}`}
          </div>
          {f.warnings.map((w) => (
            <div key={w} className="card warn-note">
              {WARNINGS[w] ?? w}
            </div>
          ))}
          <div className="card">
            <div className="row space">
              <strong>Остаток по дням</strong>
              <div className="row">
                {[30, 60, 90].map((n) => (
                  <button key={n} className={n === horizon ? "" : "secondary"} onClick={() => setHorizon(n)}>
                    {n} дней
                  </button>
                ))}
              </div>
            </div>
            <ForecastChart forecast={f} />
            <div className="muted">Линия — самый вероятный остаток, заливка — коридор, в который он попадёт в 8 случаях из 10.</div>
          </div>
          <div className="grid2">
            <div className="stack">
              <h2>Кассовые разрывы</h2>
              {f.gaps.length === 0 && <div className="card">Не видно.</div>}
              {f.gaps.map((g) => (
                <GapCard key={g.start_date + g.level} gap={g} />
              ))}
            </div>
            <div className="stack">
              <h2>Предупреждения</h2>
              {(alerts.data ?? []).length === 0 && <div className="card">Нет открытых предупреждений.</div>}
              {(alerts.data ?? []).map((a) => (
                <div key={a.id} className={`card alert ${a.severity}`}>
                  <strong>{a.title}</strong>
                  <div>{a.body}</div>
                  <button className="link" onClick={() => ack.mutate(a.id)}>
                    Понятно
                  </button>
                </div>
              ))}
            </div>
          </div>
          {f.sources && (
            <div className="muted">
              Источники: операций {f.sources.transactions}
              {Object.entries(f.sources.obligations).map(([k, v]) => ` · ${k}: ${v}`)}
              {f.sources.registry_flags > 0 && ` · отметок реестра: ${f.sources.registry_flags}`}
            </div>
          )}
        </>
      )}
      {forecast.isLoading && <div className="card">Считаем прогноз…</div>}
      {canWrite && <Upload />}
    </div>
  );
}
