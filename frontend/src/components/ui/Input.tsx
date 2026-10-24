import { useId } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'
import { cn } from '../../lib/utils'

function Field({ id, label, hint, children }: { id: string; label?: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      {label && (
        <label htmlFor={id} className="text-[13px] font-medium text-ink">
          {label}
        </label>
      )}
      {children}
      {hint && <p className="text-xs text-muted">{hint}</p>}
    </div>
  )
}

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string
  hint?: ReactNode
}

export function Input({ label, hint, className, ...rest }: InputProps) {
  const id = useId()
  return (
    <Field id={id} label={label} hint={hint}>
      <input id={id} className={cn('control', className)} {...rest} />
    </Field>
  )
}

