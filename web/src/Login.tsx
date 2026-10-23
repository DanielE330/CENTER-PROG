import { useState } from "react";
import { api } from "./api";
import type { Me } from "./ctx";
import { Icon } from "./icons";
import { Field } from "./ui";

function PasswordInput({ value, onChange, autoComplete }: { value: string; onChange: (v: string) => void; autoComplete: string }) {
  const [show, setShow] = useState(false);
  return (
    <div className="pw-wrap">
      <input type={show ? "text" : "password"} value={value} onChange={(e) => onChange(e.target.value)} required minLength={8} autoComplete={autoComplete} />
      <button type="button" className="pw-eye" aria-label={show ? "Скрыть пароль" : "Показать пароль"} aria-pressed={show} title={show ? "Скрыть пароль" : "Показать пароль"} onClick={() => setShow(!show)}>
        <Icon name={show ? "eyeOff" : "eye"} size={18} />
      </button>
    </div>
  );
}

export default function Login({ onDone }: { onDone: (m: Me) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [company, setCompany] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
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
        <div className="brand"><span className="logo-mark">ЦИ</span>Кассовый прогноз</div>
        <div>
          <h1>{reg ? "Регистрация" : "Вход"}</h1>
          <p className="muted">Узнайте о кассовом разрыве заранее, а не в день платежа.</p>
        </div>
        {reg && (
          <Field label="Название юридического лица">
            <input value={company} onChange={(e) => setCompany(e.target.value)} required minLength={2} maxLength={200} placeholder="ООО «Ромашка»" autoComplete="organization" />
          </Field>
        )}
        <Field label="Email"><input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" /></Field>
        <Field label="Пароль"><PasswordInput value={password} onChange={setPassword} autoComplete={reg ? "new-password" : "current-password"} /></Field>
        {reg && <Field label="Повторите пароль"><PasswordInput value={confirm} onChange={setConfirm} autoComplete="new-password" /></Field>}
        {mismatch && <div className="small neg" role="alert">Пароли не совпадают</div>}
        {error && <div className="alert" role="alert">{error}</div>}
        <button className="btn" disabled={busy || mismatch}>{reg ? "Зарегистрироваться" : "Войти"}</button>
        <button type="button" className="link" onClick={() => { setMode(reg ? "login" : "register"); setError(""); setConfirm(""); }}>
          {reg ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Регистрация"}
        </button>
      </form>
    </div>
  );
}
