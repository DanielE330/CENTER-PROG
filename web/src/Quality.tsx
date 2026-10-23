import { useState } from "react";
import { api, rub, pct } from "./api";

type BT = {
  origins: number; alarm_precision: number | null; alarm_recall: number | null; actual_gap_windows: number; note: string;
  metrics: { horizon: number; mae_model: string; mae_last_value: string; mae_drift: string; model_beats_baselines_share: number; n: number }[];
};

export function Quality({ base }: { base: string; rev: number }) {
  const [bt, setBt] = useState<BT | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function run() {
    setBusy(true); setError("");
    try { setBt(await api.get<BT>(`${base}/backtest`)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <section className="card">
      <h3>Проверка качества прогноза (rolling-origin backtest)</h3>
      <p className="muted">Модель «возвращается в прошлое»: на каждой контрольной дате видит только данные до неё, строит прогноз на 7, 14 и 30 дней и сравнивается с фактическим остатком. Сравнение с двумя наивными подходами.</p>
      <button onClick={run} disabled={busy}>{busy ? "Считаю…" : "Запустить backtest"}</button>
      {error && <div className="alert high">{error}</div>}
      {bt && (
        <>
          <table>
            <thead><tr><th>Горизонт</th><th className="num">MAE модели</th><th className="num">MAE «остаток не меняется»</th><th className="num">MAE «тренд»</th><th className="num">Модель лучше обоих</th><th className="num">Точек</th></tr></thead>
            <tbody>{bt.metrics.map((m) => (
              <tr key={m.horizon}><td>{m.horizon} дн.</td><td className="num">{rub(m.mae_model)}</td><td className="num">{rub(m.mae_last_value)}</td><td className="num">{rub(m.mae_drift)}</td><td className="num">{pct(m.model_beats_baselines_share)}</td><td className="num">{m.n}</td></tr>
            ))}</tbody>
          </table>
          <div className="grid3">
            <div className="stat"><b>{bt.origins}</b><span>контрольных дат</span></div>
            <div className="stat"><b>{bt.alarm_precision == null ? "—" : pct(bt.alarm_precision)}</b><span>точность тревог (P ≥ 50%)</span></div>
            <div className="stat"><b>{bt.alarm_recall == null ? "—" : pct(bt.alarm_recall)}</b><span>полнота ({bt.actual_gap_windows} реальных окон с разрывом)</span></div>
          </div>
          <p className="small muted">{bt.note} Если в истории мало реальных разрывов, precision/recall не показательны.</p>
        </>
      )}
    </section>
  );
}
