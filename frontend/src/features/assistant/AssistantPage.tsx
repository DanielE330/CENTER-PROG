import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutationState } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Bot, CheckCircle2, ClipboardList, Loader2, MessageSquare, Plus, Send, Settings2, Trash2, User } from 'lucide-react'
import { PageContainer, PageHeader } from '../../components/shared/Page'
import { EmptyState, ErrorState, LoadingState } from '../../components/shared/States'
import { Badge } from '../../components/shared/StatusBadge'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Modal } from '../../components/ui/Modal'
import { Select } from '../../components/ui/Select'
import { useAuth } from '../../app/auth'
import {
  sendKey, useAdvisorChat, useAdvisorChats, useAdvisorModels, useAdvisorPrompts, useCreateChat, useDeleteChat, useForecast,
  usePatchChat, useScenario, useSendChat,
} from '../../hooks/queries'
import { ApiError } from '../../lib/errors'
import { formatDateTime } from '../../lib/formatters'
import { cn } from '../../lib/utils'
import { errorText, tm } from '../../i18n/helpers'
import { classify, localAnswer, scenarioAnswer } from './answers'
import { SuggestionCard } from './SuggestionCard'
import type { AdvisorChat, ChatMessageMeta, ChatMode, Reasoning, StoredMessage } from './advisorTypes'

const EFFORTS: Reasoning[] = ['low', 'medium', 'high', 'xhigh']
const LAST_CHAT = 'ci.assistant.lastChat'

function remember(id: string | null) {
  try {
    if (id) localStorage.setItem(LAST_CHAT, id)
    else localStorage.removeItem(LAST_CHAT)
  } catch {
    /* storage off: nothing to remember */
  }
}

function recalled(): string | null {
  try {
    return localStorage.getItem(LAST_CHAT)
  } catch {
    return null
  }
}

/** A message on screen: saved on the server, or local (rule answer when the advisor is unreachable, an error). */
type Shown = { id: string; role: 'user' | 'assistant'; text: string; meta?: ChatMessageMeta; local?: 'rules' | 'error' }

/** Banner over a template/rule answer: why the model did not answer. */
function ProviderNotice({ m }: { m: Shown }) {
  const { t } = useTranslation()
  let kind: 'noProvider' | 'providerDown' | 'budget' | 'advisorDown' | null = null
  if (m.local === 'rules') kind = 'advisorDown'
  else if (m.meta?.provider === 'none') {
    kind = m.meta.notice === 'provider_unavailable' ? 'providerDown' : m.meta.notice === 'budget_exceeded' ? 'budget' : 'noProvider'
  }
  if (!kind) return null
  return (
    <Notice tone={kind === 'noProvider' ? 'info' : 'warning'} title={t(`assistant.${kind}Title`)} className="mb-2">
      {t(`assistant.${kind}Text`)}
    </Notice>
  )
}

export default function AssistantPage() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()
  const chats = useAdvisorChats()
  const del = useDeleteChat()
  const [creating, setCreating] = useState<ChatMode | null>(null)
  const [deleting, setDeleting] = useState<AdvisorChat | null>(null)
  const items = chats.data?.items ?? []
  const disabled = chats.error instanceof ApiError && chats.error.code === 'advisor_disabled'
  const selected = params.get('chat')

  const open = (id: string | null) => {
    remember(id)
    setParams(id ? { chat: id } : {}, { replace: false })
  }

  // ?new=plan (button on Plan vs fact) opens the new chat dialog; without ?chat= — the last opened chat
  useEffect(() => {
    const n = params.get('new')
    if (n === 'plan' || n === 'chat') {
      setCreating(n)
      setParams({}, { replace: true })
    } else if (!selected && chats.data) {
      const last = recalled()
      const pick = items.find((c) => c.id === last) ?? items[0]
      if (pick) setParams({ chat: pick.id }, { replace: true })
    }
  }, [params, selected, chats.data]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <PageContainer>
      <PageHeader title={t('assistant.title')} description={t('assistant.description')} />
      {disabled && <DisabledNotice />}
      {!disabled && (
        <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="card flex flex-col max-h-[calc(100vh-240px)] min-h-[200px]" aria-label={t('assistant.chatsAria')}>
            <div className="p-3 flex gap-2 border-b border-line">
              <Button variant="primary" className="flex-1" onClick={() => setCreating('chat')}>
                <Plus size={16} aria-hidden /> {t('assistant.newChat')}
              </Button>
              <Button onClick={() => setCreating('plan')} title={t('assistant.newPlan')} aria-label={t('assistant.newPlan')}>
                <ClipboardList size={16} aria-hidden />
              </Button>
            </div>
            {chats.isPending && <LoadingState height={120} />}
            {chats.isError && <ErrorState message={errorText(t, chats.error)} onRetry={() => void chats.refetch()} />}
            {chats.data && items.length === 0 && <p className="p-4 text-sm text-muted">{t('assistant.noChats')}</p>}
            <ul className="overflow-y-auto p-2 flex flex-col gap-1">
              {items.map((c) => (
                <li key={c.id} className="group relative">
                  <button
                    type="button"
                    onClick={() => open(c.id)}
                    aria-current={c.id === selected ? 'page' : undefined}
                    className={cn(
                      'w-full text-left rounded-lg px-3 py-2 pr-9 cursor-pointer flex gap-2 items-start',
                      c.id === selected ? 'bg-primary-soft text-primary' : 'hover:bg-app',
                    )}
                  >
                    {c.mode === 'plan' ? <ClipboardList size={16} className="shrink-0 mt-0.5" aria-hidden /> : <MessageSquare size={16} className="shrink-0 mt-0.5" aria-hidden />}
                    <span className="min-w-0">
                      <span className="block text-sm font-medium truncate">{c.title}</span>
                      <span className="block text-xs text-muted">{c.mode === 'plan' ? t('assistant.modePlan') : t('assistant.modeChat')} · {formatDateTime(c.updated_at)}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleting(c)}
                    aria-label={t('assistant.deleteChat', { title: c.title })}
                    className="absolute right-1.5 top-2 p-1.5 rounded-md text-muted hover:text-danger hover:bg-surface cursor-pointer opacity-100 lg:opacity-0 lg:group-hover:opacity-100 focus:opacity-100"
                  >
                    <Trash2 size={15} aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </aside>

          {selected ? (
            <ChatView key={selected} id={selected} onGone={() => open(null)} />
          ) : (
            <div className="card">
              <EmptyState
                icon={<Bot size={22} aria-hidden />}
                title={t('assistant.startTitle')}
                description={t('assistant.startText')}
                action={<Button variant="primary" onClick={() => setCreating('chat')}><Plus size={16} aria-hidden /> {t('assistant.newChat')}</Button>}
              />
            </div>
          )}
        </div>
      )}

      {creating && <NewChatDialog mode={creating} onClose={() => setCreating(null)} onCreated={(c) => { setCreating(null); open(c.id) }} />}
      <Modal open={!!deleting} title={t('assistant.deleteTitle')} onClose={() => setDeleting(null)}>
        <p className="text-sm">{t('assistant.deleteText', { title: deleting?.title ?? '' })}</p>
        {del.isError && <Notice tone="danger" className="mt-3">{errorText(t, del.error)}</Notice>}
        <div className="flex justify-end gap-2 mt-5">
          <Button onClick={() => setDeleting(null)}>{t('common.cancel')}</Button>
          <Button
            variant="danger"
            disabled={del.isPending}
            onClick={() => {
              const c = deleting
              if (!c) return
              del.mutate(c.id, {
                onSuccess: () => {
                  setDeleting(null)
                  if (c.id === selected) open(null)
                },
              })
            }}
          >
            {t('common.delete')}
          </Button>
        </div>
      </Modal>
    </PageContainer>
  )
}

function DisabledNotice() {
  const { t } = useTranslation()
  return (
    <Notice tone="info" title={t('assistant.disabledTitle')}>
      {t('assistant.disabledText')}{' '}
      <Link to="/settings#advisor" className="text-primary font-medium hover:underline inline-flex items-center gap-1"><Settings2 size={14} aria-hidden />{t('assistant.openSettings')}</Link>
    </Notice>
  )
}

/** New chat: mode (consultant or plan) and reasoning depth are chosen once, the model can be changed later at the input. */
function NewChatDialog({ mode: initial, onClose, onCreated }: { mode: ChatMode; onClose: () => void; onCreated: (c: AdvisorChat) => void }) {
  const { t } = useTranslation()
  const models = useAdvisorModels()
  const create = useCreateChat()
  const [mode, setMode] = useState<ChatMode>(initial)
  const [reasoning, setReasoning] = useState<Reasoning>(initial === 'plan' ? 'high' : 'medium')
  const [model, setModel] = useState('')
  const list = models.data?.models ?? []
  const efforts = models.data?.efforts.length ? models.data.efforts : EFFORTS

  return (
    <Modal open title={t('assistant.newTitle')} onClose={onClose}>
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault()
          create.mutate({ mode, reasoning, model: model || undefined }, { onSuccess: onCreated })
        }}
      >
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-semibold mb-2">{t('assistant.modeLabel')}</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {(['chat', 'plan'] as ChatMode[]).map((m) => (
              <label key={m} className={cn('rounded-card border px-3 py-2.5 cursor-pointer flex gap-2.5 items-start', mode === m ? 'border-primary bg-primary-soft' : 'border-line hover:bg-app')}>
                <input type="radio" name="mode" className="mt-1 accent-[var(--color-primary)]" checked={mode === m} onChange={() => setMode(m)} />
                <span>
                  <span className="block text-sm font-medium">{m === 'plan' ? t('assistant.modePlan') : t('assistant.modeChat')}</span>
                  <span className="block text-xs text-muted">{m === 'plan' ? t('assistant.modePlanHint') : t('assistant.modeChatHint')}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-semibold mb-2">{t('assistant.reasoningLabel')}</legend>
          <div className="grid gap-2 grid-cols-2 sm:grid-cols-4">
            {efforts.map((r) => (
              <label key={r} className={cn('rounded-card border px-3 py-2 cursor-pointer text-center', reasoning === r ? 'border-primary bg-primary-soft' : 'border-line hover:bg-app')}>
                <input type="radio" name="reasoning" className="sr-only" checked={reasoning === r} onChange={() => setReasoning(r)} />
                <span className="block text-sm font-medium">{t(`assistant.effort.${r}`)}</span>
                <span className="block text-[11px] text-muted leading-tight mt-0.5">{t(`assistant.effortHint.${r}`)}</span>
              </label>
            ))}
          </div>
          <p className="text-xs text-muted">{t('assistant.reasoningHint')}</p>
        </fieldset>
        {list.length > 0 && (
          <Select
            label={t('assistant.modelLabel')}
            value={model}
            onChange={setModel}
            options={[{ value: '', label: t('assistant.modelDefault', { model: models.data?.default_model ?? '' }) }, ...list.map((m) => ({ value: m, label: m }))]}
          />
        )}
        {create.isError && <Notice tone="danger">{errorText(t, create.error)}</Notice>}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>{t('assistant.start')}</Button>
        </div>
      </form>
    </Modal>
  )
}

function ChatView({ id, onGone }: { id: string; onGone: () => void }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language.startsWith('en') ? 'en' : 'ru'
  const { me } = useAuth()
  const q = useAdvisorChat(id)
  const models = useAdvisorModels()
  const patch = usePatchChat()
  const send = useSendChat()
  const forecast = useForecast()
  const scenario = useScenario()
  const chat = q.data?.chat
  const prompts = useAdvisorPrompts(lang, chat?.mode ?? 'chat')
  const [input, setInput] = useState('')
  const [local, setLocal] = useState<Shown[]>([])
  const [disabled, setDisabled] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  // questions still being answered for this chat — survive leaving the page (mutation cache)
  const pending = useMutationState({
    filters: { mutationKey: sendKey(me?.company_id ?? ''), status: 'pending' },
    select: (m) => m.state.variables as { chatId: string; question: string } | undefined,
  }).filter((v): v is { chatId: string; question: string } => !!v && v.chatId === id)
  const busy = pending.length > 0

  const saved: Shown[] = (q.data?.messages ?? []).map((m: StoredMessage) => ({ id: String(m.id), role: m.role, text: m.content, meta: m.meta }))
  // the question is saved on the server before the answer; show it once even if the refetch already has it
  const lastSaved = saved[saved.length - 1]
  const waiting: Shown[] = pending
    .filter((p) => !(lastSaved?.role === 'user' && lastSaved.text === p.question.trim()))
    .map((p, i) => ({ id: `pending-${i}`, role: 'user', text: p.question }))
  const shown = [...saved, ...waiting, ...local]

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [shown.length, busy])

  useEffect(() => {
    if (q.error instanceof ApiError && q.error.status === 404) onGone()
  }, [q.error]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Rule answer from the saved forecast: only when the advisor service is unreachable. Not saved in the chat. */
  async function ruleAnswer(question: string): Promise<Shown | null> {
    const v = forecast.view
    if (!v) return null
    const { intent, days } = classify(question)
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
    const source = { key: 'assistant.source', params: { asOfDate: v.r.as_of, id: v.run.run_id.slice(0, 8) } }
    return { id: crypto.randomUUID(), role: 'assistant', local: 'rules', text: [...parts, ...(intent === 'help' ? [] : [source])].map((p) => tm(t, p.key, p.params)).join('\n') }
  }

  function ask(text: string) {
    const question = text.trim()
    if (!question || busy) return
    setInput('')
    setLocal([])
    send.mutate(
      { chatId: id, question, lang },
      {
        onError: async (e) => {
          if (e instanceof ApiError && e.code === 'advisor_disabled') {
            setDisabled(true)
            return
          }
          const rules = await ruleAnswer(question).catch(() => null)
          setLocal([{ id: crypto.randomUUID(), role: 'user', text: question }, rules ?? { id: crypto.randomUUID(), role: 'assistant', local: 'error', text: t('assistant.error', { reason: errorText(t, e) }) }])
        },
      },
    )
  }

  if (q.isPending) return <div className="card"><LoadingState height={360} /></div>
  if (q.isError || !chat) return <div className="card"><ErrorState message={errorText(t, q.error)} onRetry={() => void q.refetch()} /></div>

  const last = [...saved].reverse().find((m) => m.role === 'assistant')
  const followups = saved.length > 0 ? last?.meta?.followups ?? [] : (prompts.data?.items ?? []).map((p) => p.text)
  const list = models.data?.models ?? []
  const current = chat.model || models.data?.default_model || ''

  return (
    <section className="card flex flex-col h-[calc(100vh-240px)] min-h-[460px]" aria-label={chat.title}>
      <header className="px-4 sm:px-5 py-3 border-b border-line flex flex-wrap items-center gap-2">
        <h2 className="font-semibold text-[15px] truncate min-w-0 flex-1">{chat.title}</h2>
        <Badge tone={chat.mode === 'plan' ? 'success' : 'neutral'}>{chat.mode === 'plan' ? t('assistant.modePlan') : t('assistant.modeChat')}</Badge>
        <Badge>{t('assistant.reasoningBadge', { level: t(`assistant.effort.${chat.reasoning}`) })}</Badge>
      </header>

      <div className="flex-1 overflow-y-auto p-4 sm:p-5 flex flex-col gap-4" role="log" aria-live="polite" aria-label={t('assistant.logAria')}>
        {shown.length === 0 && (
          <Bubble role="assistant">{chat.mode === 'plan' ? t('assistant.welcomePlan') : t('assistant.welcome')}</Bubble>
        )}
        {shown.map((m) => (
          <Bubble key={m.id} role={m.role}>
            {m.role === 'user' ? m.text : (
              <>
                <ProviderNotice m={m} />
                {m.text}
                {m.meta?.plan_id && (
                  <Link to="/plan-vs-actual#strategy" className="mt-3 flex items-center gap-2 rounded-lg border border-primary bg-surface px-3 py-2 text-[13px] whitespace-normal text-primary font-medium hover:bg-primary-soft">
                    <ClipboardList size={16} aria-hidden /> {t('assistant.planSaved')}
                  </Link>
                )}
                {m.meta?.actions && m.meta.actions.length > 0 && (
                  <ul className="flex flex-col gap-1.5 mt-3 whitespace-normal" aria-label={t('assistant.actionsAria')}>
                    {m.meta.actions.map((a) => (
                      <li key={`${a.type}-${a.id}`} className="flex items-start gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-[13px]">
                        <CheckCircle2 size={16} className="text-primary shrink-0 mt-0.5" aria-hidden />
                        <span><span className="font-medium">{t('assistant.done')}</span> {a.title}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {m.meta?.suggestions && m.meta.suggestions.length > 0 && (
                  <div className="flex flex-col gap-2 mt-3 whitespace-normal" role="group" aria-label={t('assistant.proposalsAria')}>
                    {m.meta.suggestions.map((s) => <SuggestionCard key={s.id} s={s} />)}
                  </div>
                )}
                {m.meta?.model && m.meta.provider !== 'none' && (
                  <p className="mt-2 text-[11px] text-muted whitespace-normal">{m.meta.model}{m.meta.reasoning ? ` · ${t(`assistant.effort.${m.meta.reasoning}`)}` : ''}</p>
                )}
              </>
            )}
          </Bubble>
        ))}
        {busy && (
          <div className="flex items-center gap-2 text-sm text-muted" role="status">
            <Loader2 size={16} className="spin" aria-hidden /> {chat.mode === 'plan' ? t('assistant.thinkingPlan') : t('assistant.thinking')}
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="border-t border-line p-3 sm:p-4 flex flex-col gap-3">
        {(disabled || (prompts.error instanceof ApiError && prompts.error.code === 'advisor_disabled')) && <DisabledNotice />}
        {followups.length > 0 && !busy && (
          <ul className="flex flex-wrap gap-2" aria-label={saved.length > 0 ? t('assistant.followupsAria') : t('assistant.suggestionsAria')}>
            {followups.map((text) => (
              <li key={text}>
                <button
                  type="button"
                  onClick={() => ask(text)}
                  className="rounded-full border border-line bg-surface px-3 py-1.5 text-[13px] text-left hover:bg-primary-soft hover:border-primary cursor-pointer"
                >
                  {text}
                </button>
              </li>
            ))}
          </ul>
        )}
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            ask(input)
          }}
        >
          <textarea
            className="control min-h-[44px] max-h-40 resize-y py-2.5"
            rows={2}
            aria-label={t('assistant.inputAria')}
            placeholder={chat.mode === 'plan' ? t('assistant.inputPlaceholderPlan') : t('assistant.inputPlaceholder')}
            maxLength={4000}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                ask(input)
              }
            }}
          />
          <div className="flex items-center gap-2">
            {list.length > 0 && (
              <div className="w-44 sm:w-52">
                <Select
                  aria-label={t('assistant.modelLabel')}
                  value={current}
                  disabled={patch.isPending || busy}
                  onChange={(m) => patch.mutate({ id, model: m })}
                  options={list.map((m) => ({ value: m, label: m }))}
                />
              </div>
            )}
            <span className="text-xs text-muted hidden sm:inline">{t('assistant.enterHint')}</span>
            <Button type="submit" variant="primary" className="ml-auto" disabled={!input.trim() || busy} aria-label={t('assistant.send')}>
              <Send size={16} aria-hidden />
              <span className="hidden sm:inline">{t('assistant.send')}</span>
            </Button>
          </div>
          {patch.isError && <p className="text-xs text-danger">{errorText(t, patch.error)}</p>}
        </form>
      </div>
    </section>
  )
}

function Bubble({ role, children }: { role: 'user' | 'assistant'; children: React.ReactNode }) {
  const { t } = useTranslation()
  return (
    <div className={cn('flex gap-3 max-w-3xl', role === 'user' && 'self-end flex-row-reverse')}>
      <span className={cn('grid place-items-center size-8 rounded-full shrink-0', role === 'user' ? 'bg-ink text-surface' : 'bg-primary-soft text-primary')} aria-hidden>
        {role === 'user' ? <User size={16} /> : <Bot size={16} />}
      </span>
      <div className={cn('rounded-card px-4 py-2.5 text-[15px] whitespace-pre-line min-w-0 break-words', role === 'user' ? 'bg-primary text-on-primary' : 'bg-app border border-line')}>
        <span className="sr-only">{role === 'user' ? t('assistant.you') : t('assistant.bot')}: </span>
        {children}
      </div>
    </div>
  )
}
