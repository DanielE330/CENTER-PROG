import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ListOrdered } from 'lucide-react'
import { Card } from '../../components/shared/Page'
import { Button } from '../../components/ui/Button'
import type { ForecastResult } from '../../types'

/** How the forecast was built: the model's step-by-step log with the numbers of this run (forecast/src/model.rs, `trace`). */
export function TraceCard({ r }: { r: ForecastResult }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const steps = r.trace ?? []
  if (steps.length === 0) return null
  return (
    <Card
      title={t('forecast.trace.title')}
      subtitle={t('forecast.trace.subtitle', { model: r.model_version, paths: r.n_paths })}
      action={<Button size="sm" onClick={() => setOpen((v) => !v)}><ListOrdered size={15} aria-hidden /> {open ? t('forecast.trace.hide') : t('forecast.trace.show')}</Button>}
    >
      {open && (
        <ol className="px-5 pb-5 flex flex-col gap-2">
          {steps.map((s) => (
            <li key={s.step} className="flex gap-3 rounded-lg border border-line px-3 py-2.5">
              <span className="grid place-items-center size-6 rounded-full bg-primary-soft text-primary text-xs font-semibold shrink-0">{s.step}</span>
              <div className="min-w-0">
                <p className="text-sm font-semibold">{s.title}</p>
                <p className="text-[13px] text-muted mt-0.5">{s.detail}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  )
}
