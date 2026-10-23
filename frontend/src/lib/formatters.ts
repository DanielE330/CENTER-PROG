import { format, parseISO, formatDistanceToNow } from 'date-fns'
import { ru } from 'date-fns/locale'

const rub = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  maximumFractionDigits: 0,
})

const num = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })

export function formatCurrency(value: number | null | undefined): string {
  if (value == null) return '—'
  return rub.format(value).replace(/\u00a0/g, '\u202f')
}

/** Compact: 12,5 млн ₽. Used on chart axes and tooltips. */
export function formatCompact(value: number | null | undefined): string {
  if (value == null) return '—'
  const abs = Math.abs(value)
  if (abs >= 1_000_000) return `${num.format(value / 1_000_000)} млн ₽`
  if (abs >= 1_000) return `${num.format(value / 1_000)} тыс ₽`
  return `${num.format(value)} ₽`
}

export function formatSignedCurrency(value: number): string {
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${formatCurrency(Math.abs(value))}`
}

export function formatDate(iso: string | null | undefined, pattern = 'd MMM yyyy'): string {
  if (!iso) return '—'
  return format(parseISO(iso), pattern, { locale: ru })
}

export function formatShortDate(iso: string): string {
  return format(parseISO(iso), 'd MMM', { locale: ru })
}

export function formatDateTime(iso: string): string {
  return format(parseISO(iso), 'd MMM yyyy, HH:mm', { locale: ru })
}

export function formatRelative(iso: string): string {
  return formatDistanceToNow(parseISO(iso), { addSuffix: true, locale: ru })
}

export function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`
}
