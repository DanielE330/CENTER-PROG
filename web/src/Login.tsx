import { useState } from "react";
import { api } from "./api";

type Me = { email: string; company_id: string; company_name: string };

function PasswordField({ label, value, onChange, autoComplete, show, minLength }: {
  label: string; value: string; onChange: (v: string) => void; autoComplete: string; show: boolean; minLength?: number;
}) {
  return (
    <label>{label}
      <input type={show ? "text" : "password"} value={value} onChange={(e) => onChange(e.target.value)} required minLength={minLength} autoComplete={autoComplete} />
    </label>
  );
}

export default function Login({ onDone }: { onDone: (m: Me) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [company, setCompany] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const reg = mode === "register";
  const mismatch = reg && confirm !== "" && confirm !== password;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (reg && password !== confirm) return setError("Пароли не совпадают");
    setBusy(true);
    try {
      const body = reg ? { email, password, password_confirm: confirm, company_name: company.trim() } : { email, password };
      onDone(await api.post(`/api/v1/auth/${mode}`, body));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>Кассовый прогноз</h1>
        <p className="muted">Узнайте о кассовом разрыве заранее, а не в день платежа.</p>
        {reg && (
          <label>Название юридического лица
            <input value={company} onChange={(e) => setCompany(e.target.value)} required minLength={2} maxLength={200} placeholder="ООО «Ромашка»" autoComplete="organization" />
          </label>
        )}
        <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" /></label>
        <PasswordField label="Пароль" value={password} onChange={setPassword} show={show} minLength={8} autoComplete={reg ? "new-password" : "current-password"} />
        {reg && <PasswordField label="Повторите пароль" value={confirm} onChange={setConfirm} show={show} minLength={8} autoComplete="new-password" />}
        {mismatch && <div className="small neg" role="alert">Пароли не совпадают</div>}
        <label className="check"><input type="checkbox" checked={show} onChange={(e) => setShow(e.target.checked)} /> Показать пароль</label>
        {error && <div className="alert high" role="alert">{error}</div>}
        <button disabled={busy || mismatch}>{reg ? "Зарегистрироваться" : "Войти"}</button>
        <button type="button" className="link" onClick={() => { setMode(reg ? "login" : "register"); setError(""); setConfirm(""); }}>
          {reg ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Регистрация"}
        </button>
      </form>
    </div>
  );
}
