import { useEffect, useRef, useState } from "react";
import { api, dateRu, pct, rub } from "../api";
import { useApp } from "../ctx";
import { Icon } from "../icons";
import { navigate } from "../router";
import type { Result } from "../types";
import { Card, Empty, Loading, Notice, PageHeader } from "../ui";

type Msg = { role: "user" | "bot"; text: string };
type ScenarioOut = { base: { result: Result }; scenario: { result: Result }; delta: { shortfall_probability: number; max_deficit_p90: string } };

const PRESETS = [
  "Почему возможен кассовый разрыв?",
  "Что будет, если крупный клиент задержит оплату на 2 недели?",
  "Какие платежи самые крупные в ближайшие 2 недели?",
  "Что проверить в первую очередь?",
  "Как снизить риск?",
];

const addDays = (iso: string, n: number) => new Date(new Date(iso + "T00:00:00").getTime() + n * 86400000).toISOString().slice(0, 10);
const LEVEL = { none: "разрыва не ожидается", low: "риск небольшой", medium: "риск заметный", high: "риск высокий" } as const;
const items = (xs: { date: string; title: string; amount: string; overdue?: boolean }[]) =>
  xs.map((x) => `• ${dateRu(x.date)} — ${x.title}: ${rub(x.amount)}${x.overdue ? " (просрочено)" : ""}`).join("\n");

function gapAnswer(r: Result) {
  const k = r.risk;
  if (k.level === "none") return `По сохранённому прогнозу кассовый разрыв не ожидается: вероятность ${pct(k.shortfall_probability)}, ожидаемый остаток в конце периода — ${rub(r.days[r.days.length - 1].p50)}.`;
  const when = k.possible_break_date ?? k.first_break_date;
  const w = k.warnings.find((x) => x.code === "cash_gap");
  return [
    `Вероятность кассового разрыва за ${r.horizon} дн. — ${pct(k.shortfall_probability)} (${LEVEL[k.level]}).`,
    when ? `Вероятная дата — ${dateRu(when)}; типичный дефицит ${rub(k.deficit_given_break_median)}, в плохом случае (90-й перцентиль) ${rub(k.max_deficit_p90)}.` : "",
    w?.reasons.length ? `Основные платежи до этой даты:\n${items(w.reasons)}` : "",
    `Остаток на ${dateRu(r.as_of)}: ${rub(r.opening_balance)}.`,
  ].filter(Boolean).join("\n");
}

function biggestAnswer(r: Result) {
  const end = addDays(r.as_of, 14);
  const out = r.planned.filter((p) => BigInt(p.amount) < 0n && p.date <= end).sort((a, b) => (BigInt(a.amount) < BigInt(b.amount) ? -1 : 1)).slice(0, 5);
  if (!out.length) return `В ближайшие 14 дней после ${dateRu(r.as_of)} запланированных списаний в прогнозе нет.`;
  const total = out.reduce((s, p) => s + BigInt(p.amount), 0n);
  return `Самые крупные списания до ${dateRu(end)}:\n${items(out)}\nИтого по этим платежам: ${rub(total.toString())}.`;
}

function checkAnswer(r: Result) {
  const lines: string[] = [];
  r.risk.warnings.filter((w) => w.code.startsWith("overdue")).forEach((w) => lines.push(`• ${w.message}`));
  const first = r.planned.filter((p) => BigInt(p.amount) < 0n)[0];
  if (first) lines.push(`• Ближайшее списание: ${dateRu(first.date)} — ${first.title}, ${rub(first.amount)}. Убедитесь, что денег на эту дату достаточно.`);
  const inc = r.planned.filter((p) => BigInt(p.amount) > 0n)[0];
  if (inc) lines.push(`• Ближайшее ожидаемое поступление: ${dateRu(inc.date)} — ${inc.title}, ${rub(inc.amount)}. Подтвердите срок оплаты с контрагентом.`);
  r.quality.notes.forEach((n) => lines.push(`• Качество данных: ${n}`));
  if (r.risk.level !== "none") lines.push(`• Риск разрыва ${pct(r.risk.shortfall_probability)}: проверьте возможность перенести платежи или ускорить поступления.`);
  return lines.length ? `Что проверить в первую очередь:\n${lines.join("\n")}` : "Серьёзных поводов для проверки в сохранённом прогнозе нет.";
}

function reduceAnswer(r: Result) {
  const when = r.risk.possible_break_date ?? r.risk.first_break_date;
  const upto = when ?? addDays(r.as_of, r.horizon);
  const out = r.planned.filter((p) => BigInt(p.amount) < 0n && p.date <= upto).sort((a, b) => (BigInt(a.amount) < BigInt(b.amount) ? -1 : 1)).slice(0, 3);
  const inc = r.planned.filter((p) => BigInt(p.amount) > 0n && p.date <= upto).slice(0, 3);
  if (r.risk.level === "none") return `Риск разрыва по сохранённому прогнозу невысокий (${pct(r.risk.shortfall_probability)}). Для запаса проверьте, как изменится картина в сценариях «что если».`;
  return [
    `Риск ${pct(r.risk.shortfall_probability)}${when ? `, вероятная дата ${dateRu(when)}` : ""}. Варианты, которые стоит проверить в сценариях:`,
    out.length ? `• Перенести или разбить крупные платежи до этой даты:\n${items(out)}` : "",
    inc.length ? `• Ускорить ожидаемые поступления:\n${items(inc)}` : "",
    `• Подготовить источник покрытия дефицита до ${rub(r.risk.max_deficit_p90)} (плохой случай).`,
  ].filter(Boolean).join("\n");
}

function taxAnswer(r: Result) {
  const tx = r.planned.filter((p) => /фнс|налог|енп|взнос/i.test(p.title) || p.source === "tax");
  const rule = r.rules.find((x) => x.category === "taxes");
  if (tx.length) return `Налоговые платежи в прогнозе:\n${items(tx)}`;
  return rule ? `Регулярный налоговый платёж найден (${rule.counterparty || rule.key}, около ${rub(rule.amount)}), но в ближайший период он не попадает.` : "Налоговых платежей в сохранённом прогнозе нет. Их можно добавить в разделе «Платежи и обязательства».";
}

function expenseAnswer(r: Result) {
  const rules = r.rules.filter((x) => BigInt(x.amount) < 0n).slice(0, 5);
  if (!rules.length) return "Регулярных расходов в истории не найдено.";
  return `Крупнейшие регулярные расходы:\n${rules.map((x) => `• ${x.counterparty || x.key}: ${rub(x.amount)}, ${x.kind === "monthly" ? "ежемесячно" : "еженедельно"}`).join("\n")}`;
}

function scenarioAnswer(label: string, o: ScenarioOut) {
  const b = o.base.result.risk, s = o.scenario.result.risk;
  const when = s.possible_break_date ?? s.first_break_date;
  return [
    `Сценарий: ${label}.`,
    `Вероятность разрыва: ${pct(b.shortfall_probability)} → ${pct(s.shortfall_probability)}.`,
    when ? `Вероятная дата разрыва в сценарии — ${dateRu(when)}.` : "В сценарии разрыв остаётся маловероятным.",
    `Дефицит в плохом случае: ${rub(b.max_deficit_p90)} → ${rub(s.max_deficit_p90)}.`,
  ].join("\n");
}

export default function Assistant() {
  const { run, runState, base } = useApp();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [msgs, busy]);

  if (runState === "loading") return <Loading />;
  if (!run) {
    return (
      <>
        <PageHeader title="AI-консультант" />
        <Card><Empty title="Нет прогноза для анализа" text="Консультант отвечает по сохранённому прогнозу. Сначала загрузите выписку и постройте прогноз." action={<button className="btn" onClick={() => navigate("/settings")}>Перейти к импорту</button>} /></Card>
      </>
    );
  }
  const r = run.result;
  const src = `Источник: прогноз от ${dateRu(r.as_of)}, расчёт ${run.run_id.slice(0, 8)}. Ответ построен по сохранённому прогнозу; числа не придуманы.`;

  async function scenario(label: string, sc: Record<string, number>) {
    const o = await api.post<ScenarioOut>(`${base}/scenarios`, { horizon_days: r.horizon, scenario: sc });
    return scenarioAnswer(label, o);
  }

  async function answer(q: string): Promise<string> {
    const s = q.toLowerCase();
    const num = Number((s.match(/(\d+)\s*(дн|нед)/) ?? [])[1] ?? 0);
    const weeks = /нед/.test(s.match(/\d+\s*(дн|нед)/)?.[0] ?? "") ? num * 7 : num;
    if (/клиент|задерж|позже|отсроч/.test(s)) {
      const days = weeks || (/две недел|2 недел/.test(s) ? 14 : 14);
      return scenario(`крупный клиент платит на ${days} дн. позже`, { delay_days: Math.min(days, 90) });
    }
    if (/выручк|падени|снижени.*продаж/.test(s)) return scenario("выручка −20%", { revenue_drop_pct: 20 });
    if (/налог|енп|взнос/.test(s)) return taxAnswer(r);
    if (/снизить|уменьш|что делать|как избеж/.test(s)) return reduceAnswer(r);
    if (/провер|в первую очередь|приоритет/.test(s)) return checkAnswer(r);
    if (/расход/.test(s) && !/крупн/.test(s)) return expenseAnswer(r);
    if (/платеж|платёж|крупн|ближайш|списан/.test(s)) return biggestAnswer(r);
    if (/разрыв|почему|риск|дефицит|хватит/.test(s)) return gapAnswer(r);
    return `Я отвечаю по сохранённому прогнозу. Спросите, например: «Почему возможен кассовый разрыв?», «Что будет, если клиент задержит оплату на 3 недели?», «Какие платежи самые крупные?», «Что проверить в первую очередь?», «Как снизить риск?».`;
  }

  async function ask(q: string) {
    const t = q.trim();
    if (!t || busy) return;
    setMsgs((m) => [...m, { role: "user", text: t }]);
    setText("");
    setBusy(true);
    try {
      const a = await answer(t);
      setMsgs((m) => [...m, { role: "bot", text: `${a}\n\n${src}` }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "bot", text: `Не удалось выполнить расчёт: ${(e as Error).message}` }]);
    } finally { setBusy(false); }
  }

  return (
    <div className="stack">
      <PageHeader title="AI-консультант" subtitle="Объясняет причины риска и помогает проверить сценарии" actions={<button className="btn secondary" onClick={() => navigate("/scenarios")}>Создать сценарий</button>} />
      <Notice tone="neutral">Демо-режим: ответы формируются правилами по сохранённому прогнозу; подключение LLM-провайдера — отдельный модуль. Внешние сервисы не вызываются.</Notice>
      <Card title="Быстрые вопросы">
        <div className="chips">{PRESETS.map((p) => <button key={p} className="chip" disabled={busy} onClick={() => ask(p)}>{p}</button>)}</div>
      </Card>
      <Card title="Диалог">
        <div className="chat" aria-live="polite">
          {msgs.length === 0 && <p className="muted">Выберите вопрос выше или задайте свой.</p>}
          {msgs.map((m, i) => <div key={i} className={m.role === "user" ? "msg user" : "msg"}>{m.text}</div>)}
          {busy && <div className="msg muted">Считаю…</div>}
          <div ref={end} />
        </div>
        <form className="toolbar" style={{ marginTop: 14, flexWrap: "nowrap" }} onSubmit={(e) => { e.preventDefault(); ask(text); }}>
          <input style={{ flex: 1, width: "100%" }} value={text} onChange={(e) => setText(e.target.value)} placeholder="Задайте вопрос о прогнозе" aria-label="Вопрос консультанту" maxLength={300} />
          <button className="btn" disabled={busy || !text.trim()} aria-label="Отправить"><Icon name="send" size={16} />Отправить</button>
        </form>
      </Card>
    </div>
  );
}
