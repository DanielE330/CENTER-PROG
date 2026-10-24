import type { TFunction } from 'i18next'
import type { Tone } from '../../components/shared/StatusBadge'
import type { FnsMode, FnsRisk } from '../../types'

export const riskTone: Record<FnsRisk, Tone> = { low: 'success', medium: 'warning', high: 'danger', unknown: 'neutral' }

export const statusLabel = (t: TFunction, status: string): string => t(`fns.status.${status}`, { defaultValue: t('fns.status.unknown') })

/** Подпись источника: имитатор, загруженная справка или реестры; режим показываем рядом, чтобы имитацию не принять за боевые данные. */
export function sourceText(t: TFunction, source: string | null, mode: FnsMode | null | undefined): string {
  if (!source) return '—'
  const name = t(`fns.sourceName.${source}`, { defaultValue: source })
  return mode === 'sim' ? `${name} · ${t('fns.modeSim')}` : name
}

/** Причины риска контрагента простыми словами. */
export function riskReasons(t: TFunction, c: { has_tax_debt?: boolean; bankruptcy_flag?: boolean; unreliable?: boolean; mass_address?: boolean; disqualified?: boolean; status: string }): string[] {
  const out: string[] = []
  if (c.bankruptcy_flag) out.push(t('fns.reason.bankruptcy'))
  if (c.status === 'liquidating' || c.status === 'liquidated') out.push(t(`fns.reason.${c.status}`))
  if (c.has_tax_debt) out.push(t('fns.reason.taxDebt'))
  if (c.unreliable) out.push(t('fns.reason.unreliable'))
  if (c.disqualified) out.push(t('fns.reason.disqualified'))
  if (c.mass_address) out.push(t('fns.reason.massAddress'))
  return out
}
