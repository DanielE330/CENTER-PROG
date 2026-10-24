import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Bot, Loader2, Send, User } from 'lucide-react'
import { PageContainer, PageHeader } from '../../components/shared/Page'
import { MockBadge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { api } from '../../lib/api'
import { cn } from '../../lib/utils'
import { errorText, tm } from '../../i18n/helpers'
import type { AssistantMessage } from '../../types'

const suggestionKeys = ['cause', 'balance', 'scenario', 'review'] as const

/** Assistant messages are keys + neutral params, so they re-translate when the language changes. */
const welcome: AssistantMessage = { id: 'welcome', role: 'assistant', parts: [{ key: 'assistant.welcome' }] }

export default function AssistantPage() {
  const { t } = useTranslation()
  const [messages, setMessages] = useState<AssistantMessage[]>([welcome])
  const [input, setInput] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  const ask = useMutation({
    mutationFn: api.askAssistant,
    onSuccess: (m) => setMessages((all) => [...all, m]),
    onError: (e: Error) =>
      setMessages((all) => [
        ...all,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          parts: [{ key: 'assistant.error', params: { reason: errorText(t, e) } }],
        },
      ]),
  })

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, ask.isPending])

  const send = (text: string) => {
    const q = text.trim()
    if (!q || ask.isPending) return
    setMessages((all) => [...all, { id: crypto.randomUUID(), role: 'user', text: q }])
    setInput('')
    ask.mutate(q)
  }

  return (
    <PageContainer>
      <PageHeader title={t('assistant.title')} description={t('assistant.description')} actions={<MockBadge label={t('assistant.demoReplies')} />} />

      <div className="card flex flex-col h-[calc(100vh-290px)] min-h-[420px]">
        <div className="flex-1 overflow-y-auto p-4 sm:p-5 flex flex-col gap-4" aria-live="polite">
          {messages.map((m) => (
            <div key={m.id} className={cn('flex gap-3 max-w-3xl', m.role === 'user' && 'self-end flex-row-reverse')}>
              <span className={cn('grid place-items-center size-8 rounded-full shrink-0', m.role === 'user' ? 'bg-ink text-surface' : 'bg-primary-soft text-primary')}>
                {m.role === 'user' ? <User size={16} /> : <Bot size={16} />}
              </span>
              <div className={cn('rounded-card px-4 py-2.5 text-[15px]', m.role === 'user' ? 'bg-primary text-on-primary' : 'bg-app border border-line')}>
                <p>{m.role === 'user' ? m.text : m.parts?.map((p) => tm(t, p.key, p.params)).join(' ')}</p>
                {m.basedOn && (
                  <p className="text-xs text-muted mt-1.5">{t('assistant.sourceLabel', { sources: m.basedOn.map((k) => t(k)).join(', ') })}</p>
                )}
              </div>
            </div>
          ))}
          {ask.isPending && (
            <div className="flex items-center gap-2 text-sm text-muted" role="status">
              <Loader2 size={16} className="spin" /> {t('assistant.thinking')}
            </div>
          )}
          <div ref={endRef} />
        </div>

        <div className="border-t border-line p-3 sm:p-4 flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {suggestionKeys.map((k) => {
              const text = t(`assistant.suggestions.${k}`)
              return (
                <button
                  key={k}
                  onClick={() => send(text)}
                  disabled={ask.isPending}
                  className="rounded-full border border-line bg-surface px-3 py-1.5 text-[13px] enabled:hover:bg-primary-soft enabled:hover:border-primary cursor-pointer disabled:opacity-50"
                >
                  {text}
                </button>
              )
            })}
          </div>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              send(input)
            }}
          >
            <input className="control" aria-label={t('assistant.inputAria')} placeholder={t('assistant.inputPlaceholder')} value={input} onChange={(e) => setInput(e.target.value)} />
            <Button type="submit" variant="primary" disabled={!input.trim() || ask.isPending} aria-label={t('assistant.send')}>
              <Send size={16} />
              <span className="hidden sm:inline">{t('assistant.send')}</span>
            </Button>
          </form>
        </div>
      </div>
    </PageContainer>
  )
}
