import { useEffect, useState } from "react";
import { api, rub, dateRu } from "./api";
import type { Ev } from "./types";

const SRC: Record<string, string> = { manual: "вручную", invoice: "счёт", contract: "договор", tax: "налоговый календарь" };

export function Events({ base, rev, onChange }: { base: string; rev: number; onChange: () => void }) {
  const [items, setItems] = useState<Ev[]>([]);
  const [error, setError] = useState("");
  const [f, setF] = useState({ date: "", amount: "", direction: "in", title: "", counterparty: "", source: "manual", confidence: 90 });
  const [tax, setTax] = useState("");

  useEffect(() => { api.get<{ items: Ev[] }>(`${base}/expected-events`).then((r) => setItems(r.items)); }, [base, rev]);

  async function add(e: React.FormEvent) {
    e.preventDefault(); setError("");
    try {
      const kop = Math.round(parseFloat(f.amount.replace(",", ".")) * 100);
      if (!Number.isFinite(kop) || kop <= 0) throw new Error("Введите положительную сумму");
      await api.post(`${base}/expected-events`, { date: f.date, amount_kop: String(kop), direction: f.direction, title: f.title, counterparty: f.counterparty, source: f.source, confidence: f.confidence / 100 });
      setF({ ...f, amount: "", title: "", counterparty: "" });
      onChange();
    } catch (err) { setError((err as Error).message); }
  }
  async function addTax(e: React.FormEvent) {
    e.preventDefault(); setError("");
    try {
      const kop = Math.round(parseFloat(tax.replace(",", ".")) * 100);
      if (!Number.isFinite(kop) || kop <= 0) throw new Error("Введите сумму налога");
      await api.post(`${base}/expected-events/tax-calendar`, { amount_kop: String(kop) });
      setTax(""); onChange();
    } catch (err) { setError((err as Error).message); }
  }
  return (
    <>
      <section className="card">
        <h3>Добавить ожидаемый платёж</h3>
        <p className="muted small">«Клиент обещал заплатить 300 000 ₽ 20-го», «в декабре куплю оборудование за 1 млн». Платёж сразу попадёт в прогноз и заменит похожий регулярный, чтобы не считаться дважды.</p>
        <form className="form" onSubmit={add}>
          <label>Дата<input type="date" required value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} /></label>
          <label>Тип<select value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value })}><option value="in">Поступление</option><option value="out">Платёж</option></select></label>
          <label>Сумма, ₽<input inputMode="decimal" required value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} /></label>
          <label>Описание<input required maxLength={200} value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></label>
          <label>Контрагент<input value={f.counterparty} onChange={(e) => setF({ ...f, counterparty: e.target.value })} /></label>
          <label>Источник<select value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })}>{Object.entries(SRC).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label>Уверенность, %<input type="number" min={0} max={100} value={f.confidence} onChange={(e) => setF({ ...f, confidence: Number(e.target.value) })} /></label>
          <button>Добавить</button>
        </form>
        <form className="form" onSubmit={addTax}>
          <label>Налоговый календарь: ЕНП до 28-го, сумма, ₽<input inputMode="decimal" value={tax} onChange={(e) => setTax(e.target.value)} /></label>
          <button className="ghost">Добавить 2 ближайших срока</button>
        </form>
        {error && <div className="alert high" role="alert">{error}</div>}
      </section>
      <section className="card">
        <h3>Ожидаемые события ({items.length})</h3>
        {items.length === 0 && <p className="muted">Пока нет.</p>}
        <table><tbody>{items.map((e) => (
          <tr key={e.id}><td>{dateRu(e.date)}</td><td>{e.title}<div className="small muted">{e.counterparty}</div></td><td><span className="chip">{SRC[e.source] ?? e.source}</span></td><td>{Math.round(e.confidence * 100)}%</td>
            <td className={"num " + (e.amount.startsWith("-") ? "neg" : "pos")}>{rub(e.amount, { sign: true })}</td>
            <td><button className="ghost" onClick={async () => { await api.del(`${base}/expected-events/${e.id}`); onChange(); }} aria-label="Удалить">✕</button></td></tr>
        ))}</tbody></table>
      </section>
    </>
  );
}
