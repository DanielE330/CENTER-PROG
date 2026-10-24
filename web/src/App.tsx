import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, type Me } from "./api";
import { Advisor } from "./pages/Advisor";
import { Dashboard } from "./pages/Dashboard";
import { Obligations } from "./pages/Obligations";
import { Operations } from "./pages/Operations";
import { PlanFact } from "./pages/PlanFact";
import { Quality } from "./pages/Quality";
import { Scenarios } from "./pages/Scenarios";
import { SettingsPage } from "./pages/Settings";

const PAGES = {
  dashboard: { title: "Главная", view: Dashboard },
  operations: { title: "Операции", view: Operations },
  obligations: { title: "Будущие платежи", view: Obligations },
  scenarios: { title: "Что если", view: Scenarios },
  planfact: { title: "План-факт", view: PlanFact },
  advisor: { title: "Консультант", view: Advisor },
  quality: { title: "Качество прогноза", view: Quality },
  settings: { title: "Настройки", view: SettingsPage },
} as const;

type PageKey = keyof typeof PAGES;

function currentPage(): PageKey {
  const key = window.location.hash.replace("#", "") as PageKey;
  return key in PAGES ? key : "dashboard";
}

/** Server-sent events: refetch what changed instead of polling. */
function useLiveUpdates() {
  const qc = useQueryClient();
  useEffect(() => {
    const source = new EventSource("/api/v1/stream");
    source.addEventListener("forecast.updated", () => {
      qc.invalidateQueries({ queryKey: ["forecast"] });
      qc.invalidateQueries({ queryKey: ["planfact"] });
      qc.invalidateQueries({ queryKey: ["scenario"] });
    });
    source.addEventListener("alerts.raised", () => qc.invalidateQueries({ queryKey: ["alerts"] }));
    source.addEventListener("advisor.proposal_created", () => qc.invalidateQueries({ queryKey: ["proposals"] }));
    return () => source.close();
  }, [qc]);
}

export function App() {
  const [page, setPage] = useState<PageKey>(currentPage());
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/v1/me") });
  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (me.isLoading) return <div className="center">Загрузка…</div>;
  if (me.isError) return <div className="center">Не удалось войти. Обновите страницу.</div>;
  return <Shell me={me.data as Me} page={page} />;
}

function Shell({ me, page }: { me: Me; page: PageKey }) {
  useLiveUpdates();
  const View = PAGES[page].view;
  return (
    <div className="layout">
      <aside>
        <div className="brand">ЦИ1</div>
        <div className="org">{me.organization ?? "организация"}</div>
        <nav>
          {(Object.keys(PAGES) as PageKey[]).map((key) => (
            <a key={key} href={`#${key}`} className={key === page ? "active" : ""}>
              {PAGES[key].title}
            </a>
          ))}
        </nav>
        <div className="user">
          <div>{me.email ?? me.sub}</div>
          <div className="muted">{me.roles.filter((r) => ["owner", "accountant", "viewer"].includes(r)).join(", ")}</div>
          <button className="link" onClick={() => void api.logout()}>
            Выйти
          </button>
        </div>
      </aside>
      <main>
        <h1>{PAGES[page].title}</h1>
        <View me={me} />
      </main>
    </div>
  );
}
