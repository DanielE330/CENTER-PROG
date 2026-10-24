/**
 * Locale-aware formatting. All functions read the active i18n language at call time, so they also
 * work inside chart callbacks. Currency is independent of the language (RUB).
 */
import { parseISO } from 'date-fns'
import i18n from '../i18n'

const LOCALES = { ru: 'ru-RU', en: 'en-GB' } as const

export function getLocale(): string {
  return LOCALES[(i18n.language?.slice(0, 2) as keyof typeof LOCALES) ?? 'ru'] ?? LOCALES.ru
}

const CURRENCY = 'RUB'

export function formatCurrency(value: number | null | undefined): string {
  if (value == null) return '—'
  return new Intl.NumberFormat(getLocale(), {
    style: 'currency',
    currency: CURRENCY,
    currencyDisplay: 'narrowSymbol',
    maximumFractionDigits: 0,
  })
    .format(value)
    .replace(/ /g, ' ')
}

/** Compact money: 12,5 млн ₽ / ₽12.5M. */
export function formatCompact(value: number | null | undefined): string {
  if (value == null) return '—'
  return new Intl.NumberFormat(getLocale(), {
    style: 'currency',
    currency: CURRENCY,
    currencyDisplay: 'narrowSymbol',
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(value)
}

/** Compact number without a currency sign — for chart axes. */
export function formatAxisNumber(value: number): string {
  return new Intl.NumberFormat(getLocale(), { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat(getLocale(), { maximumFractionDigits: 1 }).format(value)
}

export function formatSignedCurrency(value: number): string {
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${formatCurrency(Math.abs(value))}`
}

export function formatPercent(value: number): string {
  return new Intl.NumberFormat(getLocale(), { style: 'percent', maximumFractionDigits: 0 }).format(value)
}

export type DateVariant = 'medium' | 'dayMonth' | 'weekdayDayMonth'

const dateOptions: Record<DateVariant, Intl.DateTimeFormatOptions> = {
  medium: { day: 'numeric', month: 'short', year: 'numeric' },
  dayMonth: { day: 'numeric', month: 'short' },
  weekdayDayMonth: { weekday: 'short', day: 'numeric', month: 'short' },
}

/** Russian appends " г." to numeric years; the UI never wants it. */
const stripYearSuffix = (s: string) => s.replace(/\s*г\.(?=,|$)/, '')

export function formatDate(iso: string | null | undefined, variant: DateVariant = 'medium'): string {
  if (!iso) return '—'
  return stripYearSuffix(new Intl.DateTimeFormat(getLocale(), dateOptions[variant]).format(parseISO(iso)))
}

export function formatShortDate(iso: string): string {
  return formatDate(iso, 'dayMonth')
}

export function formatDateTime(iso: string): string {
  return stripYearSuffix(
    new Intl.DateTimeFormat(getLocale(), {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    }).format(parseISO(iso)),
  )
}
