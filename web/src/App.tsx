import { useEffect, useState } from "react";
import { api } from "./api";
import { AppProvider, type Me } from "./ctx";
import { Layout } from "./Layout";
import Login from "./Login";
import { usePath } from "./router";
import Overview from "./pages/Overview";
import Forecast from "./pages/Forecast";
import Transactions from "./pages/Transactions";
import Obligations from "./pages/Obligations";
import Scenarios from "./pages/Scenarios";
import PlanFact from "./pages/PlanFact";
import Assistant from "./pages/Assistant";
import Settings from "./pages/Settings";

const PAGES = {
  "/dashboard": Overview, "/forecast": Forecast, "/transactions": Transactions, "/obligations": Obligations,
  "/scenarios": Scenarios, "/plan-vs-actual": PlanFact, "/assistant": Assistant, "/settings": Settings,
} as const;

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const path = usePath();
  useEffect(() => { api.get<Me>("/api/v1/auth/me").then(setMe).catch(() => setMe(null)); }, []);

  if (me === undefined) return <div className="center muted">Загрузка…</div>;
  if (me === null) return <Login onDone={setMe} />;
  const Page = PAGES[path];
  return (
    <AppProvider me={me} setMe={setMe}>
      <Layout path={path}><Page /></Layout>
    </AppProvider>
  );
}
