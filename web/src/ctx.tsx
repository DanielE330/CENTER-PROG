import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "./api";
import type { Run } from "./types";

export type Me = { email: string; company_id: string; company_name: string };
export type RunState = "loading" | "empty" | "ok" | "error";

type Ctx = {
  me: Me;
  setMe: (m: Me | null) => void;
  base: string; // /api/v1/companies/{id}
  run: Run | null; // последний базовый прогноз
  runState: RunState;
  error: string;
  rev: number; // растёт при любом изменении данных — страницы перечитывают свои данные
  horizon: number;
  refresh: () => void;
  recalc: (horizonDays?: number) => Promise<void>;
  recalcBusy: boolean;
  importOpen: boolean;
  openImport: () => void;
  closeImport: () => void;
};

const C = createContext<Ctx | null>(null);
export const useApp = () => {
  const v = useContext(C);
  if (!v) throw new Error("AppProvider missing");
  return v;
};

export function AppProvider({ me, setMe, children }: { me: Me; setMe: (m: Me | null) => void; children: React.ReactNode }) {
  const base = `/api/v1/companies/${me.company_id}`;
  const [rev, setRev] = useState(0);
  const [run, setRun] = useState<Run | null>(null);
  const [runState, setState] = useState<RunState>("loading");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const refresh = useCallback(() => setRev((r) => r + 1), []);

  useEffect(() => {
    let alive = true;
    api
      .get<Run>(`${base}/forecasts/latest`)
      .then((r) => { if (alive) { setRun(r); setState("ok"); } })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) { setRun(null); setState("empty"); }
        else { setError(e.message); setState("error"); }
      });
    return () => { alive = false; };
  }, [base, rev]);

  const recalc = useCallback(async (horizonDays?: number) => {
    setBusy(true);
    setError("");
    try {
      await api.post(`${base}/forecasts`, { horizon_days: horizonDays ?? 30 });
      refresh();
    } finally {
      setBusy(false);
    }
  }, [base, refresh]);

  const value = useMemo<Ctx>(() => ({
    me, setMe, base, run, runState, error, rev, horizon: run?.result.horizon ?? 30, refresh, recalc, recalcBusy: busy,
    importOpen, openImport: () => setImportOpen(true), closeImport: () => setImportOpen(false),
  }), [me, setMe, base, run, runState, error, rev, refresh, recalc, busy, importOpen]);

  return <C.Provider value={value}>{children}</C.Provider>;
}
