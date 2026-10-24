import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, type Me, type Settings } from "../api";
import { money, TAX_REGIMES } from "../format";

const PROVIDERS: Record<string, string> = {
  template: "Не подключён (ответы по шаблону)",
  anthropic: "Claude",
  openai: "ChatGPT",
  gemini: "Gemini",
  openrouter: "OpenRouter",
};

export function SettingsPage({ me }: { me: Me }) {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Settings>("/api/v1/settings") });
  const [limits, setLimits] = useState({ overdraft: "", min: "" });
  const [policy, setPolicy] = useState({ provider: "template", external_allowed: false, masking: true });
  useEffect(() => {
    if (s.data) {
      setLimits({ overdraft: String(s.data.overdraft_limit_kop / 100), min: String(s.data.min_balance_kop / 100) });
      setPolicy(s.data.llm_policy);
    }
  }, [s.data]);
  const save = useMutation({
    mutationFn: (body: object) => api.put<Settings>("/api/v1/settings", body),
    onSuccess: () => qc.invalidateQueries(),
  });
  const confirm = useMutation({
    mutationFn: (tax_regime: string) => api.post<Settings>("/api/v1/settings/tax-regime/confirm", { tax_regime }),
    onSuccess: () => qc.invalidateQueries(),
  });
  const owner = me.roles.includes("owner");
  const d = s.data;
  if (!d) return <div className="card">Загрузка…</div>;
  const external = policy.provider !== "template";

  return (
    <div className="stack">
      <div className="card">
        <strong>Режим налогообложения</strong>
        {d.tax_regime ? (
          <div>Подтверждён: {TAX_REGIMES[d.tax_regime] ?? d.tax_regime}. Налоги считаются по календарю.</div>
        ) : d.tax_regime_suggested ? (
          <div>
            Похоже, у вас <b>{TAX_REGIMES[d.tax_regime_suggested]}</b> (уверенность {Math.round((d.tax_regime_confidence ?? 0) * 100)}%). Верно?
          </div>
        ) : (
          <div className="muted">Определим по выписке, когда наберётся полгода данных.</div>
        )}
        <div className="row wrap">
          {Object.entries(TAX_REGIMES).map(([k, v]) => (
            <button key={k} className={k === (d.tax_regime ?? d.tax_regime_suggested) ? "" : "secondary"} disabled={confirm.isPending} onClick={() => confirm.mutate(k)}>
              {v}
            </button>
          ))}
        </div>
      </div>
      <div className="card">
        <strong>Пороги предупреждений</strong>
        <div className="row">
          <label>
            Неснижаемый остаток, ₽ <input value={limits.min} disabled={!owner} onChange={(e) => setLimits({ ...limits, min: e.target.value })} />
          </label>
          <label>
            Лимит овердрафта, ₽ <input value={limits.overdraft} disabled={!owner} onChange={(e) => setLimits({ ...limits, overdraft: e.target.value })} />
          </label>
          {owner && (
            <button
              onClick={() =>
                save.mutate({ min_balance_kop: Math.round(Number(limits.min || 0) * 100), overdraft_limit_kop: Math.round(Number(limits.overdraft || 0) * 100) })
              }
            >
              Сохранить
            </button>
          )}
        </div>
        <div className="muted">
          Сейчас: неснижаемый {money(d.min_balance_kop)}, овердрафт {money(d.overdraft_limit_kop)}. Предупреждения будут двух уровней: «уйдёте ниже
          неснижаемого» и «превысите лимит».
        </div>
      </div>
      <div className="card">
        <strong>Консультант и данные</strong>
        <div className="row">
          <select value={policy.provider} disabled={!owner} onChange={(e) => setPolicy({ ...policy, provider: e.target.value })}>
            {Object.entries(PROVIDERS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          {external && (
            <label>
              <input type="checkbox" checked={policy.external_allowed} disabled={!owner} onChange={(e) => setPolicy({ ...policy, external_allowed: e.target.checked })} />
              Разрешаю отправку обезличенных данных внешнему провайдеру
            </label>
          )}
          {owner && <button onClick={() => save.mutate({ llm_policy: { ...policy, masking: true } })}>Сохранить</button>}
        </div>
        <div className="muted">
          Консультант работает на облачной модели. Ей уходят только замаскированные данные: названия контрагентов заменяются на «Контрагент A», ИНН,
          счета и телефоны вырезаются. Пока провайдер не подключён или недоступен, консультант отвечает по шаблону из прогноза.
        </div>
        {save.isError && <div className="error">{String(save.error)}</div>}
      </div>
    </div>
  );
}
