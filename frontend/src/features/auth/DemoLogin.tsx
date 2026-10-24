import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Eye, FlaskConical, Loader2, PencilLine, ShieldCheck } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '../../lib/api'
import { errorText } from '../../i18n/helpers'
import type { DemoLoginAccount, Me, MfaChallenge } from '../../types'

const ICONS: Record<DemoLoginAccount['id'], LucideIcon> = { director: ShieldCheck, editor: PencilLine, viewer: Eye }

/**
 * «Тестовый вход»: кнопки быстрого входа в демо-аккаунты, чтобы зайти без пароля (его подставляет сервер).
 * Блок виден, только если сервер ответил `enabled` (DEMO_LOGIN=1); иначе и при любой ошибке запроса не рисуется вовсе.
 */
export function DemoLogin({ onDone, onChallenge }: { onDone: (me: Me) => void; onChallenge: (c: MfaChallenge) => void }) {
  const { t } = useTranslation()
  const [accounts, setAccounts] = useState<DemoLoginAccount[]>([])
  const [busyId, setBusyId] = useState<DemoLoginAccount['id'] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    api
      .demoLoginStatus()
      .then((s) => alive && setAccounts(s.enabled ? s.accounts.filter((a) => a.id in ICONS) : []))
      .catch(() => alive && setAccounts([]))
    return () => {
      alive = false
    }
  }, [])

  if (!accounts.length) return null

  async function enter(id: DemoLoginAccount['id']) {
    setError('')
    setBusyId(id)
    try {
      const me = await api.demoLogin(id)
      // как после обычного входа: если включена 2FA, дальше второй фактор, иначе сессия готова
      if ('mfa' in me) {
        onChallenge(me)
        setBusyId(null)
      } else onDone(me)
    } catch (e) {
      setError(errorText(t, e))
      setBusyId(null)
    }
  }

  return (
    <section className="flex flex-col gap-3 border-t border-line pt-4" aria-labelledby="demo-login-title">
      <div>
        <h2 id="demo-login-title" className="flex items-center gap-2 text-[15px] font-semibold">
          <FlaskConical size={16} className="text-primary" aria-hidden />
          {t('auth.demo.title')}
        </h2>
        <p className="text-[13px] text-muted mt-1">{t('auth.demo.lead')}</p>
      </div>
      <ul className="flex flex-col gap-2 list-none p-0 m-0" aria-label={t('auth.demo.listLabel')}>
        {accounts.map((a) => {
          const Icon = ICONS[a.id]
          const busy = busyId === a.id
          return (
            <li key={a.id}>
              <button
                type="button"
                className="w-full flex items-start gap-3 text-left rounded-control border border-line bg-surface p-3 cursor-pointer enabled:hover:bg-hover disabled:opacity-60 disabled:cursor-not-allowed focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                disabled={busyId !== null}
                aria-busy={busy}
                aria-describedby={`demo-hint-${a.id}`}
                onClick={() => enter(a.id)}
                data-testid={`demo-login-${a.id}`}
              >
                <span className="grid place-items-center size-8 rounded-[8px] bg-primary-soft text-primary shrink-0" aria-hidden>
                  {busy ? <Loader2 size={16} className="spin" /> : <Icon size={16} />}
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{t(`auth.demo.${a.id}.name`)}</span>
                  <span id={`demo-hint-${a.id}`} className="block text-xs text-muted mt-0.5">{t(`auth.demo.${a.id}.hint`)}</span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      {error && (
        <div className="rounded-control bg-danger-soft p-3 text-sm text-danger" role="alert">{error}</div>
      )}
    </section>
  )
}
