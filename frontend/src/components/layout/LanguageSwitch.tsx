import { useTranslation } from 'react-i18next'
import { Check, Globe } from 'lucide-react'
import { LANGUAGES, setLanguage } from '../../i18n'
import type { Language } from '../../i18n'
import { cn } from '../../lib/utils'

/** Language names are shown in their own language so they stay recognisable. */
export const languageNames: Record<Language, string> = { ru: 'Русский', en: 'English' }

export function LanguageSwitch({ compact = false }: { compact?: boolean }) {
  const { t, i18n } = useTranslation()
  return (
    <div role="radiogroup" aria-label={t('settings.language.title')} className={cn('grid gap-2', compact ? 'grid-cols-2' : 'grid-cols-2')}>
      {LANGUAGES.map((lng) => {
        const active = i18n.language === lng
        return (
          <button
            key={lng}
            type="button"
            role="radio"
            aria-checked={active}
            lang={lng}
            onClick={() => setLanguage(lng)}
            className={cn(
              'flex items-center justify-center gap-2 rounded-control border text-sm font-medium cursor-pointer',
              compact ? 'h-9 px-3' : 'h-10',
              active ? 'border-primary bg-primary-soft text-primary' : 'border-line bg-surface hover:bg-hover',
            )}
          >
            {active ? <Check size={16} aria-hidden /> : <Globe size={16} className="text-muted" aria-hidden />}
            {compact ? lng.toUpperCase() : languageNames[lng]}
          </button>
        )
      })}
    </div>
  )
}
