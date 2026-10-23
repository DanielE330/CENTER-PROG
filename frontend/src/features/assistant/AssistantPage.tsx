import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Bot, Loader2, Send, User } from 'lucide-react'
import { PageContainer, PageHeader } from '../../components/shared/Page'
import { MockBadge } from '../../components/shared/StatusBadge'
import { Button } from '../../components/ui/Button'
import { api } from '../../lib/api'
import { cn } from '../../lib/utils'
import type { AssistantMessage } from '../../types'

const suggestions = [
  'Почему возможна нехватка средств?',
  'Какой сейчас остаток и минимальный прогноз?',
  'Как смоделировать сценарий с переносом платежа?',
  'Какие платежи или поступления стоит проверить?',
]

const welcome: AssistantMessage = {
  id: 'welcome',
  role: 'assistant',
  text: 'Здравствуйте! Я объясняю данные, которые есть в приложении: прогноз остатка, риск нехватки, платежи и поступления. Я не придумываю показатели. Сейчас отвечаю по заготовленным шаблонам — AI-сервис не подключён.',
}

export default function AssistantPage() {
  const [messages, setMessages] = useState<AssistantMessage[]>([welcome])
  const [input, setInput] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  const ask = useMutation({
    mutationFn: api.askAssistant,
    onSuccess: (m) => setMessages((all) => [...all, m]),
    onError: (e: Error) =>
      setMessages((all) => [...all, { id: crypto.randomUUID(), role: 'assistant', text: `Не удалось получить ответ: ${e.message}` }]),
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
      <PageHeader
        title="AI-ассистент"
        description="Помогает разобраться в прогнозе, причинах нехватки и сценариях."
        actions={<MockBadge label="Демо-ответы" />}
      />

      <div className="card flex flex-col h-[calc(100vh-290px)] min-h-[420px]">
        <div className="flex-1 overflow-y-auto p-4 sm:p-5 flex flex-col gap-4" aria-live="polite">
          {messages.map((m) => (
            <div key={m.id} className={cn('flex gap-3 max-w-3xl', m.role === 'user' && 'self-end flex-row-reverse')}>
              <span className={cn('grid place-items-center size-8 rounded-full shrink-0', m.role === 'user' ? 'bg-ink text-white' : 'bg-primary-soft text-primary')}>
                {m.role === 'user' ? <User size={16} /> : <Bot size={16} />}
              </span>
              <div className={cn('rounded-card px-4 py-2.5 text-[15px]', m.role === 'user' ? 'bg-primary text-white' : 'bg-app border border-line')}>
                <p>{m.text}</p>
                {m.basedOn && (
                  <p className="text-xs text-muted mt-1.5">Источник: {m.basedOn.join(', ')} (демо-данные)</p>
                )}
              </div>
            </div>
          ))}
          {ask.isPending && (
            <div className="flex items-center gap-2 text-sm text-muted" role="status">
              <Loader2 size={16} className="spin" /> Ассистент анализирует данные…
            </div>
          )}
          <div ref={endRef} />
        </div>

        <div className="border-t border-line p-3 sm:p-4 flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {suggestions.map((s) => (
              <button
                key={s}
                onClick={() => send(s)}
                disabled={ask.isPending}
                className="rounded-full border border-line bg-surface px-3 py-1.5 text-[13px] hover:bg-primary-soft hover:border-primary cursor-pointer disabled:opacity-50"
              >
                {s}
              </button>
            ))}
          </div>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              send(input)
            }}
          >
            <input className="control" aria-label="Вопрос ассистенту" placeholder="Задайте вопрос о прогнозе…" value={input} onChange={(e) => setInput(e.target.value)} />
            <Button type="submit" variant="primary" disabled={!input.trim() || ask.isPending} aria-label="Отправить">
              <Send size={16} />
              <span className="hidden sm:inline">Отправить</span>
            </Button>
          </form>
        </div>
      </div>
    </PageContainer>
  )
}
