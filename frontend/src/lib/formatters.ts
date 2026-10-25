/**
 * Locale-aware formatting. All functions read the active i18n language at call time, so they also
 * work inside chart callbacks. Currency is independent of the language (display currency, default RUB).
 */
import { parseISO } from 'date-fns'
import i18n from '../i18n'
import { activeCurrency, CURRENCY_SYMBOLS, kopToDisplay, rubToDisplay } from './currency'
import type { CurrencyCode } from './currency'

const LOCALES = { ru: 'ru-RU', en: 'en-GB' } as const

export function getLocale(): string {
  return LOCALES[(i18n.language?.slice(0, 2) as keyof typeof LOCALES) ?? 'ru'] ?? LOCALES.ru
}

/**
 * Money is stored and computed in rubles; the user may pick another DISPLAY currency (lib/currency.ts).
 * Everything below converts at call time, so chart callbacks and i18n helpers follow the choice too.
 */

/** Whole units are enough for large sums; small foreign amounts keep cents (1 200 ₽ is only ≈ $15). */
function fractionDigits(code: CurrencyCode, value: number): number {
  return code === 'RUB' || Math.abs(value) >= 100 ? 0 : 2
}

/** Formats an amount that is ALREADY in the active currency, swapping Intl's sign for ours (₽ $ € ¥ £ ₸ Br ₺ AED). */
function money(value: number, opts: { compact?: boolean } = {}): string {
  const code = activeCurrency()
  const fmt = new Intl.NumberFormat(getLocale(), {
    style: 'currency',
    currency: code,
    currencyDisplay: 'narrowSymbol',
    ...(opts.compact ? { notation: 'compact' as const, maximumFractionDigits: 1 } : { maximumFractionDigits: fractionDigits(code, value) }),
  })
  return fmt
    .formatToParts(value)
    .map((p) => (p.type === 'currency' ? CURRENCY_SYMBOLS[code] : p.value))
    .join('')
}

/** Rubles (a number) -> the display currency. */
export function formatCurrency(value: number | null | undefined): string {
  if (value == null) return '—'
  return money(rubToDisplay(value)).replace(/\u00A0/g, '\u202F')
}

/** Money from the API: a decimal string of kopecks. In rubles it is rounded to whole rubles without float arithmetic. */
export function formatKop(kop: string | null | undefined, opts: { sign?: boolean } = {}): string {
  if (kop == null || kop === '') return '—'
  const v = BigInt(kop)
  const abs = v < 0n ? -v : v
  let text: string
  let nonZero: boolean
  if (activeCurrency() === 'RUB') {
    const rub = (abs + 50n) / 100n
    text = money(Number(rub))
    nonZero = rub > 0n
  } else {
    const conv = kopToDisplay(abs.toString())
    text = money(conv)
    nonZero = Number(conv.toFixed(fractionDigits(activeCurrency(), conv))) > 0
  }
  if (v < 0n && nonZero) return `−${text}`
  return opts.sign && nonZero ? `+${text}` : text
}

/** Compact money from rubles: 12,5 млн ₽ / $12.5M. */
export function formatCompact(value: number | null | undefined): string {
  if (value == null) return '—'
  return money(rubToDisplay(value), { compact: true })
}

/** Chart tooltips: an amount already converted with `chartRub()`. */
export function formatChartMoney(value: number | null | undefined): string {
  return value == null ? '—' : money(value).replace(/\u00A0/g, '\u202F')
}

/** Chart tooltips: compact variant of an amount already converted with `chartRub()`. */
export function formatChartCompact(value: number | null | undefined): string {
  return value == null ? '—' : money(value, { compact: true })
}

/** Compact number without a currency sign — for money chart axes. Takes a value already converted with `chartRub()`. */
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
  // A tiny but non-zero probability must not read as "0 %".
  if (value > 0 && value < 0.005) return `<${formatPercent(0.01)}`
  return new Intl.NumberFormat(getLocale(), { style: 'percent', maximumFractionDigits: 0 }).format(value)
}

export type DateVariant = 'medium' | 'dayMonth' | 'weekdayDayMonth'

const dateOptions: Record<DateVariant, Intl.DateTimeFormatOptions> = {
  medium: { day: 'numeric', month: 'short', year: 'numeric' },
  dayMonth: { day: 'numeric', month: 'short' },
  weekdayDayMonth: { weekday: 'short', day: 'numeric', month: 'short' },
}

/** Russian appends " г." to numeric years; the UI never wants it. */
const stripYearSuffix = (s: string) => s.replace(/\s+г\.(?=,|$)/, '')

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

/** Percentage points such as the key rate (14 -> «14%», 14.25 -> «14,25%»). Not a 0..1 ratio. */
export function formatRate(value: number): string {
  return `${new Intl.NumberFormat(getLocale(), { minimumFractionDigits: 0, maximumFractionDigits: 2 }).format(value)}%`
}
