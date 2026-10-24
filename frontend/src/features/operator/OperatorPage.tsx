import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { LogOut, ShieldCheck } from 'lucide-react'
import { LoadingState, ErrorState } from '../../components/shared/States'
import { PageContainer, PageHeader } from '../../components/shared/Page'
import { Button } from '../../components/ui/Button'
import { ThemeToggle } from '../../components/layout/ThemeToggle'
import { LanguageSwitch } from '../../components/layout/LanguageSwitch'
import { errorText } from '../../i18n/helpers'
import { CompaniesList } from './CompaniesList'
import { CompanyDetail } from './CompanyDetail'
import { OperatorLogin } from './OperatorLogin'
import { OperatorStats } from './OperatorStats'
import { onOperatorExpired, opApi, opKeys, useOperatorMe } from './operatorApi'

function Panel({ email }: { email: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const open = params.get('company')

  async function logout() {
    try {
      await opApi.logout()
    } finally {
      qc.removeQueries({ queryKey: opKeys.all })
      qc.setQueryData(opKeys.me, null)
    }
  }

  return (
    <div className="min-h-full flex flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[80] focus:rounded-control focus:bg-primary focus:px-4 focus:py-2 focus:text-on-primary">
        {t('nav.skip')}
      </a>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3 sm:px-6">
        <div className="flex items-center gap-2.5">
          <span className="grid place-items-center size-9 rounded-[10px] bg-primary text-on-primary shrink-0" aria-hidden>
            <ShieldCheck size={18} />
          </span>
          <div className="leading-tight">
            <p className="font-semibold">{t('app.brand')}</p>
            <p className="text-xs text-muted">{t('operator.brand')}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] text-muted break-all">{t('operator.signedInAs', { email })}</span>
          <LanguageSwitch compact />
          <ThemeToggle />
          <Button size="sm" onClick={logout}>
            <LogOut size={15} aria-hidden />
            {t('auth.logout')}
          </Button>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="flex-1 outline-none">
        <PageContainer>
          {open ? (
            <CompanyDetail id={open} onBack={() => setParams({})} />
          ) : (
            <>
              <PageHeader title={t('operator.title')} description={t('operator.description')} />
              <OperatorStats />
              <CompaniesList onOpen={(id) => setParams({ company: id })} />
              <p className="text-[13px] text-muted">{t('operator.privacy')}</p>
            </>
          )}
        </PageContainer>
      </main>
    </div>
  )
}

/** /operator: platform operator panel. Own session, independent from the company sign-in. */
export default function OperatorPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const me = useOperatorMe()

  useEffect(() => {
    const prev = document.title
    document.title = t('operator.title')
    return () => {
      document.title = prev
    }
  }, [t])

  // the session expired while working: back to the sign-in form
  useEffect(
    () =>
      onOperatorExpired(() => {
        qc.removeQueries({ queryKey: opKeys.all, predicate: (q) => q.queryKey[1] !== 'me' })
        qc.setQueryData(opKeys.me, null)
      }),
    [qc],
  )

  if (me.isLoading) return <LoadingState height={400} />
  if (me.isError) return <ErrorState message={errorText(t, me.error)} onRetry={() => me.refetch()} />
  if (!me.data) return <OperatorLogin />
  return <Panel email={me.data.email} />
}
