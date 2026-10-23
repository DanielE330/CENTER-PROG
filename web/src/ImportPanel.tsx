import { useState } from "react";
import { api, rub, CATEGORY_RU } from "./api";
import { Icon } from "./icons";
import { Badge, Notice } from "./ui";

export type ImportPreview = {
  import_id: string;
  summary: {
    stats: {
      rows_ok: number; date_from: string; date_to: string; days_span: number; has_balance: boolean;
      balance_mismatches?: number; balance_mode?: string; missing_period_count?: number;
      first_gap_dates?: [string, string][]; suspected_duplicates?: number; opening_balance_kop?: string;
    };
    errors_total: number; errors: { row: number; code: string }[]; duplicates_existing: number; categories: Record<string, number>;
    sample: { date: string; amount_kop: string; counterparty: string; purpose: string; category: string }[];
  };
};

/** Загрузка → предпросмотр → подтверждение. Пересчёт прогноза выполняет родитель в onDone. */
export function ImportFlow({ base, onDone }: { base: string; onDone: () => void | Promise<void> }) {
  const [pv, setPv] = useState<ImportPreview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function pick(f?: File) {
    if (!f) return;
    setBusy(true); setError("");
    try { setPv(await api.upload<ImportPreview>(`${base}/imports`, f)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!pv) return;
    setBusy(true); setError("");
    try {
      await api.post(`${base}/imports/${pv.import_id}/confirm`);
      setPv(null);
      await onDone();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  const s = pv?.summary;
  const st = s?.stats;
  return (
    <div className="stack">
      {!pv && (
        <>
          <p className="muted small">CSV или XLSX: дата, сумма (или списание/поступление), контрагент, ИНН, назначение платежа, остаток на конец дня.</p>
          <label className="btn secondary" style={{ alignSelf: "flex-start", cursor: busy ? "default" : "pointer" }}>
            <Icon name="upload" size={16} />{busy ? "Загрузка…" : "Выбрать файл"}
            <input type="file" accept=".csv,.xlsx,.txt" disabled={busy} style={{ display: "none" }} onChange={(e) => { pick(e.target.files?.[0]); e.target.value = ""; }} />
          </label>
        </>
      )}
      {error && <Notice tone="risk">{error}</Notice>}
      {s && st && (
        <>
          <div className="grid3">
            <div className="stat"><b>{st.rows_ok}</b><span>операций разобрано</span></div>
            <div className="stat"><b>{st.date_from} — {st.date_to}</b><span>{st.days_span} дн.</span></div>
            <div className="stat"><b>{s.errors_total}</b><span>строк отклонено</span></div>
          </div>
          {!st.has_balance && <Notice tone="risk">В файле нет колонки остатка — прогноз построить не удастся.</Notice>}
          {!!st.balance_mismatches && <Notice tone="warn">Остатки не сходятся в {st.balance_mismatches} случаях — прогноз может быть неточным.</Notice>}
          {!!st.missing_period_count && (
            <Notice tone="warn">
              Возможно, пропущены периоды выписки ({st.missing_period_count}).
              {st.first_gap_dates?.length ? ` Например: ${st.first_gap_dates.slice(0, 3).map(([a, b]) => `${a} — ${b}`).join("; ")}.` : ""}
            </Notice>
          )}
          {!!st.suspected_duplicates && <Notice tone="warn">Похожих на дубли строк: {st.suspected_duplicates}. Они не удаляются автоматически — проверьте файл.</Notice>}
          {s.duplicates_existing > 0 && <Notice tone="neutral">{s.duplicates_existing} операций уже есть в системе и будут пропущены.</Notice>}
          <div>
            <div className="small muted" style={{ marginBottom: 8 }}>Категории</div>
            <div className="chips">{Object.entries(s.categories).map(([k, v]) => <span key={k} className="badge">{CATEGORY_RU[k] ?? k}: {v}</span>)}</div>
          </div>
          {s.errors_total > 0 && (
            <details><summary className="small">Отклонённые строки ({s.errors_total})</summary>
              {s.errors.slice(0, 20).map((e) => <div key={e.row} className="small muted">строка {e.row}: {e.code}</div>)}
            </details>
          )}
          <div className="table-wrap">
            <table>
              <tbody>{s.sample.slice(0, 6).map((t, i) => (
                <tr key={i}><td>{t.date}</td><td className="num">{rub(t.amount_kop, { sign: true })}</td><td>{t.counterparty}</td><td>{CATEGORY_RU[t.category] ?? t.category}</td></tr>
              ))}</tbody>
            </table>
          </div>
          <div className="row">
            <button className="btn" onClick={confirm} disabled={busy || !st.has_balance}>{busy ? "Обработка…" : "Подтвердить и построить прогноз"}</button>
            <button className="btn secondary" onClick={() => setPv(null)} disabled={busy}>Другой файл</button>
          </div>
        </>
      )}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  if (status === "confirmed") return <Badge tone="ok">Подтверждён</Badge>;
  if (status === "preview") return <Badge tone="warn">Ожидает подтверждения</Badge>;
  return <Badge>{status}</Badge>;
}

export function ImportPanel({ base, onClose, onDone }: { base: string; onClose: () => void; onDone: () => void }) {
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Загрузка выписки">
      <div className="card modal-body stack">
        <div className="row"><h2>Загрузка банковской выписки</h2><div className="grow" /><button className="icon-btn" aria-label="Закрыть" onClick={onClose}><Icon name="close" /></button></div>
        <ImportFlow base={base} onDone={onDone} />
      </div>
    </div>
  );
}
