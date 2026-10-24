import type { Me, WebRole } from '../types'

const RANK: Record<WebRole, number> = { observer: 1, analyst: 2, admin: 3, owner: 4 }

/** UI-only gating: the API enforces the same rules, so hiding is a courtesy, not security. */
export const atLeast = (me: Me, min: WebRole) => RANK[me.role] >= RANK[min]
export const isAdmin = (me: Me) => atLeast(me, 'admin')
export const canEdit = (me: Me) => atLeast(me, 'analyst')
