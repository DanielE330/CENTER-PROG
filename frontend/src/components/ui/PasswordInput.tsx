import { useId, useState } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '../../lib/utils'

interface Props extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'onChange' | 'value'> {
  label: string
  value: string
  onChange: (value: string) => void
  hint?: ReactNode
  error?: string | null
}

/** Password field with a show/hide toggle (aria-pressed, labelled for screen readers). */
export function PasswordInput({ label, value, onChange, hint, error, className, ...rest }: Props) {
  const { t } = useTranslation()
  const id = useId()
  const [show, setShow] = useState(false)
  const describedBy = error ? `${id}-err` : hint ? `${id}-hint` : undefined
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      <label htmlFor={id} className="text-[13px] font-medium text-ink">{label}</label>
      <div className="relative">
        <input
          id={id}
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          className={cn('control pr-11', error && 'border-danger', className)}
          {...rest}
        />
        <button
          type="button"
          onClick={() => setShow((v) => !v)}
          aria-label={show ? t('auth.hidePassword') : t('auth.showPassword')}
          aria-pressed={show}
          title={show ? t('auth.hidePassword') : t('auth.showPassword')}
          className="absolute right-1 top-1/2 -translate-y-1/2 grid place-items-center size-8 rounded-md text-muted hover:bg-hover cursor-pointer"
        >
          {show ? <EyeOff size={18} aria-hidden /> : <Eye size={18} aria-hidden />}
        </button>
      </div>
      {error ? (
        <p id={`${id}-err`} className="text-xs text-danger" role="alert">{error}</p>
      ) : (
        hint && <p id={`${id}-hint`} className="text-xs text-muted">{hint}</p>
      )}
    </div>
  )
}
