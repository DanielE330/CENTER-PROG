import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Check, Lightbulb, X } from 'lucide-react'
import { Button } from '../../components/ui/Button'
import { useApplySuggestion } from '../../hooks/queries'
import { errorText } from '../../i18n/helpers'
import { useEditGuard } from '../../app/perms'
import { formatDate, formatKop } from '../../lib/formatters'
import type { AdvisorSuggestion } from './advisorTypes'

type State = 'new' | 'applied' | 'dismissed'

/** A proposal from the advisor. Nothing is written until the user presses «Add». */
export function SuggestionCard({ s }: { s: AdvisorSuggestion }) {
  const { t } = useTranslation()
  const apply = useApplySuggestion()
  const guard = useEditGuard()
  const [state, setState] = useState<State>('new')
  const [error, setError] = useState<string | null>(null)
  const p = s.params
  const signed = p.direction === 'out' ? `-${p.amount_kop}` : p.amount_kop

  async function add() {
    setError(null)
    try {
      await apply.mutateAsync(s)
      setState('applied')
    } catch (e) {
      setError(t('assistant.applyError', { reason: errorText(t, e) }))
    }
  }

  return (
    <div className="rounded-card border border-line bg-surface p-3 text-sm flex flex-col gap-2" data-testid="advisor-suggestion">
      <p className="flex items-center gap-2 font-medium">
        <Lightbulb size={16} className="text-primary shrink-0" aria-hidden />
        {t(p.direction === 'out' ? 'assistant.suggestionAddOut' : 'assistant.suggestionAddIn')}
      </p>
      <p>{t('assistant.suggestionLine', { title: p.title, amount: formatKop(signed), date: formatDate(p.date) })}</p>
      {p.counterparty && <p className="text-muted">{t('assistant.suggestionCounterparty', { name: p.counterparty })}</p>}
      {state === 'new' && (
        <div className="flex gap-2">
          <Button variant="primary" size="sm" onClick={() => void add()} disabled={apply.isPending || guard.lock.disabled} title={guard.lock.title}>
            <Check size={14} aria-hidden /> {t('assistant.add')}
          </Button>
          <Button size="sm" onClick={() => setState('dismissed')} disabled={apply.isPending}>
            <X size={14} aria-hidden /> {t('assistant.dismiss')}
          </Button>
        </div>
      )}
      {state === 'applied' && <p className="text-primary" role="status">{t('assistant.applied')}</p>}
      {state === 'dismissed' && <p className="text-muted">{t('assistant.dismissed')}</p>}
      {error && <p className="text-danger" role="alert">{error}</p>}
    </div>
  )
}
