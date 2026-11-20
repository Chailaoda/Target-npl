import { useEffect, useMemo, useRef, useState } from 'react'
import { Crosshair } from 'lucide-react'
import { platformNames } from './platforms'
import TopStatusBar from './components/TopStatusBar'
import SideNav from './components/SideNav'
import Topbar from './components/Topbar'
import Toasts from './components/Toasts'
import InterestDiscovery from './InterestDiscovery'
import WorkbenchPage from './pages/WorkbenchPage'
import MonitorPage from './pages/MonitorPage'
import HistoryPage from './pages/HistoryPage'
import ReviewWorkbenchPage from './pages/ReviewWorkbenchPage'
import { createApiClient } from './lib/apiClient'
import { formatErrorGuidance } from './lib/errorGuide'
import { useConfigDraft, type Notices } from './hooks/useConfigDraft'
import { useRunStream } from './hooks/useRunStream'
import { useSandbox } from './hooks/useSandbox'
import type { RunState, View } from './types'

export { makeTask } from './types'
export type { Policy, Collection, Task, Bundle } from './types'

const THEME_KEY = 'npl_theme'
const readTheme = (): 'dark' | 'light' => {
  try { return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark' } catch { return 'dark' }
}

const pages: Record<View, [string, string]> = {
  workbench: ['任务配置', '描述目标、维护词库、选择平台，然后开始抓取'],
  monitor: ['实时情报流', '后台采集与模型筛选的结果在这里实时滚动'],
  discover: ['人物发现', '输入任意兴趣，从已有公开行为证据中发现账号'],
  review: ['复核与冲突', '人工确认归属，逐条复核模型判不准的内容'],
  history: ['版本与备份', '每次保存都会生成快照，可随时恢复或导入导出']
}

export default function App() {
  const tokenRef = useRef('')
  const api = useMemo(() => createApiClient(() => tokenRef.current), [])
  const [tab, setTab] = useState<View>('workbench')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [conflictCount, setConflictCount] = useState(0)
  const [theme, setTheme] = useState<'dark' | 'light'>(readTheme)

  const notices = useMemo<Notices>(() => ({
    notify: (text: string) => { setMessage(text); setError('') },
    fail: (text: string) => setError(formatErrorGuidance(text)),
    clear: () => { setMessage(''); setError('') }
  }), [])

  const started = useRef<(run: RunState) => void>(() => {})
  const config = useConfigDraft(api, notices, token => { tokenRef.current = token }, run => started.current(run))
  const stream = useRunStream(api, !!config.active)
  const sandbox = useSandbox(api, notices.fail)

  useEffect(() => {
    started.current = run => { stream.setRunState(run); setTab('monitor') }
  }, [stream.setRunState])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem(THEME_KEY, theme) } catch {}
  }, [theme])

  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => setMessage(''), 4200)
    return () => clearTimeout(timer)
  }, [message])

  // Badge only: the review page fetches the real lists itself.
  useEffect(() => {
    if (!config.active) return
    let cancelled = false
    api<{ total_pending: number }>('/api/accounts/conflicts?status=pending&limit=1')
      .then(data => { if (!cancelled) setConflictCount(data.total_pending || 0) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [api, config.active, tab])

  const openLogin = async (platformId: string) => {
    try {
      const reply = await api<{ message: string }>('/api/platforms/open-login', { platform: platformId })
      notices.notify(reply.message || `已弹出${platformNames[platformId] || platformId}登录窗口`)
    } catch (err) {
      notices.fail((err as Error).message)
    }
  }

  const sendToTest = (comment: string, context?: string) => {
    if (!config.draft) return
    sandbox.setCommentText(comment)
    sandbox.setContextText(context || '')
    setTab('workbench')
    notices.notify('已送入试金石并自动判定')
    void sandbox.run(config.draft, comment, context)
  }

  if (!config.draft || !config.active) {
    return (
      <main className="boot">
        <span className="brand-mark"><Crosshair size={22} /></span>
        <b style={{ color: 'var(--text)' }}>Target-NPL</b>
        <span>{error || '正在连接本地引擎…'}</span>
      </main>
    )
  }

  const running = stream.running
  const loginNeeded = (stream.liveData?.collection?.platform_statuses || [])
    .filter(item => item.status === 'needs_user_login').map(item => item.platform)

  return (
    <div className="shell">
      <SideNav
        tab={tab}
        onSelect={setTab}
        running={running}
        runState={stream.runState}
        liveData={stream.liveData}
        conflictCount={conflictCount}
        theme={theme}
        onToggleTheme={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
      />

      <div className="main">
        <Topbar
          title={pages[tab]}
          dirty={config.dirty}
          busy={config.busy}
          running={running}
          onSave={config.saveDraft}
          onStart={config.saveAndStart}
        />

        <TopStatusBar
          runState={stream.runState}
          liveStats={{
            captured_total: stream.liveData?.captured_total,
            exact_count: stream.liveData?.exact_count,
            potential_count: stream.liveData?.potential_count,
            platform_statuses: stream.liveData?.collection?.platform_statuses
          }}
          onOpenResultsTab={() => setTab('monitor')}
          onRequestOpenLogin={openLogin}
        />

        <main className="content">
          {tab === 'discover' && <InterestDiscovery api={api} />}
          {tab === 'workbench' && (
            <WorkbenchPage api={api} config={config} sandbox={sandbox} busy={config.busy} notices={notices} loginNeeded={loginNeeded} />
          )}
          {tab === 'monitor' && (
            <MonitorPage
              run={stream.runState}
              live={stream.liveData}
              error={stream.liveError}
              onSendToTest={sendToTest}
              onAddBlacklistWord={config.addBlacklistWords}
              onRequestOpenLogin={openLogin}
            />
          )}
          {tab === 'review' && <ReviewWorkbenchPage api={api} notices={notices} />}
          {tab === 'history' && <HistoryPage api={api} config={config} notices={notices} onRestored={() => setTab('workbench')} />}
        </main>
      </div>

      <Toasts message={message} error={error} onCloseMessage={() => setMessage('')} onCloseError={() => setError('')} />
    </div>
  )
}
