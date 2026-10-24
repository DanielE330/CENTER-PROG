/** Invite code from `?invite=<code>`: kept in sessionStorage so it survives sign-in / registration. */
const KEY = 'pending_invite'

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn()
  } catch {
    return fallback
  }
}

/** Call once at startup: moves the code from the URL into sessionStorage and removes it from the address bar. */
export function captureInvite(): void {
  safe(() => {
    const url = new URL(window.location.href)
    const code = url.searchParams.get('invite')
    if (!code) return
    sessionStorage.setItem(KEY, code)
    url.searchParams.delete('invite')
    window.history.replaceState(null, '', url.pathname + url.search + url.hash)
  }, undefined)
}

export const getInvite = (): string | null => safe(() => sessionStorage.getItem(KEY), null)
export const clearInvite = (): void => safe(() => sessionStorage.removeItem(KEY), undefined)
