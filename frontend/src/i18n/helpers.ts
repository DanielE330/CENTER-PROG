import type { TFunction } from 'i18next'
import i18n from './index'
import { formatCompact, formatDate, formatKop, formatPercent, formatShortDate } from '../lib/formatters'
import { ApiError } from '../lib/errors'

/** A dated line of a list inside a message: «• 12 Oct — Title: 1 000 ₽». `amount` is kopecks. */
export interface LineRef {
  date: string
  title: string
  amount: string
  overdue?: boolean
}

/** Raw, language-neutral values attached to a translatable message. */
export type MsgParams = Record<string, string | number | string[] | LineRef[] | undefined>

/**
 * Resolves params for interpolation:
 *  - `xKey` (string)   -> translated text
 *  - `xKeys` (string[]) -> translated, comma separated
 *  - `xLines` (LineRef[]) -> bullet list, one per line
 *  - `xDate` / `xShortDate` -> formatted date
 *  - `xKop` (kopecks string) -> money; `xAmount` (rubles number) -> compact money
 *  - `xPercent` (0..1) -> percent
 * Other values are passed through.
 */
export function resolveParams(t: TFunction, params: MsgParams = {}): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [k, v] of Object.entries(params)) {
    if (v == null) continue
    if (k.endsWith('Lines') && Array.isArray(v)) {
      out[k] = (v as LineRef[])
        .map((i) => `• ${formatShortDate(i.date)} — ${i.title}: ${formatKop(i.amount)}${i.overdue ? ` (${t('status.overdueLower')})` : ''}`)
        .join('\n')
    } else if (k.endsWith('Keys') && Array.isArray(v)) out[k] = (v as string[]).map((x) => t(x)).join(', ')
    else if (k.endsWith('Key') && typeof v === 'string') out[k] = t(v)
    else if (k.endsWith('ShortDate') && typeof v === 'string') out[k] = formatShortDate(v)
    else if (k.endsWith('Date') && typeof v === 'string') out[k] = formatDate(v)
    else if (k.endsWith('Kop') && typeof v === 'string') out[k] = formatKop(v)
    else if (k.endsWith('Amount') && typeof v === 'number') out[k] = formatCompact(v)
    else if (k.endsWith('Percent') && typeof v === 'number') out[k] = formatPercent(v)
    else out[k] = v as string | number
  }
  return out
}

/** Translates a key with language-neutral params. */
export function tm(t: TFunction, key: string, params?: MsgParams): string {
  return t(key, resolveParams(t, params))
}

/** Error carrying a translation key, so API code stays language-neutral. */
export class AppError extends Error {
  constructor(
    public key: string,
    public params?: MsgParams,
  ) {
    super(key)
  }
}

/** Translates AppErrors and known API error codes; unknown server messages are shown as they are. */
export function errorText(t: TFunction, e: unknown): string {
  if (e instanceof AppError) return tm(t, e.key, e.params)
  if (e instanceof ApiError) {
    const key = `apiErrors.${e.code}`
    return i18n.exists(key) ? t(key) : e.message
  }
  if (e instanceof Error) return e.message
  return t('errors.unknown')
}
