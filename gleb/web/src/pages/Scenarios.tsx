import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, type Gap } from "../api";
import { day, dayShort, money, shortMoney } from "../format";

interface Compare {
  as_of: string;
  points: { date: string; base_p50_kop: number; scenario_p50_kop: number }[];
  base_gaps: Gap[];
  scenario_gaps: Gap[];
}

const PRESETS = [
  { id: "delay", title: "Клиенты платят позже", hint: "на сколько дней" },
  { id: "revenue", title: "Выручка падает", hint: "на сколько процентов" },
  { id: "oneoff", title: "Внеплановый расход", hint: "сумма, ₽" },
  { id: "fixed", title: "Растут постоянные расходы", hint: "на сколько процентов" },
] as const;

type PresetId = (typeof PRESETS)[number]["id"];

function transforms(preset: PresetId, value: number, date: string): object[] {
  switch (preset) {
    case "delay":
      return [{ type: "delay_all_customers", days: value }];
    case "revenue":
      return [{ type: "scale_group", group: "revenue", factor: 1 - value / 100 }];
    case "oneoff":
      return [{ type: "one_off", amount_kop: Math.round(value * 100), date, direction: "outflow" }];
    case "fixed":
      return [
        { type: "scale_recurring", group: "rent_utilities", factor: 1 + value / 100 },
        { type: "scale_recurring", group: "purchases", factor: 1 + value / 100 },
      ];
  }
}

function firstGap(gaps: Gap[]): string {
  const g = gaps[0];
  return g ? `${day(g.start_date)}, не хватит около ${money(g.shortfall_p50_kop)} (${Math.round(g.probability * 100)}%)` : "разрыва нет";
}

export function Scenarios() {
  const [preset, setPreset] = useState<PresetId>("delay");
  const [value, setValue] = useState(21);
  const [date, setDate] = useState("");
  const run = useMutation({ mutationFn: () => api.post<Compare>("/api/v1/scenarios/preview", { transforms: transforms(preset, value, date) }) });
  const data = run.data?.points.map((p) => ({ date: p.date, base: p.base_p50_kop / 100, scenario: p.scenario_p50_kop / 100 }));

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          {PRESETS.map((p) => (
            <button key={p.id} className={p.id === preset ? "" : "secondary"} onClick={() => setPreset(p.id)}>
              {p.title}
            </button>
          ))}
        </div>
        <div className="row">
          <label>
            {PRESETS.find((p) => p.id === preset)?.hint}:{" "}
            <input type="number" value={value} min={0} onChange={(e) => setValue(Number(e.target.value))} />
          </label>
          {preset === "oneoff" && <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />}
          <button disabled={run.isPending || (preset === "oneoff" && !date)} onClick={() => run.mutate()}>
            Посчитать
          </button>
        </div>
        {run.isError && <div className="error">{String(run.error)}</div>}
      </div>
      {run.data && data && (
        <div className="card">
          <div className="grid2">
            <div>
              <div className="muted">Сейчас</div>
              <div>{firstGap(run.data.base_gaps)}</div>
            </div>
            <div>
              <div className="muted">В сценарии</div>
              <div className={run.data.scenario_gaps.length ? "error" : ""}>{firstGap(run.data.scenario_gaps)}</div>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={320}>
            <LineChart data={data} margin={{ top: 10, right: 16, left: 8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
              <XAxis dataKey="date" tickFormatter={dayShort} minTickGap={24} />
              <YAxis tickFormatter={(v: number) => shortMoney(v * 100)} width={70} />
              <Tooltip labelFormatter={(l) => day(String(l))} formatter={(v) => money(Number(v) * 100)} />
              <Legend formatter={(v) => (v === "base" ? "базовый прогноз" : "сценарий")} />
              <ReferenceLine y={0} stroke="var(--bad)" strokeDasharray="4 4" />
              <Line dataKey="base" stroke="var(--accent)" dot={false} strokeWidth={2} isAnimationActive={false} />
              <Line dataKey="scenario" stroke="var(--bad)" dot={false} strokeWidth={2} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
