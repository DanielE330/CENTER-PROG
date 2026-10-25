import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { CURRENCY_CODES, CURRENCY_SYMBOLS, rubPerUnit, setCurrency, useCurrency } from '../../lib/currency'
import type { CurrencyCode } from '../../lib/currency'
import { formatDate, getLocale } from '../../lib/formatters'
import { useCurrencyRates } from '../../hooks/queries'
import { cn } from '../../lib/utils'
import { Tooltip } from '../ui/Tooltip'

/** Localised currency name from Intl, first letter capitalised: «Доллар США» / «US Dollar». */
export function currencyName(code: CurrencyCode): string {
  try {
    const n = new Intl.DisplayNames(getLocale(), { type: 'currency' }).of(code) ?? code
    return n.charAt(0).toLocaleUpperCase(getLocale()) + n.slice(1)
  } catch {
    return code
  }
}

/** One line about the source of the rates: date, a stale/approximate warning, or why the rates are missing. */
export function CurrencyRatesNote({ className }: { className?: string }) {
  const { t } = useTranslation()
  const { selected, active, rates } = useCurrency()
  const q = useCurrencyRates()
  let text: string | null = null
  let warn = false
  if (!rates) {
    text = q.isError ? t('currency.unavailable') : t('currency.loading')
    warn = q.isError
  } else if (rates.approx) {
    text = t('currency.approx')
    warn = true
  } else {
    text = t('currency.rateDate', { date: rates.date ? formatDate(rates.date) : '—' })
    if (rates.stale) {
      text += ` · ${t('currency.stale')}`
      warn = true
    }
  }
  const fallback = selected !== active ? t('currency.shownInRub') : null
  return (
    <p className={cn('text-xs', warn ? 'text-warning' : 'text-muted', className)} role={warn ? 'status' : undefined}>
      {text}
      {fallback && ` ${fallback}`}
    </p>
  )
}

/** Short «1 USD = 80,54 ₽» for a currency row; null when there is no rate. */
function rateLine(code: CurrencyCode): string | null {
  const per = rubPerUnit(code)
  if (code === 'RUB' || per == null) return null
  return `1 ${code} = ${new Intl.NumberFormat(getLocale(), { maximumFractionDigits: 2 }).format(per)} ₽`
}

/** Compact currency picker for the app header: shows the code, opens a list of supported currencies. */
export function CurrencySwitch() {
  const { t } = useTranslation()
  const { selected, active } = useCurrency()
  useCurrencyRates()
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => !wrap.current?.contains(e.target as Node) && setOpen(false)
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        trigger.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    // Move focus into the list so it is usable from the keyboard.
    wrap.current?.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]')?.focus()
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const label = t('currency.switchLabel', { code: active })
  const onArrows = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    const items = [...(wrap.current?.querySelectorAll<HTMLElement>('[role="radio"]') ?? [])]
    const i = items.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
    items[next]?.focus()
  }

  return (
    <div className="relative" ref={wrap}>
      <Tooltip text={t('currency.title')} align="end">
        <button
          ref={trigger}
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-label={label}
          aria-expanded={open}
          aria-haspopup="true"
          className="flex items-center gap-1 h-10 px-2 rounded-control hover:bg-hover text-muted text-sm font-semibold cursor-pointer"
        >
          <span aria-hidden>{CURRENCY_SYMBOLS[active].length === 1 ? CURRENCY_SYMBOLS[active] : ''}</span>
          <span aria-hidden>{active}</span>
          <ChevronDown size={14} className="hidden sm:block" aria-hidden />
        </button>
      </Tooltip>
      {open && (
        <div className="card pop-in absolute right-0 top-12 w-72 max-w-[calc(100vw-2rem)] p-2 z-40" style={{ boxShadow: 'var(--shadow-pop)' }} onKeyDown={onArrows}>
          <p className="text-sm font-semibold px-2 pt-1">{t('currency.title')}</p>
          <CurrencyRatesNote className="px-2 pb-2 pt-0.5" />
          <div role="radiogroup" aria-label={t('currency.title')} className="flex flex-col max-h-80 overflow-y-auto">
            {CURRENCY_CODES.map((c) => {
              const on = c === selected
              const line = rateLine(c)
              return (
                <button
                  key={c}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  tabIndex={on ? 0 : -1}
                  onClick={() => { setCurrency(c); setOpen(false); trigger.current?.focus() }}
                  className={cn('flex items-center gap-2 min-h-10 px-2 rounded-control text-left text-sm cursor-pointer', on ? 'bg-primary-soft text-primary' : 'hover:bg-hover')}
                >
                  <span className="w-9 shrink-0 font-semibold">{c}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{currencyName(c)}</span>
                    {line && <span className="block text-xs text-muted tnum">{line}</span>}
                  </span>
                  {on && <Check size={16} aria-hidden />}
                </button>
              )
            })}
          </div>
          <p className="text-xs text-muted px-2 pt-2">{t('currency.inputRubHint')}</p>
        </div>
      )}
    </div>
  )
}
