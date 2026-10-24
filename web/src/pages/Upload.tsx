import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api";
import { day, money } from "../format";

interface ImportStats {
  rows: number;
  inserted: number;
  duplicates: number;
  format: string;
  date_from?: string;
  date_to?: string;
  balance_gaps: { after: string; before: string; missing_kop: number }[];
}

export function Upload() {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: (f: File) => api.upload<ImportStats>("/api/v1/imports", f),
    onSuccess: () => qc.invalidateQueries(),
  });
  const s = upload.data;
  return (
    <div className="card">
      <strong>Загрузить выписку</strong>
      <div className="muted">CSV в формате датасета или выгрузка 1С (1CClientBankExchange). Повторная загрузка не задвоит операции.</div>
      <div className="row">
        <input type="file" accept=".csv,.txt" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button disabled={!file || upload.isPending} onClick={() => file && upload.mutate(file)}>
          {upload.isPending ? "Загружаем…" : "Загрузить"}
        </button>
      </div>
      {upload.isError && <div className="error">{String(upload.error)}</div>}
      {s && (
        <div>
          Формат {s.format}: строк {s.rows}, новых {s.inserted}, дублей {s.duplicates}
          {s.date_from && s.date_to && `, период ${day(s.date_from)}–${day(s.date_to)}`}.
          {s.balance_gaps.length > 0 && (
            <div className="warn-note">
              В выписке не хватает операций:
              <ul>
                {s.balance_gaps.slice(0, 5).map((g) => (
                  <li key={g.after}>
                    между {day(g.after)} и {day(g.before)} (остаток расходится на {money(g.missing_kop)})
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
