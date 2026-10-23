import { useMemo } from "react";
import { dateRu, pct, rub } from "../api";
import Chart from "../Chart";
import { forecastOption } from "../charts";
import { useApp } from "../ctx";
import { navigate } from "../router";
import { Badge, Card, Empty, Kpi, Loading, Notice, PageHeader, type Tone } from "../ui";
import "./echartsExtra";
import "./Overview.css";

const LEVEL: Record<string, { text: string; tone: Tone }> = {
  none: { text: "Разрыва не ожидается", tone: "ok" },
  low: { text: "Низкий риск", tone: "warn" },
  medium: { text: "Заметный риск", tone: "warn" },
  high: { text: "Высокий риск разрыва", tone: "risk" },
};
const SRC: Record<string, string> = { recurring: "регулярный", manual: "вручную", invoice: "счёт", contract: "договор", tax: "налог", scenario: "сценарий" };

export default function Overview() {
  const { run, runState, error, recalc, recalcBusy, openImport } = useApp();
  const r = run?.result;
  const option = useMemo(() => (r ? forecastOption(r) : {}), [r]);

  if (runState === "loading") return <><PageHeader title="Обзор" /><Loading /></>;
  if (runState === "error") return <><PageHeader title="Обзор" /><Notice tone="risk">{error || "Не удалось загрузить прогноз"}</Notice></>;
  if (runState === "empty" || !r || !run)
    return (
      <>
        <PageHeader title="Обзор" subtitle="Ликвидность компании на ближайшие недели" />
        <Card>
          <Empty
            title="Прогноза пока нет"
            text="Загрузите банковскую выписку — сервис найдёт регулярные платежи и покажет остаток по дням."
            action={<div className="row"><button className="btn" onClick={openImport}>Загрузить выписку</button><button className="btn secondary" disabled={recalcBusy} onClick={() => recalc().catch(() => {})}>Построить по загруженным данным</button></div>}
          />
        </Card>
      </>
    );

  const risk = r.risk;
  const lvl = LEVEL[risk.level];
  const minDay = r.days.reduce((m, d) => (BigInt(d.p50) < BigInt(m.p50) ? d : m), r.days[0]);
  const minNeg = BigInt(minDay.p50) < 0n;
  const planned = r.planned.filter((p) => BigInt(p.amount) > 0n).reduce((s, p) => s + BigInt(p.amount), 0n);
  const gap = risk.possible_break_date ?? risk.first_break_date;

  return (
    <div className="stack">
      <PageHeader
        title="Обзор"
        subtitle={<>Прогноз на {r.horizon} дн. · данные актуальны на {dateRu(r.as_of)}</>}
        actions={<button className="btn secondary" disabled={recalcBusy} onClick={() => recalc(r.horizon).catch(() => {})}>{recalcBusy ? "Считаю…" : "Пересчитать"}</button>}
      />
      {run.stale && (
        <Notice tone="warn" action={<button className="btn sm" disabled={recalcBusy} onClick={() => recalc(r.horizon).catch(() => {})}>Пересчитать</button>}>
          Данные изменились после последнего расчёта — прогноз устарел.
        </Notice>
      )}

      <div className="grid kpis">
        <Kpi label="Текущий остаток" value={rub(r.opening_balance)} hint={`на ${dateRu(r.as_of)}`} />
        <Kpi label="Минимальный прогнозный остаток" value={rub(minDay.p50)} tone={minNeg ? "risk" : "ok"} hint={`ожидается ${dateRu(minDay.date)}${minNeg ? " · дефицит" : ""}`} />
        <Kpi label="Плановые поступления" value={rub(planned.toString())} hint={`по известным платежам за ${r.horizon} дн.`} />
        <Kpi label="Риск кассового разрыва" value={pct(risk.shortfall_probability)} tone={lvl.tone} hint={<Badge tone={lvl.tone}>{lvl.text}</Badge>} />
      </div>

      <Card className="hero">
        <p className="hero-text">
          {gap ? (
            <>С вероятностью <b>{pct(risk.shortfall_probability)}</b> денег не хватит. Вероятная дата — <b>{dateRu(gap)}</b>, типичный дефицит — <b>{rub(risk.deficit_given_break_median)}</b>, в плохом случае (90-й перцентиль) — <b>{rub(risk.max_deficit_p90)}</b>.</>
          ) : (
            <>В {pct(1 - risk.shortfall_probability)} сценариев остаток остаётся положительным весь период. Минимум — {rub(minDay.p50)} ({dateRu(minDay.date)}).</>
          )}
        </p>
      </Card>

      <Card title="Прогноз остатка" actions={<button className="btn ghost sm" onClick={() => navigate("/forecast")}>Подробнее</button>}>
        <Chart option={option} height={340} />
        <p className="small muted">Линия — ожидаемый остаток, полоса — диапазон 10–90 % сценариев, красная область — период возможного дефицита. Это модельная оценка.</p>
      </Card>

      <div className="grid main-side">
        <Card title="Ближайшие платежи и поступления" actions={<button className="btn ghost sm" onClick={() => navigate("/obligations")}>Все платежи</button>}>
          {r.planned.length === 0 ? <p className="muted">Плановых платежей нет.</p> : (
            <div className="plist">
              {r.planned.slice(0, 8).map((p, i) => {
                const neg = BigInt(p.amount) < 0n;
                return (
                  <div className="plist-row" key={i}>
                    <span className="plist-date">{dateRu(p.date)}</span>
                    <span className="plist-title">{p.title}<Badge>{SRC[p.source] ?? p.source}</Badge>{p.overdue && <Badge tone="risk">просрочено</Badge>}</span>
                    <span className={"money num " + (neg ? "neg" : "pos")}>{rub(p.amount, { sign: true })}</span>
                  </div>
                );
              })}
            </div>
          )}
        </Card>
        <Card title="Предупреждения">
          {risk.warnings.length === 0 && r.quality.notes.length === 0 && <Notice tone="ok">Предупреждений нет.</Notice>}
          {risk.warnings.map((w, i) => (
            <div className="warn-item" key={i}>
              <div className="row"><Badge tone={w.severity === "high" ? "risk" : "warn"}>{w.severity === "high" ? "Критично" : "Внимание"}</Badge>{w.date && <span className="small muted">{dateRu(w.date)}</span>}</div>
              <div>{w.message}</div>
              {w.reasons.length > 0 && <ul>{w.reasons.map((x, j) => <li key={j}>{dateRu(x.date)} — {x.title}: {rub(x.amount)}{x.overdue ? " (просрочено)" : ""}</li>)}</ul>}
            </div>
          ))}
          {r.quality.notes.map((n, i) => <div className="warn-item" key={"q" + i}><Badge>О данных</Badge><div className="small">{n}</div></div>)}
          <div className="fresh">Данные актуальны на {dateRu(r.as_of)} · качество данных: {r.quality.level} ({r.quality.history_days} дн. истории)</div>
        </Card>
      </div>
    </div>
  );
}
