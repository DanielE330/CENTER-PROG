import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Bot, Loader2, Send, User } from 'lucide-react'
import { PageContainer, PageHeader } from '../../components/shared/Page'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { useForecast, useScenario } from '../../hooks/queries'
import { cn } from '../../lib/utils'
import { errorText, tm } from '../../i18n/helpers'
import type { AssistantMessage } from '../../types'
import { classify, localAnswer, scenarioAnswer } from './answers'
import type { Intent } from './answers'

const suggestions: Array<{ key: string; intent: Intent; days?: number }> = [
  { key: 'cause', intent: 'gap' },
  { key: 'delay', intent: 'delay', days: 14 },
  { key: 'biggest', intent: 'biggest' },
  { key: 'review', intent: 'check' },
  { key: 'reduce', intent: 'reduce' },
]

const welcome: AssistantMessage = { id: 'welcome', role: 'assistant', parts: [{ key: 'assistant.welcome' }] }

export default function AssistantPage() {
  const { t } = useTranslation()
  const q = useForecast()
  const scenario = useScenario()
  const [messages, setMessages] = useState<AssistantMessage[]>([welcome])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, busy])

  const v = q.view

  async function ask(text: string, hint?: { intent: Intent; days?: number }) {
    const question = text.trim()
    if (!question || busy || !v) return
    const { intent, days } = hint ?? classify(question)
    setMessages((all) => [...all, { id: crypto.randomUUID(), role: 'user', text: question }])
    setInput('')
    setBusy(true)
    const source = { key: 'assistant.source', params: { asOfDate: v.r.as_of, id: v.run.run_id.slice(0, 8) } }
    try {
      let parts
      if (intent === 'delay' || intent === 'revenue') {
        const d = intent === 'delay' ? Math.min(days || 14, 90) : 20
        const o = await scenario.mutateAsync({
          horizon: v.r.horizon,
          params: {
            delay_days: intent === 'delay' ? d : 0, delay_counterparty: null,
            revenue_drop_pct: intent === 'revenue' ? 20 : 0, fixed_cost_growth_pct: 0,
            extra_expense_kop: null, extra_expense_date: null, extra_income_kop: null, extra_income_date: null,
          },
        })
        parts = scenarioAnswer(intent, d, o)
      } else {
        parts = localAnswer(intent, v.r)
      }
      setMessages((all) => [...all, { id: crypto.randomUUID(), role: 'assistant', parts: [...parts, ...(intent === 'help' ? [] : [source])] }])
    } catch (e) {
      setMessages((all) => [...all, { id: crypto.randomUUID(), role: 'assistant', parts: [{ key: 'assistant.error', params: { reason: errorText(t, e) } }] }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <PageContainer>
      <PageHeader title={t('assistant.title')} description={t('assistant.description')} actions={<Badge>{t('assistant.rulesBadge')}</Badge>} />

      {q.isPending && <div className="card"><LoadingState /></div>}
      {q.isError && <div className="card"><ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} /></div>}
      {!q.isPending && !q.isError && !v && (
        <div className="card">
          <EmptyState
            icon={<Bot size={22} aria-hidden />}
            title={t('assistant.noForecastTitle')}
            description={t('assistant.noForecastText')}
            action={<Link to="/settings" className="text-primary font-medium hover:underline text-sm">{t('forecast.goToImport')}</Link>}
          />
        </div>
      )}

      {v && (
        <div className="card flex flex-col h-[calc(100vh-290px)] min-h-[420px]">
          <div className="flex-1 overflow-y-auto p-4 sm:p-5 flex flex-col gap-4" role="log" aria-live="polite" aria-label={t('assistant.logAria')}>
            {messages.map((m) => (
              <div key={m.id} className={cn('flex gap-3 max-w-3xl', m.role === 'user' && 'self-end flex-row-reverse')}>
                <span className={cn('grid place-items-center size-8 rounded-full shrink-0', m.role === 'user' ? 'bg-ink text-surface' : 'bg-primary-soft text-primary')} aria-hidden>
                  {m.role === 'user' ? <User size={16} /> : <Bot size={16} />}
                </span>
                <div className={cn('rounded-card px-4 py-2.5 text-[15px] whitespace-pre-line', m.role === 'user' ? 'bg-primary text-on-primary' : 'bg-app border border-line')}>
                  <span className="sr-only">{m.role === 'user' ? t('assistant.you') : t('assistant.bot')}: </span>
                  {m.role === 'user' ? m.text : m.parts?.map((p) => tm(t, p.key, p.params)).join('\n')}
                </div>
              </div>
            ))}
            {busy && (
              <div className="flex items-center gap-2 text-sm text-muted" role="status">
                <Loader2 size={16} className="spin" aria-hidden /> {t('assistant.thinking')}
              </div>
            )}
            <div ref={endRef} />
          </div>

          <div className="border-t border-line p-3 sm:p-4 flex flex-col gap-3">
            <ul className="flex flex-wrap gap-2" aria-label={t('assistant.suggestionsAria')}>
              {suggestions.map((s) => (
                <li key={s.key}>
                  <button
                    type="button"
                    onClick={() => void ask(t(`assistant.suggestions.${s.key}`), { intent: s.intent, days: s.days })}
                    disabled={busy}
                    className="rounded-full border border-line bg-surface px-3 py-1.5 text-[13px] enabled:hover:bg-primary-soft enabled:hover:border-primary cursor-pointer disabled:opacity-50"
                  >
                    {t(`assistant.suggestions.${s.key}`)}
                  </button>
                </li>
              ))}
            </ul>
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                void ask(input)
              }}
            >
              <input className="control" aria-label={t('assistant.inputAria')} placeholder={t('assistant.inputPlaceholder')} maxLength={300} value={input} onChange={(e) => setInput(e.target.value)} />
              <Button type="submit" variant="primary" disabled={!input.trim() || busy} aria-label={t('assistant.send')}>
                <Send size={16} aria-hidden />
                <span className="hidden sm:inline">{t('assistant.send')}</span>
              </Button>
            </form>
          </div>
        </div>
      )}
    </PageContainer>
  )
}
