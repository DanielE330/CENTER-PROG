import type { TFunction } from 'i18next'
import { formatCompact, formatDate, formatPercent, formatShortDate } from '../lib/formatters'

/** Raw, language-neutral values the API / mocks may attach to a translatable message. */
export type MsgParams = Record<string, string | number | string[] | ItemRef[] | undefined>

/** A reference to an obligation, rendered as «name» (amount, date, status). */
export interface ItemRef {
  nameKey: string
  amount?: number
  date?: string
  statusKey?: string
}

/**
 * Resolves params for interpolation:
 *  - `xKey` (string)   -> translated text
 *  - `xKeys` (string[]) -> translated, comma separated
 *  - `xItems` (ItemRef[]) -> «name» (amount, date, status), comma separated
 *  - `xDate` / `xAmount` / `xPercent` -> formatted for the active locale
 * Other values are passed through.
 */
export function resolveParams(t: TFunction, params: MsgParams = {}): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [k, v] of Object.entries(params)) {
    if (v == null) continue
    if (k.endsWith('Items') && Array.isArray(v)) {
      out[k] = (v as ItemRef[])
        .map((i) => {
          const bits = [
            i.amount != null ? formatCompact(i.amount) : '',
            i.date ? formatShortDate(i.date) : '',
            i.statusKey ? t(i.statusKey) : '',
          ].filter(Boolean)
          return `«${t(i.nameKey)}»${bits.length ? ` (${bits.join(', ')})` : ''}`
        })
        .join(', ')
    } else if (k.endsWith('Keys') && Array.isArray(v)) out[k] = (v as string[]).map((x) => t(x)).join(', ')
    else if (k.endsWith('Key') && typeof v === 'string') out[k] = t(v)
    else if (k.endsWith('ShortDate') && typeof v === 'string') out[k] = formatShortDate(v)
    else if (k.endsWith('Date') && typeof v === 'string') out[k] = formatDate(v)
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

/** Error carrying a translation key, so mocks and API code stay language-neutral. */
export class AppError extends Error {
  constructor(
    public key: string,
    public params?: MsgParams,
  ) {
    super(key)
  }
}

/** Translates AppErrors; real server messages are shown as they are. */
export function errorText(t: TFunction, e: unknown): string {
  if (e instanceof AppError) return tm(t, e.key, e.params)
  if (e instanceof Error) return e.message
  return t('errors.unknown')
}
