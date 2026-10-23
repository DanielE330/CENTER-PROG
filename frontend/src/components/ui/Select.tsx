import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'
import { cn } from '../../lib/utils'

export interface SelectOption {
  value: string | number
  label: string
  disabled?: boolean
}

interface Props {
  options: SelectOption[]
  value: string | number
  /** Receives the option value as a string. */
  onChange: (value: string) => void
  label?: string
  hint?: ReactNode
  disabled?: boolean
  placeholder?: string
  emptyText?: string
  className?: string
  'aria-label'?: string
}

interface Pos {
  top: number
  left: number
  width: number
  maxHeight: number
  up: boolean
}

const GAP = 6
const MARGIN = 8
const MAX_MENU = 280

/**
 * Accessible custom listbox. The menu is rendered in a portal with fixed positioning,
 * so it is never clipped by cards, tables or `overflow: hidden` containers, and it flips
 * upward / shrinks to stay inside the viewport.
 */
export function Select({ options, value, onChange, label, hint, disabled, placeholder = 'Выберите…', emptyText = 'Нет вариантов', className, ...rest }: Props) {
  const uid = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLUListElement>(null)
  const typed = useRef({ text: '', t: 0 })
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const [pos, setPos] = useState<Pos | null>(null)

  const selectedIdx = options.findIndex((o) => String(o.value) === String(value))
  const selected = options[selectedIdx]

  const place = useCallback(() => {
    const el = triggerRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const vh = window.innerHeight
    const below = vh - r.bottom - GAP - MARGIN
    const above = r.top - GAP - MARGIN
    const want = Math.min(MAX_MENU, options.length * 38 + 8)
    const up = below < want && above > below
    const maxHeight = Math.max(120, Math.min(MAX_MENU, up ? above : below))
    const width = Math.max(r.width, 160)
    const left = Math.min(Math.max(MARGIN, r.left), window.innerWidth - width - MARGIN)
    setPos({ left, width, maxHeight, up, top: up ? r.top - GAP : r.bottom + GAP })
  }, [options.length])

  const close = useCallback((refocus = true) => {
    setOpen(false)
    if (refocus) triggerRef.current?.focus()
  }, [])

  const openMenu = () => {
    if (disabled) return
    setActive(selectedIdx >= 0 ? selectedIdx : options.findIndex((o) => !o.disabled))
    setOpen(true)
  }

  const choose = (i: number) => {
    const o = options[i]
    if (!o || o.disabled) return
    onChange(String(o.value))
    close()
  }

  useLayoutEffect(() => {
    if (open) place()
  }, [open, place])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!menuRef.current?.contains(t) && !triggerRef.current?.contains(t)) close(false)
    }
    const onScroll = (e: Event) => {
      if (menuRef.current?.contains(e.target as Node)) return
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
  }, [open, place, close])

  // The menu mounts one render after `open` (once positioned); move focus into it then.
  const placed = pos !== null
  useEffect(() => {
    if (open && placed) menuRef.current?.focus({ preventScroll: true })
  }, [open, placed])

  // Keep the active option visible inside the scrolling menu.
  useEffect(() => {
    if (open && active >= 0) menuRef.current?.children[active]?.scrollIntoView({ block: 'nearest' })
  }, [open, active, pos])

  const move = (from: number, dir: 1 | -1) => {
    for (let i = from + dir; i >= 0 && i < options.length; i += dir) if (!options[i].disabled) return i
    return from
  }

  const onTriggerKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape' && open) {
      e.preventDefault()
      e.stopPropagation()
      close()
      return
    }
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
      e.preventDefault()
      openMenu()
    }
  }

  const onMenuKey = (e: KeyboardEvent) => {
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); setActive((a) => move(a, 1)); break
      case 'ArrowUp': e.preventDefault(); setActive((a) => move(a, -1)); break
      case 'Home': e.preventDefault(); setActive(move(-1, 1)); break
      case 'End': e.preventDefault(); setActive(move(options.length, -1)); break
      case 'Enter':
      case ' ': e.preventDefault(); choose(active); break
      case 'Escape': e.preventDefault(); e.stopPropagation(); close(); break
      case 'Tab': close(false); break
      default:
        if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
          const now = Date.now()
          typed.current.text = now - typed.current.t > 700 ? e.key : typed.current.text + e.key
          typed.current.t = now
          const q = typed.current.text.toLowerCase()
          const i = options.findIndex((o) => !o.disabled && o.label.toLowerCase().startsWith(q))
          if (i >= 0) setActive(i)
        }
    }
  }

  const triggerId = `${uid}-btn`
  const listId = `${uid}-list`

  return (
    <div className={cn('flex flex-col gap-1.5 min-w-0', className)}>
      {label && (
        <label htmlFor={triggerId} className="text-[13px] font-medium text-ink">
          {label}
        </label>
      )}
      <button
        ref={triggerRef}
        id={triggerId}
        type="button"
        disabled={disabled}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={rest['aria-label']}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={onTriggerKey}
        className="select-trigger"
      >
        <span className={cn('truncate', !selected && 'text-muted')}>{selected?.label ?? placeholder}</span>
        <ChevronDown size={16} className={cn('shrink-0 text-muted select-chevron', open && 'select-chevron-open')} />
      </button>
      {hint && <p className="text-xs text-muted">{hint}</p>}

      {open &&
        pos &&
        createPortal(
          <ul
            ref={menuRef}
            id={listId}
            role="listbox"
            tabIndex={-1}
            aria-label={label ?? rest['aria-label']}
            aria-activedescendant={active >= 0 ? `${uid}-opt-${active}` : undefined}
            onKeyDown={onMenuKey}
            className="select-menu"
            style={{
              left: pos.left,
              width: pos.width,
              maxHeight: pos.maxHeight,
              ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }),
            }}
          >
            {options.length === 0 && <li className="select-empty">{emptyText}</li>}
            {options.map((o, i) => {
              const isSel = i === selectedIdx
              return (
                <li
                  key={o.value}
                  id={`${uid}-opt-${i}`}
                  role="option"
                  aria-selected={isSel}
                  aria-disabled={o.disabled || undefined}
                  data-active={i === active || undefined}
                  data-selected={isSel || undefined}
                  className="select-option"
                  onMouseMove={() => !o.disabled && setActive(i)}
                  onClick={() => choose(i)}
                >
                  <span className="truncate">{o.label}</span>
                  {isSel && <Check size={15} className="shrink-0 text-primary" />}
                </li>
              )
            })}
          </ul>,
          document.body,
        )}
    </div>
  )
}
