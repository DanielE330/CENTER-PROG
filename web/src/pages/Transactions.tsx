import { useEffect, useRef, useState } from "react";
import { api, CATEGORY_RU, dateRu, rub } from "../api";
import { useApp } from "../ctx";
import { Badge, Card, Empty, Loading, Notice, PageHeader } from "../ui";
import type { Tx } from "../types";

type SortKey = "date" | "amount" | "counterparty" | "category";
type Dir = "" | "in" | "out";
const LIMIT = 50;

function status(t: Tx) {
  if (t.manual) return <Badge tone="ok">Подтверждена</Badge>;
  if (t.category_confidence < 0.5) return <Badge tone="warn">Требует проверки</Badge>;
  return <Badge>Автокатегория</Badge>;
}

export default function Transactions() {
  const { base, rev, refresh, openImport } = useApp();
  const [items, setItems] = useState<Tx[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [cat, setCat] = useState("");
  const [dir, setDir] = useState<Dir>("");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("date");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const timer = useRef<number>();

  useEffect(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { setOffset(0); setQ(qInput.trim()); }, 300);
    return () => window.clearTimeout(timer.current);
  }, [qInput]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const p = new URLSearchParams({ limit: String(LIMIT), offset: String(offset), category: cat, direction: dir, q, sort, order });
    api.get<{ items: Tx[]; total: number }>(`${base}/transactions?${p}`)
      .then((r) => { if (alive) { setItems(r.items); setTotal(r.total); setError(""); } })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [base, rev, offset, cat, dir, q, sort, order]);

  function toggleSort(k: SortKey) {
    setOffset(0);
    if (sort === k) setOrder(order === "asc" ? "desc" : "asc");
    else { setSort(k); setOrder(k === "counterparty" || k === "category" ? "asc" : "desc"); }
  }
  async function setCategory(id: number, category: string) {
    try { await api.patch(`${base}/transactions/${id}/category`, { category }); refresh(); }
    catch (e) { setError((e as Error).message); }
  }
  const th = (k: SortKey, label: string, cls = "") => (
    <th className={`sortable ${cls}`} aria-sort={sort === k ? (order === "asc" ? "ascending" : "descending") : "none"} onClick={() => toggleSort(k)}>
      {label}{sort === k ? (order === "asc" ? " ↑" : " ↓") : ""}
    </th>
  );
  const filtered = cat !== "" || dir !== "" || q !== "";
  const catKeys = Object.keys(CATEGORY_RU);

  return (
    <div className="stack">
      <PageHeader title="Операции" subtitle={`${total.toLocaleString("ru-RU")} операций`} actions={<button className="btn secondary" onClick={openImport}>Загрузить выписку</button>} />
      <Card pad={false}>
        <div className="card-body toolbar">
          <input type="search" placeholder="Поиск по контрагенту или назначению" aria-label="Поиск" value={qInput} onChange={(e) => setQInput(e.target.value)} style={{ minWidth: 260 }} />
          <div className="chips" role="group" aria-label="Направление">
            {([["", "Все"], ["in", "Поступления"], ["out", "Списания"]] as const).map(([v, l]) => (
              <button key={v} className={dir === v ? "chip on" : "chip"} aria-pressed={dir === v} onClick={() => { setOffset(0); setDir(v); }}>{l}</button>
            ))}
          </div>
          <select aria-label="Категория" value={cat} onChange={(e) => { setOffset(0); setCat(e.target.value); }}>
            <option value="">Все категории</option>
            {catKeys.map((k) => <option key={k} value={k}>{CATEGORY_RU[k]}</option>)}
          </select>
          {filtered && <button className="btn ghost sm" onClick={() => { setQInput(""); setQ(""); setCat(""); setDir(""); setOffset(0); }}>Сбросить</button>}
        </div>
        {error && <div className="card-body"><Notice tone="risk">{error}</Notice></div>}
        {loading && items.length === 0 ? <Loading /> : items.length === 0 ? (
          <Empty
            title={filtered ? "Ничего не найдено" : "Операций пока нет"}
            text={filtered ? "Измените или сбросьте фильтры." : "Загрузите банковскую выписку, чтобы увидеть операции."}
            action={!filtered && <button className="btn" onClick={openImport}>Загрузить выписку</button>}
          />
        ) : (
          <div className="table-wrap" style={{ opacity: loading ? 0.6 : 1 }}>
            <table>
              <thead>
                <tr>
                  {th("date", "Дата")}
                  {th("counterparty", "Контрагент / назначение")}
                  <th>Направление</th>
                  {th("category", "Категория")}
                  <th>Статус</th>
                  {th("amount", "Сумма", "num")}
                </tr>
              </thead>
              <tbody>
                {items.map((t) => {
                  const out = t.amount_kop.startsWith("-");
                  return (
                    <tr key={t.id}>
                      <td className="money" style={{ whiteSpace: "nowrap" }}>{dateRu(t.date)}</td>
                      <td>
                        <div>{t.counterparty || "—"}</div>
                        <div className="small muted" style={{ maxWidth: 420 }}>{t.purpose}</div>
                      </td>
                      <td><Badge tone={out ? "neutral" : "ok"}>{out ? "Списание" : "Поступление"}</Badge></td>
                      <td>
                        <select aria-label="Категория операции" value={t.category} onChange={(e) => setCategory(t.id, e.target.value)} style={{ minWidth: 170 }}>
                          {!(t.category in CATEGORY_RU) && <option value={t.category}>{t.category}</option>}
                          {catKeys.map((k) => <option key={k} value={k}>{CATEGORY_RU[k]}</option>)}
                        </select>
                      </td>
                      <td>{status(t)}</td>
                      <td className={"num money " + (out ? "neg" : "pos")}>{rub(t.amount_kop, { sign: true })}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {total > LIMIT && (
          <div className="card-body row">
            <button className="btn secondary sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>← Назад</button>
            <span className="small muted">{offset + 1}–{Math.min(offset + LIMIT, total)} из {total.toLocaleString("ru-RU")}</span>
            <button className="btn secondary sm" disabled={offset + LIMIT >= total} onClick={() => setOffset(offset + LIMIT)}>Вперёд →</button>
          </div>
        )}
      </Card>
    </div>
  );
}
