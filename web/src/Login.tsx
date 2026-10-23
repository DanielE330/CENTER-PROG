import { useState } from "react";
import { api } from "./api";
import type { Me } from "./ctx";
import { Field } from "./ui";

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
  const type = show ? "text" : "password";

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
        <Field label="Пароль"><input type={type} value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} autoComplete={reg ? "new-password" : "current-password"} /></Field>
        {reg && <Field label="Повторите пароль"><input type={type} value={confirm} onChange={(e) => setConfirm(e.target.value)} required minLength={8} autoComplete="new-password" /></Field>}
        {mismatch && <div className="small neg" role="alert">Пароли не совпадают</div>}
        <label className="check"><input type="checkbox" checked={show} onChange={(e) => setShow(e.target.checked)} /> Показать пароль</label>
        {error && <div className="alert" role="alert">{error}</div>}
        <button className="btn" disabled={busy || mismatch}>{reg ? "Зарегистрироваться" : "Войти"}</button>
        <button type="button" className="link" onClick={() => { setMode(reg ? "login" : "register"); setError(""); setConfirm(""); }}>
          {reg ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Регистрация"}
        </button>
      </form>
    </div>
  );
}
