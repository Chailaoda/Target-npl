import { useState } from 'react'
import { AlertTriangle, ArrowRight } from 'lucide-react'
import { platformNames } from '../platforms'

export interface TopStatusBarProps {
  runState: { run_id: string; status: string; phase?: string; message?: string } | null
  liveStats?: {
    captured_total?: number
    exact_count?: number
    potential_count?: number
    platform_statuses?: { platform: string; status: string }[]
  } | null
  onOpenResultsTab: () => void
  onRequestOpenLogin?: (platformId: string) => Promise<void> | void
}

export const phaseNames: Record<string, string> = {
  preparing: '准备环境',
  collecting: '多平台采集中',
  analyzing: '语义分析中',
  finished: '已结束',
  collection_stopped: '采集已停止',
  stopped: '已停止'
}

export default function TopStatusBar({ runState, liveStats, onOpenResultsTab, onRequestOpenLogin }: TopStatusBarProps) {
  const [opening, setOpening] = useState<string | null>(null)
  const loginNeeded = (liveStats?.platform_statuses || []).filter(p => p.status === 'needs_user_login')
  const running = !!runState && ['running', 'queued'].includes(runState.status)

  const openLogin = async (id: string) => {
    if (!onRequestOpenLogin) return
    setOpening(id)
    try { await onRequestOpenLogin(id) } finally { setOpening(null) }
  }

  if (loginNeeded.length > 0) {
    const names = loginNeeded.map(p => platformNames[p.platform] || p.platform).join('、')
    return (
      <div className="strip strip-warn" role="status">
        <AlertTriangle size={15} />
        <span className="grow">
          <b>{names}</b>{' '}
          {running
            ? '需要本人登录或验证，对应浏览器窗口已弹到桌面；其余平台继续在后台采集。'
            : '在上次运行中需要本人登录或验证；登录完成后再次开始抓取即可。'}
        </span>
        {loginNeeded.map(p => (
          <button key={p.platform} type="button" className="btn btn-sm" disabled={opening === p.platform} onClick={() => openLogin(p.platform)}>
            {opening === p.platform ? '唤起中…' : `${running ? '重新显示' : '登录'}${platformNames[p.platform] || p.platform}`}
          </button>
        ))}
      </div>
    )
  }

  if (!running) return null
  return (
    <div className="strip strip-live" role="status">
      <span className="live-dot on" />
      <b>正在运行 · {phaseNames[runState?.phase || ''] || '启动中'}</b>
      <span className="stats">
        <span>已采集<em>{liveStats?.captured_total ?? 0}</em>条</span>
        <span>精准<em style={{ color: 'var(--exact)' }}>{liveStats?.exact_count ?? 0}</em>人</span>
        <span>潜在<em style={{ color: 'var(--potential)' }}>{liveStats?.potential_count ?? 0}</em>人</span>
      </span>
      <span className="grow" />
      <button type="button" className="btn btn-sm btn-ghost" onClick={onOpenResultsTab}>
        查看实时情报流 <ArrowRight size={13} />
      </button>
      <span className="strip-bar" />
    </div>
  )
}
