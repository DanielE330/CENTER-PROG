import { useEffect, useRef, useState } from "react";
import { api, dateRu } from "./api";
import { useApp } from "./ctx";
import { Icon } from "./icons";
import { ROUTES, navigate, type RoutePath } from "./router";
import { Badge } from "./ui";
import { ImportPanel } from "./ImportPanel";

function useOutside(close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const on = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) close(); };
    document.addEventListener("mousedown", on);
    return () => document.removeEventListener("mousedown", on);
  }, [close]);
  return ref;
}

function Notifications() {
  const { run } = useApp();
  const [open, setOpen] = useState(false);
  const ref = useOutside(() => setOpen(false));
  const warnings = run?.result.risk.warnings ?? [];
  return (
    <div className="menu-wrap" ref={ref}>
      <button className="icon-btn" aria-label={`Уведомления: ${warnings.length}`} aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="bell" />
        {warnings.length > 0 && <span className="dot">{warnings.length}</span>}
      </button>
      {open && (
        <div className="dropdown notif" role="menu">
          <div className="dropdown-title">Уведомления</div>
          {warnings.length === 0 && <div className="dropdown-empty muted">Новых предупреждений нет</div>}
          {warnings.map((w, i) => (
            <button key={i} className="notif-item" onClick={() => { setOpen(false); navigate("/forecast"); }}>
              <Badge tone={w.severity === "high" ? "risk" : "warn"}>{w.severity === "high" ? "Критично" : "Внимание"}</Badge>
              <span>{w.message}{w.date ? ` · ${dateRu(w.date)}` : ""}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Profile() {
  const { me, setMe } = useApp();
  const [open, setOpen] = useState(false);
  const ref = useOutside(() => setOpen(false));
  const initials = me.email.slice(0, 2).toUpperCase();
  return (
    <div className="menu-wrap" ref={ref}>
      <button className="profile-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="avatar">{initials}</span>
        <span className="profile-mail">{me.email}</span>
        <Icon name="down" size={16} />
      </button>
      {open && (
        <div className="dropdown" role="menu">
          <div className="dropdown-title">{me.email}</div>
          <button className="dropdown-item" onClick={() => { setOpen(false); navigate("/settings"); }}><Icon name="settings" size={16} />Настройки</button>
          <button className="dropdown-item" onClick={async () => { await api.post("/api/v1/auth/logout"); setMe(null); }}><Icon name="logout" size={16} />Выйти</button>
        </div>
      )}
    </div>
  );
}

export function Layout({ path, children }: { path: RoutePath; children: React.ReactNode }) {
  const { me, importOpen, closeImport, refresh, base, recalc } = useApp();
  const [nav, setNav] = useState(false);
  useEffect(() => setNav(false), [path]);
  return (
    <div className="shell">
      <aside className={nav ? "sidebar open" : "sidebar"} aria-label="Основное меню">
        <div className="logo"><span className="logo-mark">ЦИ</span><span>Кассовый прогноз</span></div>
        <nav>
          {ROUTES.map((r) => (
            <a key={r.path} href={r.path} className={path === r.path ? "nav-item on" : "nav-item"} aria-current={path === r.path ? "page" : undefined}
              onClick={(e) => { e.preventDefault(); navigate(r.path); }}>
              <Icon name={r.icon} /><span>{r.label}</span>
            </a>
          ))}
        </nav>
      </aside>
      {nav && <div className="scrim" onClick={() => setNav(false)} />}
      <div className="main-col">
        <header className="topbar">
          <button className="icon-btn menu-btn" aria-label="Меню" onClick={() => setNav(!nav)}><Icon name={nav ? "close" : "menu"} /></button>
          <div className="org" title="Организация">{me.company_name}</div>
          <div className="grow" />
          <Notifications />
          <Profile />
        </header>
        <main className="page">{children}</main>
      </div>
      {importOpen && (
        <ImportPanel base={base} onClose={closeImport} onDone={async () => { closeImport(); try { await recalc(); } catch { refresh(); } }} />
      )}
    </div>
  );
}
