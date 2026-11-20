import { useEffect, useState, type CSSProperties } from 'react'
import { Check } from 'lucide-react'
import { PLATFORM_META } from './platforms'

export { platformNames } from './platforms'

type Platform = {
  id: string
  browser_configured: boolean
  login_status: string
  last_status?: string
  last_run_at?: string
  needs_login?: boolean
  warning_count?: number
}

type Budget = { posts_used: number; posts_limit: number; api_calls_used: number; api_calls_limit: number }
type Catalog = { platforms: Platform[]; budget?: Budget; vendor_preflight?: { ok: boolean; mode: string; code?: string } }

const statusLabels: Record<string, string> = {
  needs_user_login: '需登录',
  running: '上次运行中',
  completed: '上次完成',
  completed_empty: '上次为空',
  partial: '上次部分完成',
  failed: '上次失败',
  interrupted: '上次中断'
}

export default function PlatformPicker({
  selected,
  onChange,
  request,
  loginNeeded = [],
  onNotice
}: {
  selected: string[]
  onChange: (ids: string[]) => void
  request: <T>(path: string, body?: unknown) => Promise<T>
  loginNeeded?: string[]
  onNotice?: (message: string, isError?: boolean) => void
}) {
  const [catalog, setCatalog] = useState<Platform[]>([])
  const [budget, setBudget] = useState<Budget | null>(null)
  const [vendor, setVendor] = useState<Catalog['vendor_preflight'] | null>(null)
  const [opening, setOpening] = useState<string | null>(null)

  const absorb = (payload: Catalog | Platform[]) => {
    if (Array.isArray(payload)) {
      setCatalog(payload)
      return
    }
    setCatalog(payload.platforms || [])
    setBudget(payload.budget || null)
    setVendor(payload.vendor_preflight || null)
  }

  useEffect(() => {
    let disposed = false
    request<Catalog | Platform[]>('/api/platforms')
      .then(value => { if (!disposed) absorb(value) })
      .catch(() => { if (!disposed) onNotice?.('平台目录暂未读取，请刷新后重试', true) })
    return () => { disposed = true }
  }, [])

  async function login(id: string) {
    setOpening(id)
    try {
      const reply = await request<{ message: string }>('/api/platforms/open-login', { platform: id })
      onNotice?.(reply.message)
      absorb(await request<Catalog | Platform[]>('/api/platforms'))
    } catch (e) {
      onNotice?.((e as Error).message, true)
    } finally {
      setOpening(null)
    }
  }

  const all = Object.keys(PLATFORM_META)
  return (
    <div>
      <div className="toolbar-row">
        <span>已选 {selected.length} / {all.length} · 共享同一份作品、请求与模型额度，按顺序后台采集</span>
        <button type="button" className="link-btn" onClick={() => onChange(all)}>全选</button>
      </div>
      {budget && (
        <div className="budget-bar">
          <span className="budget-label">今日额度</span>
          <div className="budget-track" role="progressbar" aria-label="今日额度余量">
            <i style={{ width: `${Math.min(100, Math.round(Math.max(
              budget.posts_limit ? budget.posts_used / budget.posts_limit : 0,
              budget.api_calls_limit ? budget.api_calls_used / budget.api_calls_limit : 0
            ) * 100))}%` }} />
          </div>
          <span className="budget-num">作品 {budget.posts_used}/{budget.posts_limit} · 模型 {budget.api_calls_used}/{budget.api_calls_limit}</span>
          {vendor && !vendor.ok && <span className="budget-warn">供应商预检未通过：{vendor.code || 'VENDOR_BLOCKED'}</span>}
        </div>
      )}
      <div className="platforms">
        {all.map(id => {
          const meta = PLATFORM_META[id]
          const on = selected.includes(id)
          const info = catalog.find(p => p.id === id)
          const needsLogin = loginNeeded.includes(id) || info?.needs_login === true
          const lastLabel = info?.last_status ? statusLabels[info.last_status] || info.last_status : ''
          const [stateText, stateColor] = needsLogin
            ? ['需要登录', 'var(--warn)']
            : info?.browser_configured
              ? [lastLabel || '后台就绪', lastLabel ? 'var(--text-2)' : 'var(--accent)']
              : ['未登录过', 'var(--text-3)']
          return (
            <div key={id} className="ptile" data-on={on} style={{ '--pc': meta.color } as CSSProperties}>
              <label>
                <span className="pglyph">{meta.glyph}</span>
                <span className="ptile-name">{meta.name}</span>
                <input
                  type="checkbox"
                  aria-label={`采集${meta.name}`}
                  checked={on}
                  disabled={selected.length === 1 && on}
                  onChange={e => onChange(e.target.checked ? [...selected, id] : selected.filter(p => p !== id))}
                />
                <span className="pcheck">{on && <Check size={12} strokeWidth={3} />}</span>
              </label>
              <div className="ptile-foot">
                <span className="state" style={{ '--sc': stateColor } as CSSProperties}><i />{stateText}</span>
                <button type="button" disabled={opening !== null} onClick={() => login(id)}>
                  {opening === id ? '打开中…' : '登录'}
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
