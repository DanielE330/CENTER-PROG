import { chartRub, dateRu, pct, rub } from "./api";
import type { Result } from "./types";

export const COLORS = {
  green: "#176B45", greenSoft: "#E5F4EA", line: "#E2E8E3", text: "#202923", muted: "#66716A", warn: "#D97706", risk: "#DC5252",
  band: "rgba(23,107,69,.12)", scenario: "#D97706",
};

export const axisBase = {
  axisLine: { lineStyle: { color: COLORS.line } },
  axisLabel: { color: COLORS.muted },
  splitLine: { lineStyle: { color: COLORS.line } },
};

export const kRub = (v: number) => (Math.abs(v) >= 1_000_000 ? `${(v / 1_000_000).toFixed(1).replace(".", ",")} млн` : `${Math.round(v / 1000)} тыс.`);

/** Периоды, в которые ожидаемый остаток или нижняя граница диапазона уходят в минус. */
export function deficitAreas(r: Result) {
  const areas: any[] = [];
  let start: string | null = null;
  r.days.forEach((d, i) => {
    const bad = d.prob_negative >= 0.25 || BigInt(d.p50) < 0n;
    if (bad && start === null) start = d.date;
    if ((!bad || i === r.days.length - 1) && start !== null) {
      areas.push([{ xAxis: start, itemStyle: { color: "rgba(220,82,82,.10)" } }, { xAxis: bad ? d.date : r.days[i - 1].date }]);
      start = null;
    }
  });
  return areas;
}

/** График остатка: ожидаемая линия, диапазон 10–90 %, граница нуля, период дефицита; опционально сценарий. */
export function forecastOption(r: Result, extra?: { name: string; r: Result }) {
  const p10 = r.days.map((d) => chartRub(d.p10));
  const p90 = r.days.map((d) => chartRub(d.p90));
  const brk = r.risk.possible_break_date ?? r.risk.first_break_date;
  const series: any[] = [
    { name: "p10", type: "line", data: p10, stack: "band", lineStyle: { opacity: 0 }, symbol: "none", tooltip: { show: false }, silent: true },
    { name: "Диапазон 10–90%", type: "line", data: p90.map((v, i) => v - p10[i]), stack: "band", lineStyle: { opacity: 0 }, areaStyle: { color: COLORS.band }, symbol: "none", silent: true },
    {
      name: "Ожидаемый остаток", type: "line", data: r.days.map((d) => chartRub(d.p50)), symbol: "none", smooth: false, lineStyle: { width: 2.5, color: COLORS.green },
      markArea: { silent: true, data: deficitAreas(r) },
      markLine: {
        symbol: "none", silent: true,
        data: [
          { yAxis: 0, lineStyle: { color: COLORS.risk, type: "solid", width: 1 }, label: { formatter: "0 ₽", position: "insideStartTop", color: COLORS.risk } },
          ...(brk ? [{ xAxis: brk, lineStyle: { color: COLORS.risk, type: "dashed" }, label: { formatter: "риск разрыва", position: "insideEndTop", color: COLORS.risk } }] : []),
        ],
      },
    },
  ];
  if (extra) series.push({ name: extra.name, type: "line", data: extra.r.days.map((d) => chartRub(d.p50)), symbol: "none", lineStyle: { width: 2.5, color: COLORS.scenario, type: "dashed" } });
  return {
    animation: false,
    grid: { left: 64, right: 16, top: 24, bottom: 44 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#fff", borderColor: COLORS.line, textStyle: { color: COLORS.text },
      formatter: (ps: any[]) => {
        const i = ps[0].dataIndex;
        const d = r.days[i];
        const l = [`<b>${dateRu(d.date)}</b>`, `Ожидаемый: ${rub(d.p50)}`, `Диапазон: ${rub(d.p10)} … ${rub(d.p90)}`, `Вероятность минуса: ${pct(d.prob_negative)}`];
        if (extra) l.push(`Сценарий: ${rub(extra.r.days[i].p50)}`);
        return l.join("<br/>");
      },
    },
    legend: { bottom: 0, textStyle: { color: COLORS.muted }, data: extra ? ["Ожидаемый остаток", extra.name, "Диапазон 10–90%"] : ["Ожидаемый остаток", "Диапазон 10–90%"] },
    xAxis: { type: "category", data: r.days.map((d) => d.date), ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: (v: string) => dateRu(v) } },
    yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: kRub } },
    series,
  };
}
