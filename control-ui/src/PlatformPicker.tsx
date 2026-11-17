import { useEffect, useState, type CSSProperties } from 'react'
import { Check } from 'lucide-react'
import { PLATFORM_META } from './platforms'

export { platformNames } from './platforms'

type Platform = { id: string; browser_configured: boolean; login_status: string }

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
  const [opening, setOpening] = useState<string | null>(null)

  useEffect(() => {
    let disposed = false
    request<Platform[]>('/api/platforms')
      .then(v => { if (!disposed && Array.isArray(v)) setCatalog(v) })
      .catch(() => { if (!disposed) onNotice?.('平台目录暂未读取，请刷新后重试', true) })
    return () => { disposed = true }
  }, [])

  async function login(id: string) {
    setOpening(id)
    try {
      const reply = await request<{ message: string }>('/api/platforms/open-login', { platform: id })
      onNotice?.(reply.message)
      setCatalog(await request<Platform[]>('/api/platforms'))
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
      <div className="platforms">
        {all.map(id => {
          const meta = PLATFORM_META[id]
          const on = selected.includes(id)
          const info = catalog.find(p => p.id === id)
          const needsLogin = loginNeeded.includes(id)
          const [stateText, stateColor] = needsLogin
            ? ['需要登录', 'var(--warn)']
            : info?.browser_configured
              ? ['后台就绪', 'var(--accent)']
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
