import { useQuery } from "@tanstack/react-query";

import { api } from "../api";

interface Row {
  variant: string;
  cutoffs: number;
  mae_rub: number;
  mae_turnover_days: number;
  coverage_p10_p90: number;
  gap_precision: number | null;
  gap_recall: number | null;
  gap_lead_days_median: number | null;
  actual_gap_windows: number;
}

const NAMES: Record<string, string> = {
  naive_flat: "Остаток не меняется",
  naive_last_week: "Повтор прошлой недели",
  weekday_bootstrap: "Бутстреп по дням недели",
  layered: "Слои: регулярные + остаток",
  layered_invoices: "Слои + выставленные счета",
};

const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`);

export function Quality() {
  const q = useQuery({ queryKey: ["backtest"], queryFn: () => api.get<{ horizon: number; results: Row[] }>("/api/v1/forecast/backtest"), staleTime: 600_000 });
  if (q.isLoading) return <div className="card">Считаем бэктест на вашей истории (до минуты)…</div>;
  if (q.isError) return <div className="card">{String(q.error)}</div>;
  return (
    <div className="card">
      <div className="muted">
        Прогноз «из прошлого»: модель видит данные только до даты среза и сравнивается с тем, что произошло за {q.data?.horizon} дней. Разрывом считается
        минус на счёте или спасательное поступление собственных денег.
      </div>
      <table>
        <thead>
          <tr>
            <th>Подход</th>
            <th>Срезов</th>
            <th>Ошибка остатка</th>
            <th>В днях оборота</th>
            <th>Попадание в коридор</th>
            <th>Точность разрывов</th>
            <th>Полнота разрывов</th>
            <th>За сколько дней</th>
          </tr>
        </thead>
        <tbody>
          {(q.data?.results ?? []).map((r) => (
            <tr key={r.variant}>
              <td>{NAMES[r.variant] ?? r.variant}</td>
              <td className="num">{r.cutoffs}</td>
              <td className="num">{Math.round(r.mae_rub).toLocaleString("ru-RU")} ₽</td>
              <td className="num">{r.mae_turnover_days}</td>
              <td className="num">{pct(r.coverage_p10_p90)}</td>
              <td className="num">{pct(r.gap_precision)}</td>
              <td className="num">{pct(r.gap_recall)}</td>
              <td className="num">{r.gap_lead_days_median ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
