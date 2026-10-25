import type { TFunction } from 'i18next'
import type { Tone } from '../../components/shared/StatusBadge'
import type { FnsRisk } from '../../types'

export const riskTone: Record<FnsRisk, Tone> = { low: 'success', medium: 'warning', high: 'danger', unknown: 'neutral' }

export const statusLabel = (t: TFunction, status: string): string => t(`fns.status.${status}`, { defaultValue: t('fns.status.unknown') })

/** Подпись источника: загруженная справка или открытый реестр ФНС. */
export function sourceText(t: TFunction, source: string | null): string {
  if (!source) return '—'
  return t(`fns.sourceName.${source}`, { defaultValue: source })
}

/** Коды причин риска из fns (учтены настройки компании) -> ключи перевода. */
const REASON_KEY: Record<string, string> = {
  bankruptcy: 'bankruptcy', liquidating: 'liquidating', liquidated: 'liquidated', not_in_registry: 'notInRegistry',
  unreliable_data: 'unreliable', disqualified_head: 'disqualified', tax_debt: 'taxDebt', tax_debt_large: 'taxDebtLarge', mass_address: 'massAddress',
}

/** Причины риска контрагента простыми словами. */
export function riskReasons(t: TFunction, c: { risk_reasons?: string[]; has_tax_debt?: boolean; bankruptcy_flag?: boolean; unreliable?: boolean; mass_address?: boolean; disqualified?: boolean; status: string }): string[] {
  // причины от fns учитывают настройки (выключенный признак риском не считается)
  if (c.risk_reasons) return c.risk_reasons.map((r) => t(`fns.reason.${REASON_KEY[r] ?? r}`, { defaultValue: r }))
  const out: string[] = []
  if (c.bankruptcy_flag) out.push(t('fns.reason.bankruptcy'))
  if (c.status === 'liquidating' || c.status === 'liquidated') out.push(t(`fns.reason.${c.status}`))
  if (c.has_tax_debt) out.push(t('fns.reason.taxDebt'))
  if (c.unreliable) out.push(t('fns.reason.unreliable'))
  if (c.disqualified) out.push(t('fns.reason.disqualified'))
  if (c.mass_address) out.push(t('fns.reason.massAddress'))
  return out
}
