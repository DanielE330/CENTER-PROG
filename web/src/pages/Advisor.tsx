import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Me } from "../api";

interface Proposal {
  id: string;
  kind: string;
  title: string;
  status: string;
}

interface Reply {
  answer: string;
  tools_used: string[];
  proposals: string[];
  runtime: string;
  notice: "" | "not_connected" | "unavailable";
}

interface Message {
  role: "user" | "assistant";
  content: string;
  meta?: string;
  notice?: Reply["notice"];
}

const SUGGESTIONS = ["Почему может не хватить денег и что делать?", "Что будет, если крупный клиент заплатит на 3 недели позже?", "Какие расходы сильнее всего давят на остаток?"];

export function Advisor({ me }: { me: Me }) {
  const qc = useQueryClient();
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const proposals = useQuery({ queryKey: ["proposals"], queryFn: () => api.get<Proposal[]>("/api/v1/proposals") });
  const ask = useMutation({
    mutationFn: (message: string) =>
      api.post<Reply>("/api/v1/advisor/chat", { message, history: messages.map(({ role, content }) => ({ role, content })) }),
    onSuccess: (r) => {
      setMessages((m) => [...m, { role: "assistant", content: r.answer, notice: r.notice, meta: `${r.runtime}${r.tools_used.length ? ` · инструменты: ${r.tools_used.join(", ")}` : ""}` }]);
      qc.invalidateQueries({ queryKey: ["proposals"] });
    },
  });
  const decide = useMutation({
    mutationFn: ({ id, accept }: { id: string; accept: boolean }) => api.post(`/api/v1/proposals/${id}/${accept ? "accept" : "reject"}`),
    onSuccess: () => qc.invalidateQueries(),
  });
  const send = (message: string) => {
    if (!message.trim()) return;
    setMessages((m) => [...m, { role: "user", content: message }]);
    setText("");
    ask.mutate(message);
  };
  const owner = me.roles.includes("owner");
  const canWrite = me.roles.some((r) => r === "owner" || r === "accountant");
  const pending = (proposals.data ?? []).filter((p) => p.status === "pending");

  return (
    <div className="grid2 wide-left">
      <div className="card chat">
        <div className="muted">Консультант видит ваш прогноз и может только предлагать. Ничего не меняется без вашего подтверждения.</div>
        <div className="messages">
          {messages.length === 0 && (
            <div className="row wrap">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="secondary" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.notice && (
                <div className="warn-note small">
                  {m.notice === "unavailable" ? "ИИ-провайдер сейчас недоступен, ниже ответ по шаблону из прогноза." : "Это ответ по шаблону. Для персональных советов подключите ИИ-провайдера"}
                  {m.notice === "not_connected" && (owner ? <> в <a href="#settings">настройках</a>.</> : ": это может сделать владелец.")}
                </div>
              )}
              <div className="bubble">{m.content}</div>
              {m.meta && <div className="muted small">{m.meta}</div>}
            </div>
          ))}
          {ask.isPending && <div className="muted">Консультант думает…</div>}
          {ask.isError && <div className="error">{String(ask.error)}</div>}
        </div>
        <div className="row">
          <input className="grow" value={text} maxLength={2000} placeholder="Спросите о прогнозе" onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send(text)} />
          <button disabled={ask.isPending} onClick={() => send(text)}>
            Спросить
          </button>
        </div>
      </div>
      <div className="stack">
        <h2>Предложения</h2>
        {pending.length === 0 && <div className="card muted">Пока нет.</div>}
        {pending.map((p) => (
          <div key={p.id} className="card">
            <div>{p.title}</div>
            {canWrite && (
              <div className="row">
                <button onClick={() => decide.mutate({ id: p.id, accept: true })}>Добавить</button>
                <button className="secondary" onClick={() => decide.mutate({ id: p.id, accept: false })}>
                  Отклонить
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
