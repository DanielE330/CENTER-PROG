import { useState } from "react";
import { api, rub, CATEGORY_RU } from "./api";

type Preview = {
  import_id: string;
  summary: {
    stats: { rows_ok: number; date_from: string; date_to: string; days_span: number; balance_mismatches?: number; has_balance: boolean };
    errors_total: number; errors: { row: number; code: string }[]; duplicates_existing: number; categories: Record<string, number>;
    sample: { date: string; amount_kop: string; counterparty: string; purpose: string; category: string }[];
  };
};

export function ImportPanel({ base, onClose, onDone }: { base: string; onClose: () => void; onDone: () => void }) {
  const [pv, setPv] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function pick(f?: File) {
    if (!f) return;
    setBusy(true); setError("");
    try { setPv(await api.upload<Preview>(`${base}/imports`, f)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function confirm() {
    setBusy(true);
    try {
      await api.post(`${base}/imports/${pv!.import_id}/confirm`);
      await api.post(`${base}/forecasts`);
      onDone();
    } catch (e) { setError((e as Error).message); setBusy(false); }
  }
  const s = pv?.summary;
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Загрузка выписки">
      <div className="card modal-body">
        <div className="row"><h2>Загрузка банковской выписки</h2><div className="grow" /><button className="ghost" onClick={onClose}>✕</button></div>
        {!pv && (
          <>
            <p className="muted">CSV или XLSX: дата, сумма (или списание/поступление), контрагент, ИНН, назначение, остаток на конец дня.</p>
            <input type="file" accept=".csv,.xlsx,.txt" disabled={busy} onChange={(e) => pick(e.target.files?.[0])} />
          </>
        )}
        {error && <div className="alert high" role="alert">{error}</div>}
        {s && (
          <>
            <div className="grid3">
              <div className="stat"><b>{s.stats.rows_ok}</b><span>операций разобрано</span></div>
              <div className="stat"><b>{s.stats.date_from} — {s.stats.date_to}</b><span>{s.stats.days_span} дн.</span></div>
              <div className="stat"><b>{s.errors_total}</b><span>строк отклонено</span></div>
            </div>
            {s.stats.balance_mismatches ? <div className="alert medium">Остатки не сходятся в {s.stats.balance_mismatches} строках — прогноз может быть неточным.</div> : null}
            {s.duplicates_existing > 0 && <div className="alert low">{s.duplicates_existing} операций уже есть в системе и будут пропущены.</div>}
            {!s.stats.has_balance && <div className="alert high">В файле нет колонки остатка — прогноз построить не удастся.</div>}
            <h3>Категории</h3>
            <div className="chips">{Object.entries(s.categories).map(([k, v]) => <span key={k} className="chip">{CATEGORY_RU[k] ?? k}: {v}</span>)}</div>
            {s.errors_total > 0 && <details><summary>Отклонённые строки</summary>{s.errors.map((e) => <div key={e.row} className="small">строка {e.row}: {e.code}</div>)}</details>}
            <h3>Пример операций</h3>
            <table><tbody>{s.sample.slice(0, 6).map((t, i) => (
              <tr key={i}><td>{t.date}</td><td className="num">{rub(t.amount_kop, { sign: true })}</td><td>{t.counterparty}</td><td>{CATEGORY_RU[t.category]}</td></tr>
            ))}</tbody></table>
            <div className="row"><button onClick={confirm} disabled={busy}>Подтвердить и построить прогноз</button><button className="ghost" onClick={() => setPv(null)}>Другой файл</button></div>
          </>
        )}
      </div>
    </div>
  );
}
