import { useCallback, useEffect, useState } from "react";
import { api, dateRu } from "../api";
import { useApp } from "../ctx";
import { ImportFlow, StatusBadge } from "../ImportPanel";
import { Badge, Card, Field, Notice, PageHeader } from "../ui";

type ImportRow = { import_id: string; filename: string; status: string; created_at: string; rows_ok: number | null; errors_total: number | null; date_from: string | null; date_to: string | null };
const PREFS_KEY = "ci1.prefs";
type Prefs = { horizon: number; money: "full" | "short" };

function loadPrefs(): Prefs {
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY) ?? "{}");
    return { horizon: [7, 14, 30, 60, 90].includes(p.horizon) ? p.horizon : 30, money: p.money === "short" ? "short" : "full" };
  } catch { return { horizon: 30, money: "full" }; }
}

export default function Settings() {
  const { me, setMe, base, recalc, rev } = useApp();
  const [name, setName] = useState(me.company_name);
  const [nameMsg, setNameMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [prefsSaved, setPrefsSaved] = useState(false);
  const [imports, setImports] = useState<ImportRow[]>([]);
  const [impError, setImpError] = useState("");

  const loadImports = useCallback(() => {
    api.get<{ items: ImportRow[] }>(`${base}/imports`).then((r) => setImports(r.items)).catch((e) => setImpError(e.message));
  }, [base]);
  useEffect(loadImports, [loadImports, rev]);

  async function saveName(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true); setNameMsg(null);
    try {
      const r = await api.patch<{ company_name: string }>(base, { name });
      setMe({ ...me, company_name: r.company_name });
      setNameMsg({ ok: true, text: "Название сохранено" });
    } catch (err) { setNameMsg({ ok: false, text: (err as Error).message }); }
    finally { setSaving(false); }
  }
  function savePrefs(e: React.FormEvent) {
    e.preventDefault();
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* хранилище недоступно */ }
    setPrefsSaved(true);
  }

  return (
    <div className="stack">
      <PageHeader title="Настройки" subtitle="Организация, предпочтения и импорт банковской выписки" />
      <Notice tone="neutral">Прототип: данные демонстрационные. Интеграции с банками и серверная обработка реальных счетов не подключены.</Notice>

      <div className="grid two">
        <Card title="Организация">
          <form className="stack" onSubmit={saveName}>
            <Field label="Юридическое лицо"><input value={name} onChange={(e) => { setName(e.target.value); setNameMsg(null); }} required minLength={2} maxLength={200} /></Field>
            <Field label="Email"><input value={me.email} readOnly /></Field>
            <Field label="Идентификатор компании"><input value={me.company_id} readOnly /></Field>
            {nameMsg && <Notice tone={nameMsg.ok ? "ok" : "risk"}>{nameMsg.text}</Notice>}
            <div><button className="btn" disabled={saving || name.trim() === me.company_name}>{saving ? "Сохранение…" : "Сохранить"}</button></div>
          </form>
        </Card>

        <Card title="Предпочтения отображения">
          <form className="stack" onSubmit={savePrefs}>
            <Field label="Горизонт прогноза по умолчанию">
              <select value={prefs.horizon} onChange={(e) => { setPrefs({ ...prefs, horizon: Number(e.target.value) }); setPrefsSaved(false); }}>
                {[7, 14, 30, 60, 90].map((h) => <option key={h} value={h}>{h} дней</option>)}
              </select>
            </Field>
            <Field label="Формат сумм">
              <select value={prefs.money} onChange={(e) => { setPrefs({ ...prefs, money: e.target.value as Prefs["money"] }); setPrefsSaved(false); }}>
                <option value="full">Полностью (1 234 567 ₽)</option>
                <option value="short">Кратко (1,2 млн ₽)</option>
              </select>
            </Field>
            <p className="small muted">Настройки хранятся только в этом браузере. В текущей версии они сохраняются, но ещё не применяются к страницам автоматически.</p>
            {prefsSaved && <Notice tone="ok">Предпочтения сохранены</Notice>}
            <div><button className="btn secondary">Сохранить предпочтения</button></div>
          </form>
        </Card>
      </div>

      <Card title="Импорт банковской выписки">
        <ImportFlow base={base} onDone={async () => { try { await recalc(prefs.horizon); } finally { loadImports(); } }} />
      </Card>

      <Card title="История импортов" pad={false}>
        {impError && <div className="card-body"><Notice tone="risk">{impError}</Notice></div>}
        <div className="table-wrap">
          <table>
            <thead><tr><th>Файл</th><th>Статус</th><th>Период</th><th className="num">Операций</th><th className="num">Отклонено</th><th>Загружен</th></tr></thead>
            <tbody>
              {imports.length === 0 && <tr><td colSpan={6} className="muted">Импортов пока нет</td></tr>}
              {imports.map((i) => (
                <tr key={i.import_id}>
                  <td>{i.filename}</td>
                  <td><StatusBadge status={i.status} /></td>
                  <td>{i.date_from && i.date_to ? `${dateRu(i.date_from)} — ${dateRu(i.date_to)}` : "—"}</td>
                  <td className="num">{i.rows_ok ?? "—"}</td>
                  <td className="num">{i.errors_total ? <Badge tone="warn">{i.errors_total}</Badge> : "0"}</td>
                  <td>{new Date(i.created_at).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
