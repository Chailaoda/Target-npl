import type { CSSProperties, ReactNode } from 'react'
import { Activity, Crosshair, History, Moon, ShieldQuestion, SlidersHorizontal, Sun, Users } from 'lucide-react'
import { phaseNames } from './TopStatusBar'
import type { LiveData } from '../RunMonitor'
import type { RunState, View } from '../types'

type Props = {
  tab: View
  onSelect: (view: View) => void
  running: boolean
  runState: RunState | null
  liveData: LiveData | null
  conflictCount: number
  theme: 'dark' | 'light'
  onToggleTheme: () => void
}

const items: [View, string, ReactNode][] = [
  ['workbench', '任务配置', <SlidersHorizontal size={16} key="w" />],
  ['monitor', '实时情报流', <Activity size={16} key="m" />],
  ['discover', '人物发现', <Users size={16} key="d" />],
  ['review', '复核与冲突', <ShieldQuestion size={16} key="r" />],
  ['history', '版本与备份', <History size={16} key="h" />]
]

export default function SideNav({ tab, onSelect, running, runState, liveData, conflictCount, theme, onToggleTheme }: Props) {
  return (
    <aside className="side">
      <div className="brand">
        <span className="brand-mark"><Crosshair size={17} /></span>
        <div>
          <b>TARGET·NPL</b>
          <small>公开内容找同好</small>
        </div>
      </div>
      <nav className="nav">
        {items.map(([id, label, icon]) => (
          <button key={id} type="button" className="nav-item" aria-current={tab === id ? 'page' : undefined} onClick={() => onSelect(id)}>
            {icon}
            <span>{label}</span>
            {id === 'monitor' && running && <span className="live-dot on" />}
            {id === 'review' && conflictCount > 0 && <span className="nav-badge">{conflictCount}</span>}
          </button>
        ))}
      </nav>
      <div className="side-foot">
        <button type="button" className="run-mini" onClick={() => onSelect('monitor')}>
          <span className="run-mini-top">
            <span className={`live-dot ${running ? 'on' : ''}`} style={{ '--dc': running ? 'var(--accent)' : 'var(--text-3)' } as CSSProperties} />
            {running ? phaseNames[runState?.phase || ''] || '运行中' : runState ? '空闲 · 上次已结束' : '空闲'}
          </span>
          <span className="run-mini-sub">
            {liveData ? `${liveData.captured_total} 条 · 精准 ${liveData.exact_count ?? 0} · 潜在 ${liveData.potential_count ?? 0}` : '暂无运行数据'}
          </span>
        </button>
        <div className="side-tools">
          <span>{theme === 'dark' ? '深色' : '浅色'}主题</span>
          <button type="button" className="btn btn-ghost btn-sm icon-btn" onClick={onToggleTheme} aria-label="切换明暗主题" title="切换明暗主题">
            {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
          </button>
        </div>
      </div>
    </aside>
  )
}
