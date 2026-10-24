import type { Me, Role } from '../types'

const RANK: Record<Role, number> = { viewer: 1, editor: 2, admin: 3, director: 4 }

/** Роль пользователя; ответ старого бэкенда без поля трактуется как директор. */
export const roleOf = (me: Me): Role => me.role ?? 'director'

/** UI-only gating: the API enforces the same rules, so hiding is a courtesy, not security. */
export const atLeast = (me: Me, min: Role) => RANK[roleOf(me)] >= RANK[min]
/** Команда, настройки уведомлений, журнал: владелец и администратор. */
export const isAdmin = (me: Me) => atLeast(me, 'admin')
/** Менять данные компании: все, кроме наблюдателя. */
export const canEdit = (me: Me) => atLeast(me, 'editor')
