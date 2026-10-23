import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Check, Globe } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { Button } from '../../components/ui/Button'
import { Checkbox } from '../../components/ui/Checkbox'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { setDemoState, useDemoState } from '../../lib/demoState'
import { LANGUAGES, setLanguage } from '../../i18n'
import type { Language } from '../../i18n'
import { cn } from '../../lib/utils'
import type { DemoState } from '../../types'
import { ImportStatement } from './ImportStatement'

const KEY = 'ci.settings'
interface Prefs {
  /** null until the user edits it, so the default follows the active language. */
  org: string | null
  horizon: string
  notify: boolean
}
const defaults: Prefs = { org: null, horizon: '30', notify: true }

function load(): Prefs {
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') }
  } catch {
    return defaults
  }
}

const demoStates: DemoState[] = ['normal', 'loading', 'error', 'empty', 'incomplete']

/** Language names are shown in their own language so they stay recognisable. */
const languageNames: Record<Language, string> = { ru: 'Русский', en: 'English' }

export default function SettingsPage() {
  const { t, i18n } = useTranslation()
  const [prefs, setPrefs] = useState<Prefs>(load)
  const [saved, setSaved] = useState(false)
  const demo = useDemoState()

  const save = () => {
    try {
      localStorage.setItem(KEY, JSON.stringify(prefs))
    } catch {
      /* storage unavailable */
    }
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <PageContainer>
      <PageHeader title={t('settings.title')} description={t('settings.description')} />

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="flex flex-col gap-5">
          <Card title={t('settings.language.title')} subtitle={t('settings.language.subtitle')}>
            <div className="px-5 pb-5">
              <div role="radiogroup" aria-label={t('settings.language.title')} className="grid grid-cols-2 gap-2">
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
                        'flex h-10 items-center justify-center gap-2 rounded-control border text-sm font-medium cursor-pointer',
                        active ? 'border-primary bg-primary-soft text-primary' : 'border-line bg-surface hover:bg-hover',
                      )}
                    >
                      {active ? <Check size={16} /> : <Globe size={16} className="text-muted" />}
                      {languageNames[lng]}
                    </button>
                  )
                })}
              </div>
            </div>
          </Card>

          <Card title={t('settings.org.title')} subtitle={t('settings.org.subtitle')}>
            <div className="px-5 pb-5 flex flex-col gap-4">
              <Input label={t('settings.org.name')} value={prefs.org ?? t('app.org')} onChange={(e) => setPrefs({ ...prefs, org: e.target.value })} />
              <Select label={t('settings.org.currency')} disabled onChange={() => {}} hint={t('settings.org.currencyHint')} value="RUB" options={[{ value: 'RUB', label: t('settings.org.rub') }]} />
              <Select
                label={t('settings.org.horizon')}
                value={prefs.horizon}
                onChange={(v) => setPrefs({ ...prefs, horizon: v })}
                options={[14, 30].map((d) => ({ value: String(d), label: t('common.daysCount', { count: d }) }))}
              />
              <Checkbox checked={prefs.notify} onChange={(e) => setPrefs({ ...prefs, notify: e.target.checked })} label={t('settings.org.notify')} />
              <div className="flex items-center gap-3">
                <Button variant="primary" onClick={save}>{t('common.save')}</Button>
                {saved && (
                  <span className="inline-flex items-center gap-1 text-sm text-primary" role="status">
                    <Check size={16} /> {t('common.saved')}
                  </span>
                )}
              </div>
            </div>
          </Card>

          <Card title={t('settings.demo.title')} subtitle={t('settings.demo.subtitle')}>
            <div className="px-5 pb-5">
              <Select
                label={t('settings.demo.label')}
                value={demo}
                onChange={(v) => setDemoState(v as DemoState)}
                options={demoStates.map((s) => ({ value: s, label: t(`settings.demo.options.${s}`) }))}
                hint={t('settings.demo.hint')}
              />
            </div>
          </Card>
        </div>

        <Card title={t('import.title')} subtitle={t('import.subtitle')}>
          <div className="px-5 pb-5">
            <ImportStatement />
          </div>
        </Card>
      </div>
    </PageContainer>
  )
}
