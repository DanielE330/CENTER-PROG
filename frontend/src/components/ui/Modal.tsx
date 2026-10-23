import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'

interface Props {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
}

export function Modal({ open, title, onClose, children }: Props) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="absolute inset-0 bg-overlay" onClick={onClose} />
      <div className="card relative w-full max-w-lg max-h-[90vh] overflow-auto p-6" style={{ boxShadow: 'var(--shadow-pop)' }}>
        <div className="flex items-start justify-between gap-4 mb-4">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Закрыть" className="p-1 rounded-md text-muted hover:bg-hover cursor-pointer">
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
