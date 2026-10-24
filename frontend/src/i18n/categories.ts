import type { TFunction } from 'i18next'

/** Transaction categories known to the backend (parser). */
export const CATEGORIES = [
  'revenue', 'other_income', 'suppliers', 'payroll', 'rent', 'taxes', 'loans', 'utilities',
  'bank_fees', 'marketing', 'transfers', 'refunds', 'other_expense',
] as const

export const categoryLabel = (t: TFunction, key: string): string => t(`categories.${key}`, { defaultValue: key })
