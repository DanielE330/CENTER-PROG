import { useEffect, useMemo, useState } from "react";
import { api, dateRu, rub } from "../api";
import { useApp } from "../ctx";
import { Icon } from "../icons";
import { Badge, Card, Empty, Field, Kpi, Loading, Notice, PageHeader, type Tone } from "../ui";
import type { Ev } from "../types";

type St = "planned" | "expected" | "done" | "overdue";
type Row = { key: string; date: string; title: string; amount: string; source: string; status: St; fact?: string | null; factDate?: string | null; diff?: string | null };
type PfItem = { date: string; title: string; source: string; plan: string; fact: string | null; fact_date: string | null; diff: string | null; status: string };
type Pf = { as_of: string; items: PfItem[] };

const ST: Record<St, { label: string; tone: Tone }> = {
  planned: { label: "Запланировано", tone: "neutral" },
  expected: { label: "Ожидается", tone: "warn" },
  done: { label: "Исполнено", tone: "ok" },
  overdue: { label: "Просрочено", tone: "risk" },
};
const SRC: Record<string, string> = { recurring: "Регулярный", tax: "Налоговый календарь", manual: "Вручную", invoice: "Счёт", contract: "Договор", event: "Ожидаемое событие", scenario: "Сценарий" };
const FORM_SRC = ["manual", "invoice", "contract", "tax"] as const;

/** "12 345,67" -> "1234567" копеек, без float-арифметики. */
function parseKop(s: string): string | null {
  const t = s.replace(/[\s ]/g, "").replace(",", ".");
  const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(t);
  if (!m) return null;
  const kop = BigInt(m[1]) * 100n + BigInt((m[2] ?? "").padEnd(2, "0") || "0");
  return kop > 0n ? kop.toString() : null;
}
const addDays = (iso: string, n: number) => { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const isOut = (a: string) => a.startsWith("-");

export default function Obligations() {
  const { base, rev, run, runState, refresh, openImport } = useApp();
  const [pf, setPf] = useState<Pf | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [fSt, setFSt] = useState<"" | St>("");
  const [fDir, setFDir] = useState<"" | "in" | "out">("");
  const [modal, setModal] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      api.get<Pf>(`${base}/plan-fact?days=90`).catch(() => null),
      api.get<{ items: Ev[] }>(`${base}/expected-events`),
    ])
      .then(([p, e]) => { if (alive) { setPf(p); setEvents(e.items); setError(""); } })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [base, rev]);

  const asOf = run?.result.as_of ?? pf?.as_of ?? null;

  const { upcoming, history } = useMemo(() => {
    const up: Row[] = [];
    const hist: Row[] = [];
    if (run && asOf) {
      run.result.planned.forEach((p, i) => {
        const status: St = p.overdue ? "overdue" : p.date <= addDays(asOf, 3) ? "expected" : "planned";
        up.push({ key: `u${i}`, date: p.date, title: p.title, amount: p.amount, source: p.source, status });
      });
    }
    pf?.items.forEach((p, i) => {
      const status: St = p.status === "paid" || p.status === "deviation" ? "done" : p.status === "missed" ? "overdue" : "expected";
      hist.push({ key: `h${i}`, date: p.date, title: p.title, amount: p.plan, source: p.source, status, fact: p.fact, factDate: p.fact_date, diff: p.status === "deviation" ? p.diff : null });
    });
    up.sort((a, b) => a.date.localeCompare(b.date));
    hist.sort((a, b) => b.date.localeCompare(a.date));
    return { upcoming: up, history: hist };
  }, [run, pf, asOf]);

  const all = [...upcoming, ...history.filter((h) => h.status === "expected")];
  const week = asOf ? all.filter((r) => r.date <= addDays(asOf, 7) && r.status !== "done") : [];
  const outSum = week.filter((r) => isOut(r.amount)).reduce((s, r) => s + -BigInt(r.amount), 0n);
  const inSum = week.filter((r) => !isOut(r.amount)).reduce((s, r) => s + BigInt(r.amount), 0n);
  const overdueN = [...upcoming, ...history].filter((r) => r.status === "overdue").length;
  const soon = upcoming.filter((r) => r.status !== "overdue").slice(0, 5);

  const pass = (r: Row) => (!fSt || r.status === fSt) && (!fDir || (fDir === "out") === isOut(r.amount));
  const up = upcoming.filter(pass);
  const hi = history.filter(pass);

  async function del(id: string) {
    try { await api.del(`${base}/expected-events/${id}`); refresh(); } catch (e) { setError((e as Error).message); }
  }

  const table = (rows: Row[], withFact: boolean) => (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Дата</th><th>Платёж</th><th>Источник</th><th>Статус</th>{withFact && <th className="num">Факт</th>}<th className="num">Сумма</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={r.status === "overdue" ? "row-risk" : ""}>
              <td className="money" style={{ whiteSpace: "nowrap" }}>{dateRu(r.date)}</td>
              <td>{r.title}<div className="small muted">{isOut(r.amount) ? "Платёж" : "Поступление"}</div></td>
              <td><Badge>{SRC[r.source] ?? r.source}</Badge></td>
              <td><Badge tone={ST[r.status].tone}>{ST[r.status].label}</Badge></td>
              {withFact && (
                <td className="num money">
                  {r.fact ? <>{rub(r.fact, { sign: true })}<div className="small muted">{r.factDate ? dateRu(r.factDate) : ""}{r.diff ? ` · откл. ${rub(r.diff, { sign: true })}` : ""}</div></> : "—"}
                </td>
              )}
              <td className={"num money " + (isOut(r.amount) ? "neg" : "pos")}>{rub(r.amount, { sign: true })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  if (loading && !pf && events.length === 0) return <Loading />;
  const noData = runState === "empty" && !pf;

  return (
    <div className="stack">
      <PageHeader
        title="Платежи и обязательства"
        subtitle={asOf ? `Данные на ${dateRu(asOf)}` : undefined}
        actions={<button className="btn" onClick={() => setModal(true)}><Icon name="plus" size={16} />Добавить платёж</button>}
      />
      {error && <Notice tone="risk">{error}</Notice>}
      {noData ? (
        <Card><Empty title="Нет данных" text="Загрузите выписку: сервис найдёт регулярные платежи и составит график обязательств." action={<button className="btn" onClick={openImport}>Загрузить выписку</button>} /></Card>
      ) : (
        <>
          <div className="grid kpis">
            <Kpi label="Платежи за 7 дней" value={rub((-outSum).toString())} tone={outSum > 0n ? "warn" : "neutral"} hint="запланированные списания" />
            <Kpi label="Поступления за 7 дней" value={rub(inSum.toString())} tone={inSum > 0n ? "ok" : "neutral"} hint="ожидаемые зачисления" />
            <Kpi label="Просрочено" value={String(overdueN)} tone={overdueN > 0 ? "risk" : "ok"} hint={overdueN > 0 ? "требуют внимания" : "всё в срок"} />
            <Kpi label="Ожидаемых событий" value={String(events.length)} hint="введено вручную" />
          </div>

          <div className="grid main-side">
            <div className="stack">
              <Card
                title="Фильтры"
                actions={null}
              >
                <div className="toolbar">
                  <div className="chips" role="group" aria-label="Статус">
                    <button className={fSt === "" ? "chip on" : "chip"} aria-pressed={fSt === ""} onClick={() => setFSt("")}>Все статусы</button>
                    {(Object.keys(ST) as St[]).map((s) => <button key={s} className={fSt === s ? "chip on" : "chip"} aria-pressed={fSt === s} onClick={() => setFSt(s)}>{ST[s].label}</button>)}
                  </div>
                  <div className="chips" role="group" aria-label="Направление">
                    {([["", "Все"], ["in", "Поступления"], ["out", "Платежи"]] as const).map(([v, l]) => <button key={v} className={fDir === v ? "chip on" : "chip"} aria-pressed={fDir === v} onClick={() => setFDir(v)}>{l}</button>)}
                  </div>
                </div>
              </Card>
              <Card title={`Предстоящие (${up.length})`} pad={false}>
                {up.length === 0 ? <Empty title="Нет предстоящих платежей" text="Измените фильтры или добавьте платёж вручную." /> : table(up, false)}
              </Card>
              <Card title={`История за 90 дней (${hi.length})`} pad={false}>
                {hi.length === 0 ? <Empty title="История пуста" /> : table(hi, true)}
              </Card>
            </div>

            <div className="stack">
              <Card title="Ближайшие">
                {soon.length === 0 ? <p className="muted">Ближайших платежей нет.</p> : (
                  <div className="stack" style={{ gap: 12 }}>
                    {soon.map((r) => (
                      <div key={r.key} className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
                        <div>
                          <div style={{ fontWeight: 600 }}>{r.title}</div>
                          <div className="small muted">{dateRu(r.date)} · <Badge tone={ST[r.status].tone}>{ST[r.status].label}</Badge></div>
                        </div>
                        <div className={"money " + (isOut(r.amount) ? "neg" : "pos")} style={{ fontWeight: 600, whiteSpace: "nowrap" }}>{rub(r.amount, { sign: true })}</div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
              <Card title="Введено вручную" pad={false}>
                {events.length === 0 ? <div className="card-body muted">Пока нет. Добавьте обещанный платёж клиента или плановую закупку — они сразу попадут в прогноз.</div> : (
                  <div className="table-wrap">
                    <table>
                      <tbody>
                        {events.map((e) => (
                          <tr key={e.id}>
                            <td>
                              <div>{e.title}</div>
                              <div className="small muted">{dateRu(e.date)} · {SRC[e.source] ?? e.source} · {Math.round(e.confidence * 100)}%</div>
                            </td>
                            <td className={"num money " + (isOut(e.amount) ? "neg" : "pos")}>{rub(e.amount, { sign: true })}</td>
                            <td><button className="icon-btn" aria-label={`Удалить: ${e.title}`} onClick={() => del(e.id)}><Icon name="trash" size={16} /></button></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>
          </div>
        </>
      )}
      {modal && <AddModal base={base} onClose={() => setModal(false)} onDone={() => { setModal(false); refresh(); }} />}
    </div>
  );
}

function AddModal({ base, onClose, onDone }: { base: string; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ date: "", amount: "", direction: "in", title: "", counterparty: "", source: "manual", confidence: 90 });
  const [tax, setTax] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f, v: string | number) => setF({ ...f, [k]: v });

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const kop = parseKop(f.amount);
    if (!kop) return setError("Введите сумму больше нуля, например 300 000 или 1500,50");
    setBusy(true);
    try {
      await api.post(`${base}/expected-events`, { date: f.date, amount_kop: kop, direction: f.direction, title: f.title.trim(), counterparty: f.counterparty.trim(), source: f.source, confidence: f.confidence / 100 });
      onDone();
    } catch (err) { setError((err as Error).message); setBusy(false); }
  }
  async function addTax(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const kop = tax.trim() ? parseKop(tax) : null;
    if (tax.trim() && !kop) return setError("Введите сумму налога, например 250 000");
    setBusy(true);
    try { await api.post(`${base}/expected-events/tax-calendar`, kop ? { amount_kop: kop } : {}); onDone(); }
    catch (err) { setError((err as Error).message); setBusy(false); }
  }
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Добавить платёж">
      <div className="card modal-body stack">
        <div className="row"><h2>Добавить платёж или поступление</h2><div className="grow" /><button className="icon-btn" aria-label="Закрыть" onClick={onClose}><Icon name="close" /></button></div>
        <p className="muted small">Например: «клиент обещал заплатить 300 000 ₽ 20-го» или «в декабре куплю оборудование за 1 000 000 ₽». Платёж сразу попадёт в прогноз и заменит похожий регулярный, чтобы не считаться дважды.</p>
        <form className="stack" onSubmit={add}>
          <div className="form-grid">
            <Field label="Дата"><input type="date" required value={f.date} onChange={(e) => set("date", e.target.value)} /></Field>
            <Field label="Тип">
              <select value={f.direction} onChange={(e) => set("direction", e.target.value)}><option value="in">Поступление</option><option value="out">Платёж</option></select>
            </Field>
            <Field label="Сумма, ₽"><input inputMode="decimal" required placeholder="300 000" value={f.amount} onChange={(e) => set("amount", e.target.value)} /></Field>
            <Field label="Описание"><input required maxLength={200} value={f.title} onChange={(e) => set("title", e.target.value)} /></Field>
            <Field label="Контрагент"><input value={f.counterparty} onChange={(e) => set("counterparty", e.target.value)} /></Field>
            <Field label="Источник">
              <select value={f.source} onChange={(e) => set("source", e.target.value)}>{FORM_SRC.map((s) => <option key={s} value={s}>{SRC[s]}</option>)}</select>
            </Field>
            <Field label="Уверенность, %" hint="Вероятность, что платёж состоится"><input type="number" min={0} max={100} value={f.confidence} onChange={(e) => set("confidence", Number(e.target.value))} /></Field>
          </div>
          <div className="row"><button className="btn" disabled={busy}>Добавить</button><button type="button" className="btn ghost" onClick={onClose}>Отмена</button></div>
        </form>
        <hr style={{ border: 0, borderTop: "1px solid var(--line)", width: "100%" }} />
        <form className="form-grid" onSubmit={addTax}>
          <Field label="Налоговый календарь ФНС: ЕНП, сумма, ₽" hint="Сроки берутся с nalog.gov.ru. Без суммы возьмём типичный платёж из вашей выписки"><input inputMode="decimal" value={tax} onChange={(e) => setTax(e.target.value)} /></Field>
          <div><button className="btn secondary" disabled={busy}>Подтянуть сроки ЕНП</button></div>
        </form>
        {error && <Notice tone="risk">{error}</Notice>}
      </div>
    </div>
  );
}
