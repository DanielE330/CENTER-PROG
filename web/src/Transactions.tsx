import { useEffect, useState } from "react";
import { api, rub, CATEGORY_RU } from "./api";
import type { Tx } from "./types";

export function Transactions({ base, rev, onChange }: { base: string; rev: number; onChange: () => void }) {
  const [items, setItems] = useState<Tx[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [cat, setCat] = useState("");
  const limit = 50;

  useEffect(() => {
    api.get<{ items: Tx[]; total: number }>(`${base}/transactions?limit=${limit}&offset=${offset}&category=${cat}`).then((r) => { setItems(r.items); setTotal(r.total); });
  }, [base, rev, offset, cat]);

  async function setCategory(id: number, category: string) {
    await api.patch(`${base}/transactions/${id}/category`, { category });
    onChange();
  }
  return (
    <section className="card">
      <div className="row"><h3>Операции ({total})</h3><div className="grow" />
        <select value={cat} onChange={(e) => { setOffset(0); setCat(e.target.value); }} aria-label="Категория">
          <option value="">Все категории</option>
          {Object.entries(CATEGORY_RU).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      <table>
        <thead><tr><th>Дата</th><th>Контрагент / назначение</th><th>Категория</th><th className="num">Сумма</th></tr></thead>
        <tbody>{items.map((t) => (
          <tr key={t.id}>
            <td>{t.date}</td>
            <td><div>{t.counterparty}</div><div className="small muted">{t.purpose}</div></td>
            <td>
              <select value={t.category} onChange={(e) => setCategory(t.id, e.target.value)} aria-label="Категория операции">
                {Object.entries(CATEGORY_RU).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              {!t.manual && t.category_confidence < 0.5 && <span className="chip warn" title="Низкая уверенность — проверьте">?</span>}
            </td>
            <td className={"num " + (t.amount_kop.startsWith("-") ? "neg" : "pos")}>{rub(t.amount_kop, { sign: true })}</td>
          </tr>
        ))}</tbody>
      </table>
      <div className="row">
        <button className="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>← Назад</button>
        <span className="small muted">{total ? `${offset + 1}–${Math.min(offset + limit, total)} из ${total}` : ""}</span>
        <button className="ghost" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>Вперёд →</button>
      </div>
    </section>
  );
}
