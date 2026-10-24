import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, Copy, KeyRound, Loader2, Plus, Trash2 } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { ErrorState, LoadingState } from '../../components/shared/States'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/formatters'
import { errorText } from '../../i18n/helpers'
import type { NewApiToken } from '../../types'

const KEY = ['account', 'tokens'] as const
const iso = (sec: number) => new Date(sec * 1000).toISOString()

/** Tokens for the external API (`/api/ext/v1`): the token identifies the company, so no company id is needed. */
export function ApiTokens() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const list = useQuery({ queryKey: KEY, queryFn: api.apiTokens })
  const [name, setName] = useState('')
  const [fresh, setFresh] = useState<NewApiToken | null>(null)
  const [copied, setCopied] = useState(false)

  const create = useMutation({
    mutationFn: (n: string) => api.createApiToken(n),
    onSuccess: (tok) => {
      setFresh(tok)
      setName('')
      setCopied(false)
      void qc.invalidateQueries({ queryKey: KEY })
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteApiToken(id),
    onSuccess: (_, id) => {
      if (fresh?.id === id) setFresh(null)
      void qc.invalidateQueries({ queryKey: KEY })
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    create.mutate(name.trim())
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(fresh!.token)
      setCopied(true)
    } catch {
      /* clipboard unavailable: the token is selectable in the field */
    }
  }

  return (
    <Card title={t('apiTokens.title')} subtitle={t('apiTokens.subtitle')}>
      <div className="px-5 pb-5 flex flex-col gap-4">
        {fresh && (
          <Notice tone="success" title={t('apiTokens.createdTitle')}>
            <p className="mb-2">{t('apiTokens.createdText')}</p>
            <div className="flex items-center gap-2">
              <input readOnly value={fresh.token} aria-label={t('apiTokens.tokenAria')} onFocus={(e) => e.currentTarget.select()} className="tnum min-w-0 flex-1 rounded-control border border-line bg-surface px-3 py-2 text-xs" />
              <Button size="sm" onClick={() => void copy()}>
                {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? t('apiTokens.copied') : t('apiTokens.copy')}
              </Button>
            </div>
          </Notice>
        )}

        {list.isPending && <LoadingState height={80} />}
        {list.isError && <ErrorState message={errorText(t, list.error)} onRetry={() => void list.refetch()} />}
        {list.data && (list.data.items.length === 0 ? (
          <p className="text-sm text-muted">{t('apiTokens.empty')}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {list.data.items.map((k) => (
              <li key={k.id} className="rounded-control border border-line px-3.5 py-2.5 flex items-center justify-between gap-3">
                <div className="flex items-center gap-2.5 min-w-0">
                  <KeyRound size={16} className="text-muted shrink-0" aria-hidden />
                  <div className="min-w-0">
                    <p className="text-sm font-medium truncate">{k.name} <span className="tnum text-xs text-muted font-normal">{k.prefix}…</span></p>
                    <p className="text-xs text-muted truncate">
                      {t('apiTokens.created', { value: formatDateTime(iso(k.created_at)) })} · {k.last_used ? t('apiTokens.lastUsed', { value: formatDateTime(iso(k.last_used)) }) : t('apiTokens.neverUsed')}
                    </p>
                  </div>
                </div>
                <Button size="sm" disabled={remove.isPending} onClick={() => remove.mutate(k.id)}>
                  <Trash2 size={14} aria-hidden /> {t('apiTokens.revoke')}
                </Button>
              </li>
            ))}
          </ul>
        ))}

        <form className="flex flex-col sm:flex-row sm:items-end gap-3" onSubmit={submit}>
          <div className="flex-1">
            <Input label={t('apiTokens.name')} placeholder={t('apiTokens.namePlaceholder')} value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
          </div>
          <Button type="submit" variant="primary" disabled={create.isPending || !name.trim()}>
            {create.isPending ? <Loader2 size={16} className="spin" aria-hidden /> : <Plus size={16} aria-hidden />} {t('apiTokens.create')}
          </Button>
        </form>
        {(create.isError || remove.isError) && <Notice tone="danger">{errorText(t, create.error ?? remove.error)}</Notice>}

        <div className="text-xs text-muted">
          <p className="mb-1">{t('apiTokens.usage')}</p>
          <pre className="tnum overflow-x-auto rounded-control bg-hover p-3">{`curl -H "Authorization: Bearer ckt_…" \\\n  ${typeof window === 'undefined' ? '' : window.location.origin}/api/ext/v1/forecast`}</pre>
          <p className="mt-1">{t('apiTokens.endpoints')}</p>
        </div>
      </div>
    </Card>
  )
}
