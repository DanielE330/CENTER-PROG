import type { TFunction } from 'i18next'

export const sourceLabel = (t: TFunction, source: string): string => t(`source.${source}`, { defaultValue: source })
