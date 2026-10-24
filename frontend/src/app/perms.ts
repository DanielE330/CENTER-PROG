import { useTranslation } from 'react-i18next'
import { useMe } from './auth'
import type { Me, Permissions, Role } from '../types'

/** Права вычисляются на сервере; клиент только скрывает/блокирует кнопки (сервер всё равно ответит 403 forbidden). */
export function permsOf(me: Me): { role: Role; can: Permissions } {
  const role: Role = me.role ?? 'director'
  const can: Permissions = me.permissions ?? { read: true, edit: role !== 'viewer', manage: role === 'director' }
  return { role, can }
}

export function usePerms() {
  const { role, can } = permsOf(useMe())
  return { role, ...can }
}

/** For buttons that change company data: `{disabled, title}` spread onto a button. Viewer gets a hint instead of a silent no-op. */
export function useEditGuard() {
  const { edit } = usePerms()
  const { t } = useTranslation()
  return { edit, lock: { disabled: !edit, title: edit ? undefined : t('perms.denied') } as { disabled?: boolean; title?: string } }
}
