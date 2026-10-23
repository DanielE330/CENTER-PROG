import { Moon, Sun } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { setTheme, useTheme } from '../../lib/theme'
import { Tooltip } from '../ui/Tooltip'

export function ThemeToggle() {
  const { t } = useTranslation()
  const theme = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  const label = next === 'dark' ? t('theme.toDark') : t('theme.toLight')
  return (
    <Tooltip text={label}>
      <button
        onClick={() => setTheme(next)}
        aria-label={label}
        aria-pressed={theme === 'dark'}
        className="grid place-items-center size-10 rounded-control hover:bg-hover text-muted cursor-pointer"
      >
        {theme === 'dark' ? <Sun size={19} /> : <Moon size={19} />}
      </button>
    </Tooltip>
  )
}
