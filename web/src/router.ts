import { useEffect, useState } from "react";

export const ROUTES = [
  { path: "/dashboard", label: "Обзор", icon: "overview" },
  { path: "/forecast", label: "Прогноз", icon: "forecast" },
  { path: "/transactions", label: "Операции", icon: "transactions" },
  { path: "/obligations", label: "Платежи и обязательства", icon: "obligations" },
  { path: "/scenarios", label: "Сценарии", icon: "scenarios" },
  { path: "/plan-vs-actual", label: "План-факт", icon: "planfact" },
  { path: "/assistant", label: "AI-консультант", icon: "assistant" },
  { path: "/settings", label: "Настройки", icon: "settings" },
] as const;

export type RoutePath = (typeof ROUTES)[number]["path"];

const known = (p: string): RoutePath => (ROUTES.find((r) => r.path === p)?.path ?? "/dashboard") as RoutePath;

export function navigate(path: RoutePath | string) {
  if (window.location.pathname !== path) window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}

export function usePath(): RoutePath {
  const [p, setP] = useState<RoutePath>(known(window.location.pathname));
  useEffect(() => {
    const on = () => setP(known(window.location.pathname));
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return p;
}
