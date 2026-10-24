import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Category, type Me, type Transaction } from "../api";
import { day, GROUP_NAMES, money } from "../format";

export function Operations({ me }: { me: Me }) {
  const qc = useQueryClient();
  const [offset, setOffset] = useState(0);
  const tx = useQuery({
    queryKey: ["transactions", offset],
    queryFn: () => api.get<{ total: number; items: Transaction[] }>(`/api/v1/transactions?limit=100&offset=${offset}`),
  });
  const cats = useQuery({ queryKey: ["categories"], queryFn: () => api.get<{ items: Category[] }>("/api/v1/categories") });
  const fix = useMutation({
    mutationFn: ({ id, category_id }: { id: string; category_id: string }) =>
      api.patch(`/api/v1/transactions/category/${id}`, { category_id, apply_to_similar: true }),
    onSuccess: () => setTimeout(() => qc.invalidateQueries({ queryKey: ["transactions"] }), 1200),
  });
  const canWrite = me.roles.some((r) => r === "owner" || r === "accountant");
  const byId = new Map((cats.data?.items ?? []).map((c) => [c.id, c]));
  const total = tx.data?.total ?? 0;

  return (
    <div className="card">
      <div className="muted">
        Категория ставится автоматически. Исправление применяется ко всем операциям этого контрагента и запоминается.
      </div>
      <table>
        <thead>
          <tr>
            <th>Дата</th>
            <th>Сумма</th>
            <th>Контрагент</th>
            <th>Назначение</th>
            <th>Категория</th>
          </tr>
        </thead>
        <tbody>
          {(tx.data?.items ?? []).map((t) => (
            <tr key={t.id}>
              <td>{day(t.op_date)}</td>
              <td className={t.amount_kop >= 0 ? "pos num" : "neg num"}>{money(t.amount_kop)}</td>
              <td>{t.counterparty_name}</td>
              <td className="purpose">{t.purpose}</td>
              <td>
                {canWrite ? (
                  <select value={t.category_id ?? ""} onChange={(e) => fix.mutate({ id: t.id, category_id: e.target.value })}>
                    <option value="" disabled>
                      —
                    </option>
                    {(cats.data?.items ?? []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {GROUP_NAMES[c.group] ?? c.group}: {c.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  byId.get(t.category_id ?? "")?.name ?? "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>
          ← Новее
        </button>
        <span className="muted">
          {offset + 1}–{Math.min(offset + 100, total)} из {total}
        </span>
        <button className="secondary" disabled={offset + 100 >= total} onClick={() => setOffset(offset + 100)}>
          Старее →
        </button>
      </div>
    </div>
  );
}
