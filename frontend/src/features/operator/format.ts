import { formatDate, formatDateTime } from '../../lib/formatters'

/** Dates come from services as ISO strings; a malformed value is shown as is instead of breaking the page. */
export function safeDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return formatDate(iso)
  } catch {
    return iso
  }
}

export function safeDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return formatDateTime(iso)
  } catch {
    return iso
  }
}
