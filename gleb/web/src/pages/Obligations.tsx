import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Me, type Obligation } from "../api";
import { day, money } from "../format";

const KIND: Record<string, string> = {
  manual: "Вручную",
  tax: "Налог",
  invoice_out: "Счёт клиенту",
  invoice_in: "Счёт поставщика",
  recurring: "Регулярный",
  contract: "Договор",
};

const SOURCE: Record<string, string> = {
  user: "введено вами",
  tax_calendar: "налоговый календарь",
  edo_replay: "ЭДО (реконструкция)",
};

const STATUS: Record<string, string> = { planned: "ожидается", overdue: "просрочен", paid: "оплачен", cancelled: "отменён" };

export function Obligations({ me }: { me: Me }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["obligations"], queryFn: () => api.get<Obligation[]>("/api/v1/obligations") });
  const [form, setForm] = useState({ direction: "inflow", amount: "", due_date: "", note: "" });
  const create = useMutation({
    mutationFn: () =>
      api.post("/api/v1/obligations", {
        direction: form.direction,
        amount_kop: Math.round(Number(form.amount.replace(",", ".").replace(/\s/g, "")) * 100),
        due_date: form.due_date,
        note: form.note || undefined,
      }),
    onSuccess: () => {
      setForm({ direction: "inflow", amount: "", due_date: "", note: "" });
      qc.invalidateQueries({ queryKey: ["obligations"] });
    },
  });
  const cancel = useMutation({ mutationFn: (id: string) => api.del(`/api/v1/obligations/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["obligations"] }) });
  const canWrite = me.roles.some((r) => r === "owner" || r === "accountant");
  const open = (list.data ?? []).filter((o) => o.status === "planned" || o.status === "overdue");
  const closed = (list.data ?? []).filter((o) => o.status === "paid").slice(-20);

  return (
    <div className="stack">
      {canWrite && (
        <div className="card">
          <strong>Добавить событие</strong>
          <div className="muted">Например: «клиент обещал заплатить 300 000 ₽ 20-го» или «в декабре куплю оборудование за 1 млн».</div>
          <div className="row">
            <select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value })}>
              <option value="inflow">Поступление</option>
              <option value="outflow">Платёж</option>
            </select>
            <input placeholder="Сумма, ₽" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
            <input type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} />
            <input placeholder="Комментарий" maxLength={200} value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
            <button disabled={!form.amount || !form.due_date || create.isPending} onClick={() => create.mutate()}>
              Добавить
            </button>
          </div>
          {create.isError && <div className="error">{String(create.error)}</div>}
        </div>
      )}
      <div className="card">
        <strong>Ожидаются ({open.length})</strong>
        <ObligationTable rows={open} onCancel={canWrite ? (id) => cancel.mutate(id) : undefined} />
      </div>
      <div className="card">
        <strong>Недавно закрылись по выписке</strong>
        <ObligationTable rows={closed} />
      </div>
    </div>
  );
}

function ObligationTable({ rows, onCancel }: { rows: Obligation[]; onCancel?: (id: string) => void }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Срок</th>
          <th>Что</th>
          <th>Сумма</th>
          <th>Источник</th>
          <th>Статус</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((o) => (
          <tr key={o.id} className={o.status === "overdue" ? "overdue" : ""}>
            <td>{day(o.due_date)}</td>
            <td>
              {KIND[o.kind] ?? o.kind}
              {o.note ? ` — ${o.note}` : ""}
              {o.doc_no ? ` № ${o.doc_no}` : ""}
            </td>
            <td className={o.direction === "inflow" ? "pos num" : "neg num"}>{money(o.direction === "inflow" ? o.amount_kop : -o.amount_kop)}</td>
            <td className="muted">{SOURCE[o.source] ?? o.source}</td>
            <td>{STATUS[o.status] ?? o.status}</td>
            <td>
              {onCancel && o.source === "user" && (
                <button className="link" onClick={() => onCancel(o.id)}>
                  отменить
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
