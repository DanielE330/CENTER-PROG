import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { Calendar, ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '../../lib/utils'

interface Props {
  /** ISO date (YYYY-MM-DD) or ''. */
  value: string
  onChange: (value: string) => void
  label?: string
  hint?: ReactNode
  /** ISO bounds, inclusive. */
  min?: string
  max?: string
  required?: boolean
  disabled?: boolean
  className?: string
  'aria-label'?: string
}

const POP_W = 288
const POP_H = 404
const GAP = 6
const MARGIN = 8
const CLOSE_MS = 120

const pad = (n: number) => String(n).padStart(2, '0')
const toIso = (y: number, m: number, d: number) => `${y}-${pad(m + 1)}-${pad(d)}`
const parseIso = (s: string): Date | null => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  if (!m) return null
  const d = new Date(+m[1], +m[2] - 1, +m[3])
  return d.getMonth() === +m[2] - 1 ? d : null
}
const dateIso = (d: Date) => toIso(d.getFullYear(), d.getMonth(), d.getDate())
const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)
const addMonths = (d: Date, n: number) => {
  const t = new Date(d.getFullYear(), d.getMonth() + n, 1)
  const last = new Date(t.getFullYear(), t.getMonth() + 1, 0).getDate()
  return new Date(t.getFullYear(), t.getMonth(), Math.min(d.getDate(), last))
}

const isRu = (lang: string) => lang.startsWith('ru')

function format(iso: string, lang: string): string {
  const d = parseIso(iso)
  if (!d) return ''
  const y = d.getFullYear()
  return isRu(lang) ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${y}` : `${pad(d.getMonth() + 1)}/${pad(d.getDate())}/${y}`
}

/** Accepts the localized format and plain ISO. Returns '' when the text is not a real date. */
function parseText(text: string, lang: string): string {
  const s = text.trim()
  if (parseIso(s)) return s
  const m = /^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$/.exec(s)
  if (!m) return ''
  const [a, b, y] = [+m[1], +m[2], +m[3]]
  const [day, mon] = isRu(lang) ? [a, b] : [b, a]
  const d = new Date(y, mon - 1, day)
  return d.getFullYear() === y && d.getMonth() === mon - 1 && d.getDate() === day ? toIso(y, mon - 1, day) : ''
}

/**
 * Custom calendar with manual entry. The value stays an ISO string, so forms and calculations are unaffected.
 * The popup is portalled with fixed positioning: never clipped by modals or scrollable cards.
 */
export function DatePicker({ value, onChange, label, hint, min, max, required, disabled, className, ...rest }: Props) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const uid = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<number | undefined>(undefined)

  const [text, setText] = useState(() => format(value, lang))
  const [open, setOpen] = useState(false)
  const [mounted, setMounted] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number; up: boolean } | null>(null)
  const [active, setActive] = useState<Date>(() => parseIso(value) ?? new Date())
  const focusGrid = useRef(false)

  // Follow external value / language changes.
  useEffect(() => setText(format(value, lang)), [value, lang])

  const inRange = useCallback((iso: string) => (!min || iso >= min) && (!max || iso <= max), [min, max])
  const todayIso = dateIso(new Date())

  const place = useCallback(() => {
    const el = inputRef.current?.parentElement
    if (!el) return
    const r = el.getBoundingClientRect()
    const below = window.innerHeight - r.bottom - GAP - MARGIN
    const h = Math.min(POP_H, window.innerHeight - 2 * MARGIN)
    const up = below < h && r.top - GAP - MARGIN > below
    const left = Math.min(Math.max(MARGIN, r.right - POP_W), window.innerWidth - POP_W - MARGIN)
    const top = up ? Math.max(MARGIN, r.top - GAP - h) : Math.min(r.bottom + GAP, Math.max(MARGIN, window.innerHeight - h - MARGIN))
    setPos({ top, left: Math.max(MARGIN, left), up })
  }, [])

  const openPop = (focus: boolean) => {
    if (disabled) return
    window.clearTimeout(closeTimer.current)
    const base = parseIso(value) ?? new Date()
    setActive(base)
    focusGrid.current = focus
    setMounted(true)
    setOpen(true)
  }

  const closePop = useCallback((refocus = true) => {
    setOpen(false)
    window.clearTimeout(closeTimer.current)
    closeTimer.current = window.setTimeout(() => setMounted(false), CLOSE_MS)
    if (refocus) inputRef.current?.focus({ preventScroll: true })
  }, [])

  useEffect(() => () => window.clearTimeout(closeTimer.current), [])

  useLayoutEffect(() => {
    if (mounted) place()
  }, [mounted, place])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const n = e.target as Node
      if (!popRef.current?.contains(n) && !inputRef.current?.parentElement?.contains(n)) closePop(false)
    }
    const onScroll = (e: Event) => {
      if (popRef.current?.contains(e.target as Node)) return
      place()
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open, place, closePop])

  const activeIso = dateIso(active)
  const placed = pos !== null
  useEffect(() => {
    if (open && placed && focusGrid.current) popRef.current?.querySelector<HTMLElement>('[data-day][tabindex="0"]')?.focus({ preventScroll: true })
  }, [open, placed, activeIso])

  const commitText = () => {
    if (!text.trim()) return onChange('')
    const iso = parseText(text, lang)
    if (iso && inRange(iso)) {
      if (iso !== value) onChange(iso)
      setText(format(iso, lang))
    } else setText(format(value, lang))
  }

  const pick = (iso: string) => {
    if (!inRange(iso)) return
    onChange(iso)
    setText(format(iso, lang))
    closePop()
  }

  const onInputKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || (e.altKey && e.key === 'ArrowDown')) {
      e.preventDefault()
      openPop(true)
    } else if (e.key === 'Escape' && open) {
      e.preventDefault()
      e.stopPropagation()
      closePop()
    } else if (e.key === 'Enter') commitText()
  }

  const onGridKey = (e: KeyboardEvent) => {
    const step: Record<string, (d: Date) => Date> = {
      ArrowLeft: (d) => addDays(d, -1),
      ArrowRight: (d) => addDays(d, 1),
      ArrowUp: (d) => addDays(d, -7),
      ArrowDown: (d) => addDays(d, 7),
      PageUp: (d) => addMonths(d, -1),
      PageDown: (d) => addMonths(d, 1),
      Home: (d) => addDays(d, -((d.getDay() + 6) % 7)),
      End: (d) => addDays(d, 6 - ((d.getDay() + 6) % 7)),
    }
    const onDay = (e.target as HTMLElement).hasAttribute('data-day')
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      closePop()
    } else if (!onDay) return
    else if (e.key === 'Tab') closePop(false)
    else if (step[e.key]) {
      e.preventDefault()
      focusGrid.current = true
      setActive(step[e.key](active))
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      pick(activeIso)
    }
  }

  // Month names / weekdays from Intl, so both languages are covered without extra strings.
  const monthTitle = useMemo(() => {
    const s = new Intl.DateTimeFormat(lang, { month: 'long', year: 'numeric' }).format(new Date(active.getFullYear(), active.getMonth(), 1))
    return s.charAt(0).toUpperCase() + s.slice(1)
  }, [active, lang])
  const weekdays = useMemo(() => {
    const f = new Intl.DateTimeFormat(lang, { weekday: 'short' })
    return Array.from({ length: 7 }, (_, i) => f.format(new Date(2024, 0, 1 + i)).replace('.', ''))
  }, [lang])
  const cells = useMemo(() => {
    const first = new Date(active.getFullYear(), active.getMonth(), 1)
    const start = addDays(first, -((first.getDay() + 6) % 7))
    return Array.from({ length: 42 }, (_, i) => addDays(start, i))
  }, [active])

  const shiftMonth = (n: number) => {
    focusGrid.current = false
    setActive((a) => addMonths(a, n))
  }

  const inputId = `${uid}-in`
  const popId = `${uid}-pop`

  return (
    <div className={cn('flex flex-col gap-1.5 min-w-0', className)}>
      {label && (
        <label htmlFor={inputId} className="text-[13px] font-medium text-ink">
          {label}
        </label>
      )}
      <div className="relative">
        <input
          ref={inputRef}
          id={inputId}
          type="text"
          inputMode="numeric"
          autoComplete="off"
          className="control pr-10"
          placeholder={t('common.datePicker.placeholder')}
          value={text}
          required={required}
          disabled={disabled}
          aria-label={rest['aria-label']}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? popId : undefined}
          maxLength={10}
          onChange={(e) => setText(e.target.value)}
          onBlur={commitText}
          onKeyDown={onInputKey}
        />
        <button
          ref={btnRef}
          type="button"
          disabled={disabled}
          aria-label={t('common.datePicker.open')}
          onClick={() => (open ? closePop() : openPop(true))}
          className="absolute right-1 top-1/2 -translate-y-1/2 grid place-items-center size-8 rounded-md text-muted hover:bg-hover cursor-pointer"
        >
          <Calendar size={16} aria-hidden />
        </button>
      </div>
      {hint && <p className="text-xs text-muted">{hint}</p>}

      {mounted &&
        pos &&
        createPortal(
          <div
            ref={popRef}
            id={popId}
            role="dialog"
            aria-label={label ?? t('common.datePicker.calendar')}
            className="dp-pop"
            data-state={open ? 'open' : 'closed'}
            data-up={pos.up || undefined}
            style={{ top: pos.top, left: pos.left, width: POP_W, height: Math.min(POP_H, window.innerHeight - 2 * MARGIN) }}
            onKeyDown={onGridKey}
          >
            <div className="dp-head">
              <button type="button" className="dp-nav" aria-label={t('common.datePicker.prevMonth')} onClick={() => shiftMonth(-1)}>
                <ChevronLeft size={16} aria-hidden />
              </button>
              <div className="dp-title" aria-live="polite">{monthTitle}</div>
              <button type="button" className="dp-nav" aria-label={t('common.datePicker.nextMonth')} onClick={() => shiftMonth(1)}>
                <ChevronRight size={16} aria-hidden />
              </button>
            </div>
            <div className="dp-grid" role="grid" aria-label={monthTitle}>
              {weekdays.map((w, i) => (
                <div key={i} className="dp-dow" role="columnheader">{w}</div>
              ))}
              {cells.map((d) => {
                const iso = dateIso(d)
                const outside = d.getMonth() !== active.getMonth()
                const off = !inRange(iso)
                return (
                  <button
                    key={iso}
                    type="button"
                    role="gridcell"
                    data-day
                    tabIndex={iso === activeIso ? 0 : -1}
                    disabled={off}
                    aria-selected={iso === value}
                    aria-current={iso === todayIso ? 'date' : undefined}
                    aria-label={new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'long', year: 'numeric' }).format(d)}
                    data-selected={iso === value || undefined}
                    data-today={iso === todayIso || undefined}
                    data-outside={outside || undefined}
                    className="dp-day"
                    onClick={() => pick(iso)}
                  >
                    {d.getDate()}
                  </button>
                )
              })}
            </div>
            <div className="dp-foot">
              <button type="button" className="dp-link" disabled={!inRange(todayIso)} onClick={() => pick(todayIso)}>
                {t('common.datePicker.today')}
              </button>
              <button
                type="button"
                className="dp-link"
                onClick={() => {
                  onChange('')
                  setText('')
                  closePop()
                }}
              >
                {t('common.datePicker.clear')}
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}
