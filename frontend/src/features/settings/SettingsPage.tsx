import { useState } from 'react'
import { Check } from 'lucide-react'
import { PageContainer, PageHeader, Card } from '../../components/shared/Page'
import { Button } from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Input'
import { setDemoState, useDemoState } from '../../lib/demoState'
import type { DemoState } from '../../types'
import { ImportStatement } from './ImportStatement'

const KEY = 'ci.settings'
interface Prefs {
  org: string
  horizon: string
  notify: boolean
}
const defaults: Prefs = { org: 'ООО «Южный Агрохолдинг»', horizon: '30', notify: true }

function load(): Prefs {
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') }
  } catch {
    return defaults
  }
}

const demoOptions: Array<{ value: DemoState; label: string }> = [
  { value: 'normal', label: 'Обычные данные' },
  { value: 'loading', label: 'Долгая загрузка' },
  { value: 'error', label: 'Ошибка загрузки' },
  { value: 'empty', label: 'Нет данных' },
  { value: 'incomplete', label: 'Неполные данные' },
]

export default function SettingsPage() {
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
      <PageHeader title="Настройки" description="Параметры приложения и организации, импорт выписки." />

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="flex flex-col gap-5">
          <Card title="Организация и приложение" subtitle="Сохраняется только в этом браузере.">
            <div className="px-5 pb-5 flex flex-col gap-4">
              <Input label="Название организации" value={prefs.org} onChange={(e) => setPrefs({ ...prefs, org: e.target.value })} />
              <Select label="Валюта" disabled hint="В прототипе поддерживаются только рубли." value="RUB" options={[{ value: 'RUB', label: 'Российский рубль (RUB)' }]} />
              <Select
                label="Горизонт прогноза по умолчанию"
                value={prefs.horizon}
                onChange={(e) => setPrefs({ ...prefs, horizon: e.target.value })}
                options={[{ value: '14', label: '14 дней' }, { value: '30', label: '30 дней' }]}
              />
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={prefs.notify} onChange={(e) => setPrefs({ ...prefs, notify: e.target.checked })} />
                Уведомлять о риске нехватки средств
              </label>
              <div className="flex items-center gap-3">
                <Button variant="primary" onClick={save}>Сохранить</Button>
                {saved && (
                  <span className="inline-flex items-center gap-1 text-sm text-primary" role="status">
                    <Check size={16} /> Сохранено
                  </span>
                )}
              </div>
            </div>
          </Card>

          <Card title="Демонстрация состояний данных" subtitle="Только для прототипа: принудительно показывает состояния интерфейса.">
            <div className="px-5 pb-5">
              <Select
                label="Состояние данных"
                value={demo}
                onChange={(e) => setDemoState(e.target.value as DemoState)}
                options={demoOptions}
                hint="Применяется ко всем страницам. Для реального API не используется."
              />
            </div>
          </Card>
        </div>

        <Card title="Импорт банковской выписки" subtitle="Симуляция интерфейса — без загрузки на сервер.">
          <div className="px-5 pb-5">
            <ImportStatement />
          </div>
        </Card>
      </div>
    </PageContainer>
  )
}
