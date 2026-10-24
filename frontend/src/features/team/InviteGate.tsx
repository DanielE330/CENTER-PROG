import { useState } from 'react'
import type { ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../../app/auth'
import { Card } from '../../components/shared/Page'
import { Notice } from '../../components/shared/Notice'
import { Button } from '../../components/ui/Button'
import { api } from '../../lib/api'
import { clearInvite, getInvite } from '../../lib/invite'
import { errorText } from '../../i18n/helpers'

/**
 * A signed-in user who opened an invite link is asked to confirm before joining: accepting switches the whole
 * workspace to the inviter's company (the user's own company is kept and returns after leaving the team).
 */
export function InviteGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const { setMe } = useAuth()
  const [code, setCode] = useState(getInvite())
  const accept = useMutation({
    mutationFn: (c: string) => api.acceptInvite(c),
    onSuccess: async () => {
      clearInvite()
      setCode(null)
      setMe(await api.me())
    },
  })
  const decline = () => {
    clearInvite()
    setCode(null)
  }
  if (!code) return <>{children}</>
  return (
    <div className="mx-auto max-w-md px-4 py-10">
      <Card title={t('team.inviteTitle')} subtitle={t('team.inviteText')}>
        <div className="px-5 pb-5 flex flex-col gap-4">
          {accept.isError && <Notice tone="danger">{errorText(t, accept.error)}</Notice>}
          <div className="flex gap-3">
            <Button variant="primary" disabled={accept.isPending} onClick={() => accept.mutate(code)}>{t('team.inviteAccept')}</Button>
            <Button disabled={accept.isPending} onClick={decline}>{t('team.inviteDecline')}</Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
