import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Activity, Ban, ExternalLink, FileText, FlaskConical, Pause, Play, Radio, Search, Users } from 'lucide-react'
import { platformMeta } from './platforms'
import { phaseNames } from './components/TopStatusBar'

export interface CandidateItem {
  id: string
  platform: string
  text: string
  reason?: string
  evidence?: Record<string, any>
  url?: string
  author?: string
  author_url?: string
  author_key?: string
  score?: number
  evidence_level?: string
  quality_flags?: string[]
  accepted?: boolean
  intent?: string
  stance?: string
  kind?: string
  keyword?: string
}

export interface CapturedItem {
  id: string
  platform: string
  kind?: string
  text: string
  keyword?: string
  published_at?: string
}

export interface LiveData {
  run_id: string
  status: string
  phase?: string
  collection: {
    requests?: number
    active_platform?: string
    platforms?: string[]
    platform_statuses?: { platform: string; status: string; requests?: number }[]
  }
  captured: CapturedItem[]
  captured_total: number
  candidates: CandidateItem[]
  candidate_total: number
  exact_count?: number
  potential_count?: number
  processed: number
  needs_review: number
  report_url: string | null
}

export interface PersonCard {
  personKey: string
  platform: string
  authorName: string
  authorUrl?: string
  highestScore: number
  isExact: boolean
  isPotential: boolean
  records: CandidateItem[]
}

type FeedItem = CapturedItem & { at: string; fresh: boolean }

const statusNames: Record<string, string> = {
  completed_empty: '采集为空',
  not_started_budget: '额度不足未启动',
  queued: '等待启动',
  running: '运行中',
  completed: '已完成',
  completed_with_review: '已完成 · 有待复核',
  failed: '失败',
  interrupted: '已中断',
  needs_user_login: '等待本人登录',
  partial: '部分完成'
}

// 平台通道状态：[文案, 视觉状态, 颜色]
const laneStates: Record<string, [string, string, string]> = {
  running: ['采集中', 'running', 'var(--accent)'],
  completed: ['完成', 'done', 'var(--text-2)'],
  completed_empty: ['无结果', 'done', 'var(--text-3)'],
  needs_user_login: ['需登录', 'login', 'var(--warn)'],
  failed: ['失败', 'failed', 'var(--danger)'],
  partial: ['部分完成', 'done', 'var(--warn)'],
  not_started_budget: ['额度不足', 'failed', 'var(--text-3)']
}

const FEED_LIMIT = 400
const clock = () => new Date().toLocaleTimeString('zh-CN', { hour12: false })
const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

function highlight(text: string, words: string[]): ReactNode {
  const list = Array.from(new Set(words.map(w => w.trim()).filter(Boolean)))
  if (!list.length) return text
  const parts = text.split(new RegExp(`(${list.map(escapeRegex).join('|')})`, 'gi'))
  return parts.map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : part))
}

function shortDate(iso?: string) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

function useCountUp(value: number) {
  const [shown, setShown] = useState(value)
  const current = useRef(value)
  useEffect(() => {
    const start = current.current
    const delta = value - start
    if (!delta) return
    if (typeof requestAnimationFrame !== 'function') {
      current.current = value
      setShown(value)
      return
    }
    let frame = 0
    const t0 = performance.now()
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / 700)
      const v = Math.round(start + delta * (1 - Math.pow(1 - p, 3)))
      current.current = v
      setShown(v)
      if (p < 1) frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [value])
  return shown
}

function Kpi({ label, value, unit, sub, tier, icon, children }: {
  label: string; value: number; unit?: string; sub?: string; tier?: 'exact' | 'potential'; icon?: ReactNode; children?: ReactNode
}) {
  const shown = useCountUp(value)
  return (
    <div className={`kpi ${tier || ''}`} style={tier ? ({ '--tier': `var(--${tier})` } as CSSProperties) : undefined}>
      <div className="kpi-label">{icon}{label}</div>
      <div className="kpi-value">{shown.toLocaleString('zh-CN')}{unit && <small>{unit}</small>}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
      {children}
    </div>
  )
}

function Sparkline({ points }: { points: number[] }) {
  const w = 160, h = 44
  const values = points.length > 1 ? points : [0, 0]
  const max = Math.max(1, ...values)
  const step = w / (values.length - 1)
  const coords = values.map((v, i) => [i * step, h - 3 - (v / max) * (h - 8)])
  const line = coords.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--accent)" stopOpacity="0.28" />
          <stop offset="1" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line} L${w},${h} L0,${h} Z`} fill="url(#spark-fill)" />
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

export default function RunMonitor({
  run,
  live,
  error,
  onSendToTest,
  onAddBlacklistWord,
  onRequestOpenLogin
}: {
  run: { run_id: string; status: string; phase?: string; result?: { report: string } } | null
  live: LiveData | null
  error?: string
  onSendToTest?: (text: string, context?: string) => void
  onAddBlacklistWord?: (word: string) => void
  onRequestOpenLogin?: (platformId: string) => void
}) {
  const [personTab, setPersonTab] = useState<'exact' | 'potential'>('exact')
  const [includeText, setIncludeText] = useState('')
  const [excludeText, setExcludeText] = useState('')
  const [feed, setFeed] = useState<FeedItem[]>([])
  const [queued, setQueued] = useState(0)
  const [hovering, setHovering] = useState(false)
  const [manualPause, setManualPause] = useState(false)
  const [throughput, setThroughput] = useState<number[]>([])
  const seen = useRef(new Set<string>())
  const queue = useRef<CapturedItem[]>([])
  const lastTotal = useRef<number | null>(null)
  const running = !!run && ['queued', 'running'].includes(run.status)
  const paused = hovering || manualPause

  // 切换批次时清空信息流
  useEffect(() => {
    seen.current = new Set()
    queue.current = []
    lastTotal.current = null
    setFeed([])
    setQueued(0)
    setThroughput([])
  }, [run?.run_id])

  // 新到达的记录先进入队列，再匀速放出，形成连续滚动的信息流
  useEffect(() => {
    if (!live) return
    const fresh = (live.captured || []).filter(item => !seen.current.has(item.id))
    const firstBatch = seen.current.size === 0
    fresh.forEach(item => seen.current.add(item.id))
    if (fresh.length) {
      if (firstBatch || !running) {
        const at = clock()
        setFeed(prev => [...fresh.map(item => ({ ...item, at, fresh: false })).reverse(), ...prev].slice(0, FEED_LIMIT))
      } else {
        queue.current.push(...fresh)
        setQueued(queue.current.length)
      }
    }
    const total = live.captured_total || 0
    if (lastTotal.current !== null) setThroughput(prev => [...prev, Math.max(0, total - lastTotal.current!)].slice(-36))
    lastTotal.current = total
  }, [live])

  useEffect(() => {
    if (paused) return
    const timer = setInterval(() => {
      if (!queue.current.length) return
      const batch = queue.current.splice(0, Math.max(1, Math.ceil(queue.current.length / 10)))
      const at = clock()
      setFeed(prev => [...batch.map(item => ({ ...item, at, fresh: true })).reverse(), ...prev].slice(0, FEED_LIMIT))
      setQueued(queue.current.length)
    }, 240)
    return () => clearInterval(timer)
  }, [paused])

  const includeWords = useMemo(() => includeText.split(/[\s,，]+/).map(s => s.trim().toLowerCase()).filter(Boolean), [includeText])
  const excludeWords = useMemo(() => excludeText.split(/[\s,，]+/).map(s => s.trim().toLowerCase()).filter(Boolean), [excludeText])
  const passes = (text: string) => {
    const lower = text.toLowerCase()
    if (includeWords.length && !includeWords.some(w => lower.includes(w))) return false
    if (excludeWords.length && excludeWords.some(w => lower.includes(w))) return false
    return true
  }

  // 将命中记录按“人”归拢
  const groupedPersons = useMemo(() => {
    const map = new Map<string, PersonCard>()
    for (const item of live?.candidates || []) {
      const isExact = item.evidence_level === 'explicit' || (!item.evidence_level && (item.score || 0) >= 70)
      const isPotential = item.evidence_level === 'related_only' || (item.quality_flags || []).includes('tier_potential') || (!isExact && (item.score || 0) >= 50)
      const key = item.author_key || `${item.platform}:${item.author || item.id}`
      const existing = map.get(key)
      if (!existing) {
        map.set(key, { personKey: key, platform: item.platform, authorName: item.author?.trim() || '公开平台用户', authorUrl: item.author_url, highestScore: item.score || 0, isExact, isPotential, records: [item] })
      } else {
        existing.records.push(item)
        existing.highestScore = Math.max(existing.highestScore, item.score || 0)
        existing.isExact ||= isExact
        existing.isPotential ||= isPotential
      }
    }
    return Array.from(map.values()).sort((a, b) => b.highestScore - a.highestScore)
  }, [live?.candidates])

  const exactCount = groupedPersons.filter(p => p.isExact).length
  const potentialCount = groupedPersons.filter(p => !p.isExact && p.isPotential).length
  const visiblePersons = groupedPersons.filter(p =>
    (personTab === 'exact' ? p.isExact : !p.isExact && p.isPotential) && passes(p.records.map(r => r.text).join(' '))
  )
  const visibleFeed = feed.filter(item => passes(item.text))

  if (!run) {
    return (
      <div className="card hero-empty empty">
        <div className="empty-ring"><Radio size={20} /></div>
        <h2>还没有运行记录</h2>
        <p>在「任务配置」里设置好词库和平台后点击「开始抓取」，采集内容和命中人物会在这里实时滚动出现。</p>
      </div>
    )
  }

  const statuses = live?.collection?.platform_statuses || []
  const lanePlatforms = Array.from(new Set([...(live?.collection?.platforms || []), ...statuses.map(s => s.platform)].filter(Boolean)))
  const reportUrl = live?.report_url || (run.result?.report ? `/reports/${run.run_id}` : null)
  const perMinute = throughput.length ? Math.round((throughput.slice(-8).reduce((a, b) => a + b, 0) / Math.min(8, throughput.length)) * 40) : 0
  const blacklist = () => {
    const words = excludeText.split(/[\s,，]+/).map(w => w.trim()).filter(Boolean)
    words.forEach(w => onAddBlacklistWord?.(w))
  }

  return (
    <section className="monitor" aria-label="实时情报流">
      <div className="run-head">
        <div className="run-head-left">
          <span className="status-pill">
            <span className={`live-dot ${running ? 'on' : ''}`} style={{ '--dc': running ? 'var(--accent)' : run.status.startsWith('completed') ? 'var(--text-2)' : 'var(--warn)' } as CSSProperties} />
            {statusNames[run.status] || run.status}
          </span>
          <span className="run-meta">{phaseNames[run.phase || ''] || run.phase || '—'} · 批次 {run.run_id.slice(0, 8)}</span>
        </div>
        {reportUrl && (
          <a className="btn btn-sm" href={reportUrl} target="_blank" rel="noreferrer">
            <FileText size={13} />查看完整报告<ExternalLink size={12} />
          </a>
        )}
      </div>

      {error && <div className="lock-note" style={{ color: 'var(--danger)' }}>读取实时数据失败：{error}</div>}

      <div className="kpis">
        <Kpi label="已采集内容" value={live?.captured_total ?? 0} unit="条" sub={running ? `≈ ${perMinute} 条 / 分钟` : `请求 ${live?.collection?.requests ?? 0} 次`} icon={<Activity size={13} />}>
          {throughput.length > 1 && <Sparkline points={throughput} />}
        </Kpi>
        <Kpi label="精准目标" value={exactCount} unit="人" tier="exact" sub="明确找搭子 / 同行" />
        <Kpi label="潜在沾边" value={potentialCount} unit="人" tier="potential" sub="正向赞美 / 感兴趣" />
        <Kpi label="模型已分析" value={live?.processed ?? 0} unit="条" sub={`待复核 ${live?.needs_review ?? 0}`} />
        <Kpi label="采集请求" value={live?.collection?.requests ?? 0} unit="次" sub={live?.collection?.active_platform ? `当前 ${platformMeta(live.collection.active_platform).name}` : '各平台合计'} />
      </div>

      {lanePlatforms.length > 0 && (
        <div className="lanes">
          {lanePlatforms.map(id => {
            const meta = platformMeta(id)
            const entry = statuses.find(s => s.platform === id)
            // 运行已结束但平台仍停在“采集中”，说明该平台进程被中断
            const stale = !running && entry?.status === 'running'
            const [text, visual, color] = stale ? ['已中断', 'failed', 'var(--warn)'] : entry ? laneStates[entry.status] || [entry.status, 'done', 'var(--text-3)'] : running ? ['排队中', 'pending', 'var(--text-3)'] : ['未执行', 'pending', 'var(--text-3)']
            return (
              <div key={id} className="lane" data-state={visual} style={{ '--pc': meta.color, '--sc': color } as CSSProperties}>
                <div className="lane-top">
                  <span className="pglyph">{meta.glyph}</span>{meta.name}
                  <span className="lane-state">{text}</span>
                </div>
                <div className="lane-bar"><i /></div>
                <div className="lane-foot">
                  <span>{entry?.requests ?? 0} 请求</span>
                  {visual === 'login' && onRequestOpenLogin && <button type="button" onClick={() => onRequestOpenLogin(id)}>弹出窗口</button>}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <div className="stage">
        <div className="card">
          <div className="panel-head">
            <h2>实时信息流 <span className={`live-badge ${running ? '' : 'off'}`}>{running && <span className="live-dot on" style={{ width: 6, height: 6 }} />}{running ? 'LIVE' : 'REPLAY'}</span></h2>
            <div className="top-actions">
              <span className="count">{visibleFeed.length} / {live?.captured_total ?? 0}</span>
              <button type="button" className="btn btn-sm btn-ghost" onClick={() => setManualPause(v => !v)} aria-pressed={manualPause}>
                {manualPause ? <Play size={13} /> : <Pause size={13} />}{manualPause ? '继续滚动' : '暂停滚动'}
              </button>
            </div>
          </div>
          <div className="filters">
            <label className="search">
              <Search size={14} />
              <input value={includeText} onChange={e => setIncludeText(e.target.value)} placeholder="只看包含，如：示例城市乙 周末" aria-label="只看包含" />
            </label>
            <label className="search">
              <Ban size={14} />
              <input value={excludeText} onChange={e => setExcludeText(e.target.value)} placeholder="排除包含，如：教练 卖课" aria-label="排除包含" />
            </label>
            {excludeText.trim() && onAddBlacklistWord && (
              <button type="button" className="btn btn-sm btn-danger" onClick={blacklist} title="把排除词永久写入任务黑名单（需保存）">加入黑名单</button>
            )}
          </div>
          <div className="scroll" onMouseEnter={() => setHovering(true)} onMouseLeave={() => setHovering(false)}>
            {paused && queued > 0 && <div className="queue-pill"><Pause size={12} />已暂停 · {queued} 条新内容等待</div>}
            {visibleFeed.length ? (
              visibleFeed.map(item => {
                const meta = platformMeta(item.platform)
                return (
                  <article key={item.id} className={`feed-row ${item.fresh ? 'fresh' : ''}`}>
                    <time>{item.at}</time>
                    <div>
                      <div className="feed-meta">
                        <span className="pchip" style={{ '--pc': meta.color } as CSSProperties}>{meta.name}</span>
                        <span className="tag">{item.kind === 'comment' ? '评论' : '作品'}</span>
                        {item.keyword && <span className="kw">#{item.keyword}</span>}
                        {shortDate(item.published_at) && <span className="mono">{shortDate(item.published_at)}</span>}
                      </div>
                      <p>{item.text ? highlight(item.text, [...includeWords, item.keyword || '']) : '（无文字内容）'}</p>
                    </div>
                  </article>
                )
              })
            ) : (
              <div className="empty">
                <div className="empty-ring"><Activity size={18} /></div>
                {running ? '正在后台采集，内容到达后会从这里滚动进入…' : feed.length ? '没有符合筛选条件的内容' : '这一批次没有采集到内容'}
              </div>
            )}
          </div>
        </div>

        <div className="card">
          <div className="panel-head">
            <h2><Users size={15} />命中人物</h2>
            <div className="segmented" role="group" aria-label="人物分栏">
              <button type="button" aria-pressed={personTab === 'exact'} onClick={() => setPersonTab('exact')} style={{ '--tier': 'var(--exact)' } as CSSProperties}>
                精准目标 <em>{exactCount}</em>
              </button>
              <button type="button" aria-pressed={personTab === 'potential'} onClick={() => setPersonTab('potential')} style={{ '--tier': 'var(--potential)' } as CSSProperties}>
                潜在沾边 <em>{potentialCount}</em>
              </button>
            </div>
          </div>
          <div className="scroll">
            {visiblePersons.length ? (
              <div className="people">
                {visiblePersons.map(person => {
                  const meta = platformMeta(person.platform)
                  const tier = person.isExact ? 'var(--exact)' : 'var(--potential)'
                  return (
                    <article key={person.personKey} className="person" style={{ '--tier': tier } as CSSProperties}>
                      <div className="person-head">
                        <span className="avatar">{person.authorName.slice(0, 1)}</span>
                        <div style={{ minWidth: 0 }}>
                          <div className="person-name">{person.authorName}</div>
                          <div className="person-sub">
                            <span className="pchip" style={{ '--pc': meta.color } as CSSProperties}>{meta.name}</span>
                            <span>{person.records.length} 条发言命中</span>
                          </div>
                        </div>
                        <span className="score">{person.highestScore}</span>
                      </div>
                      {person.records.slice(0, 3).map((rec, i) => (
                        <div key={rec.id || i} className="quote">
                          {highlight(rec.text, includeWords)}
                          {rec.reason && <small>{rec.reason}</small>}
                          <div className="quote-actions">
                            {onSendToTest && <button type="button" onClick={() => onSendToTest(rec.text, '')}>测此条</button>}
                            {onAddBlacklistWord && (
                              <button type="button" onClick={() => {
                                const word = prompt('输入要加入黑名单的词：', '')
                                if (word?.trim()) onAddBlacklistWord(word.trim())
                              }}>加黑名单</button>
                            )}
                          </div>
                        </div>
                      ))}
                      <div className="person-actions">
                        {person.authorUrl && <a className="btn btn-sm btn-ghost" href={person.authorUrl} target="_blank" rel="noreferrer">主页<ExternalLink size={12} /></a>}
                        {person.records[0]?.url && <a className="btn btn-sm btn-ghost" href={person.records[0].url} target="_blank" rel="noreferrer">原帖<ExternalLink size={12} /></a>}
                        {onSendToTest && <button type="button" className="btn btn-sm btn-ghost" onClick={() => onSendToTest(person.records[0]?.text || '', '')}><FlaskConical size={12} />送入试金石</button>}
                      </div>
                    </article>
                  )
                })}
              </div>
            ) : (
              <div className="empty">
                <div className="empty-ring"><Users size={18} /></div>
                {running ? '模型分析后，命中的人物会实时出现在这里' : '这一栏暂无人物，可放宽词库或调整筛选'}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
