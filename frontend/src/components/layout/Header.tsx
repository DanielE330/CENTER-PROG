import { useEffect, useRef, useState } from 'react'
import { Bell, Building2, ChevronDown, Menu } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ThemeToggle } from './ThemeToggle'
import { useForecast } from '../../hooks/queries'
import { formatShortDate } from '../../lib/formatters'

export function Header({ onMenu }: { onMenu: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const { data } = useForecast()
  const risk = data?.risk

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <header className="h-[var(--header-h)] shrink-0 bg-surface border-b border-line flex items-center gap-3 px-4 sm:px-6 lg:px-8">
      <button onClick={onMenu} aria-label="Открыть меню" className="md:hidden p-2 -ml-2 rounded-md hover:bg-hover cursor-pointer">
        <Menu size={20} />
      </button>

      <div className="flex items-center gap-2 min-w-0">
        <Building2 size={18} className="text-muted shrink-0" />
        <div className="leading-tight min-w-0">
          <p className="text-sm font-semibold truncate">ООО «Южный Агрохолдинг»</p>
          <p className="text-xs text-muted hidden sm:block">Рабочее пространство · Демо</p>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-1 sm:gap-2">
        <ThemeToggle />
        <div className="relative" ref={ref}>
          <button
            onClick={() => setOpen((v) => !v)}
            aria-label="Уведомления"
            aria-expanded={open}
            className="relative grid place-items-center size-10 rounded-control hover:bg-hover text-muted cursor-pointer"
          >
            <Bell size={19} />
            {risk?.shortageDate && <span className="absolute top-2 right-2.5 size-2 rounded-full bg-danger" />}
          </button>
          {open && (
            <div className="card absolute right-0 top-12 w-80 max-w-[calc(100vw-2rem)] p-3 z-40" style={{ boxShadow: 'var(--shadow-pop)' }}>
              <p className="text-sm font-semibold px-2 pb-2">Уведомления</p>
              {risk?.shortageDate ? (
                <Link to="/forecast" onClick={() => setOpen(false)} className="block rounded-control p-2 hover:bg-hover">
                  <p className="text-sm font-medium text-danger">Возможна нехватка средств</p>
                  <p className="text-xs text-muted mt-0.5">Ожидаемая дата — {formatShortDate(risk.shortageDate)}. Откройте прогноз.</p>
                </Link>
              ) : (
                <p className="text-sm text-muted p-2">Новых уведомлений нет.</p>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2.5 pl-2 sm:pl-3 sm:border-l border-line">
          <span className="grid place-items-center size-9 rounded-full bg-primary-soft text-primary text-sm font-semibold">ФС</span>
          <div className="hidden sm:block leading-tight">
            <p className="text-sm font-medium">Финансовый специалист</p>
            <p className="text-xs text-muted">Казначейство</p>
          </div>
          <ChevronDown size={16} className="text-muted hidden sm:block" />
        </div>
      </div>
    </header>
  )
}
