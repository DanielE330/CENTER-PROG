import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "./api";
import Login from "./Login";
import { Dashboard } from "./Dashboard";
import { Transactions } from "./Transactions";
import { Events } from "./Events";
import { Scenarios } from "./Scenarios";
import { PlanFact } from "./PlanFact";
import { Quality } from "./Quality";
import { ImportPanel } from "./ImportPanel";

type Me = { email: string; company_id: string; company_name: string };
const TABS = [
  ["forecast", "Прогноз"], ["scenarios", "Что если"], ["planfact", "План-факт"], ["events", "Ожидаемые платежи"],
  ["tx", "Операции"], ["quality", "Качество модели"],
] as const;

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("forecast");
  const [showImport, setShowImport] = useState(false);
  const [rev, setRev] = useState(0); // растёт при изменении данных → экраны перечитывают
  const bump = useCallback(() => setRev((r) => r + 1), []);

  useEffect(() => {
    api.get<Me>("/api/v1/auth/me").then(setMe).catch((e) => {
      if (e instanceof ApiError && e.status === 401) setMe(null);
      else setMe(null);
    });
  }, []);

  if (me === undefined) return <div className="center muted">Загрузка…</div>;
  if (me === null) return <Login onDone={setMe} />;
  const base = `/api/v1/companies/${me.company_id}`;

  return (
    <div className="app">
      <header>
        <h1>Кассовый прогноз</h1>
        <span className="chip" title="Юридическое лицо">{me.company_name}</span>
        <div className="grow" />
        <button onClick={() => setShowImport(true)}>Загрузить выписку</button>
        <span className="muted small">{me.email}</span>
        <button className="ghost" onClick={async () => { await api.post("/api/v1/auth/logout"); setMe(null); }}>Выйти</button>
      </header>
      <nav role="tablist">
        {TABS.map(([k, t]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "tab on" : "tab"} onClick={() => setTab(k)}>{t}</button>
        ))}
      </nav>
      <main>
        {showImport && <ImportPanel base={base} onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); bump(); }} />}
        {tab === "forecast" && <Dashboard base={base} rev={rev} onChange={bump} openImport={() => setShowImport(true)} />}
        {tab === "scenarios" && <Scenarios base={base} rev={rev} />}
        {tab === "planfact" && <PlanFact base={base} rev={rev} />}
        {tab === "events" && <Events base={base} rev={rev} onChange={bump} />}
        {tab === "tx" && <Transactions base={base} rev={rev} onChange={bump} />}
        {tab === "quality" && <Quality base={base} rev={rev} />}
      </main>
    </div>
  );
}
