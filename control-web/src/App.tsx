import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Crosshair,
  Download,
  FlaskConical,
  History,
  Loader2,
  Lock,
  Moon,
  Play,
  RotateCcw,
  Save,
  SlidersHorizontal,
  Sparkles,
  Sun,
  Upload,
  X
} from 'lucide-react'
import PlatformPicker from './PlatformPicker'
import { platformNames } from './platforms'
import RunMonitor, { type LiveData } from './RunMonitor'
import TagInput from './components/TagInput'
import TopStatusBar, { phaseNames } from './components/TopStatusBar'
import {
  updatePatterns,
  mergeSavedEdits,
  PRESET_TEMPLATES,
  patternsToTags,
  tagsToPatterns,
  syncSimpleWordsToPolicy,
  type PresetTemplate
} from './presets'

export type Policy = {
  schema_version: number
  id: string
  name: string
  topic_patterns: string[]
  ambiguous_reply_patterns: string[]
  dimensions: Record<string, string[]>
  rules: Record<string, string[]>
  decision_gates?: Record<string, string[]>
  acceptance: { stances: string[]; intents: string[] }
  display: { sort_weights: Record<string, number>; include_related: boolean }
}

export type Collection = {
  schema_version: number
  platform: string
  platforms?: string[]
  keywords: string[]
  pages_per_keyword: number
  posts_per_keyword: number
  max_posts: number
  comments_per_post: number
  request_interval_seconds: number
  max_requests: number
  timeout_seconds: number
  proxy_mode: string
}

export type Task = {
  schema_version: number
  description: string
  scope: {
    locations: string[]
    recent_days: number | null
    published_after: string | null
    published_before: string | null
  }
  exclusions: {
    promotions: boolean
    terms: string[]
  }
  execution: {
    source: 'existing' | 'collect'
    dataset_id: string
    mode: 'rules' | 'ark'
    max_api_calls: number
  }
  assumptions: string[]
  unresolved: string[]
  human_overrides: string[]
}

export type Bundle = {
  policy: Policy
  collection: Collection
  task?: Task
}

type State = {
  bundle: Bundle
  revision: string
  etag: string
  token?: string
}

type Version = {
  revision: string
  created_at: string
  name: string
}

type RunState = {
  run_id: string
  status: string
  phase?: string
  message?: string
  collection_incomplete?: boolean
  result?: { candidate_contents: number; unique_contents: number; report: string }
}

type PreviewResult = {
  accepted: boolean
  score: number
  evidence_level: string
  quality_flags: string[]
  topic: string
  stance: string
  intent: string
  promotion: string
  author_role: string
  reason: string
  scope_check?: {
    status: string
    reasons: string[]
  }
  evidence?: Record<string, any>
}

type View = 'workbench' | 'monitor' | 'history'

export const makeTask = (bundle: Bundle): Task => ({
  schema_version: 1,
  description: bundle.policy.name,
  scope: { locations: [], recent_days: null, published_after: null, published_before: null },
  exclusions: { promotions: true, terms: [] },
  execution: { source: 'collect', dataset_id: 'latest', mode: 'ark', max_api_calls: 100 },
  assumptions: [],
  unresolved: [],
  human_overrides: []
})

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T
const THEME_KEY = 'npl_theme'
const readTheme = () => {
  try { return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark' } catch { return 'dark' }
}

const pages: Record<View, [string, string]> = {
  workbench: ['任务配置', '描述目标、维护词库、选择平台，然后开始抓取'],
  monitor: ['实时情报流', '后台采集与模型筛选的结果在这里实时滚动'],
  history: ['版本与备份', '每次保存都会生成快照，可随时恢复或导入导出']
}

function Card({ step, title, desc, aside, children, className = '' }: { step?: string; title: string; desc?: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      <div className="card-head">
        <div>
          <div className="card-title">{step && <span className="step">{step}</span>}<h2>{title}</h2></div>
          {desc && <p className="card-desc">{desc}</p>}
        </div>
        {aside && <div className="card-aside">{aside}</div>}
      </div>
      <div className="card-body">{children}</div>
    </section>
  )
}

export default function App() {
  const [active, setActive] = useState<State | null>(null)
  const [draft, setDraft] = useState<Bundle | null>(null)
  const [tab, setTab] = useState<View>('workbench')
  const [versions, setVersions] = useState<Version[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [runState, setRunState] = useState<RunState | null>(null)
  const [liveData, setLiveData] = useState<LiveData | null>(null)
  const [liveError, setLiveError] = useState('')
  const [theme, setTheme] = useState<'dark' | 'light'>(readTheme)

  // 自然语言目标输入
  const [goalText, setGoalText] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [jsonText, setJsonText] = useState('')

  // 试金石输入与状态
  const [testContextText, setTestContextText] = useState('')
  const [testCommentText, setTestCommentText] = useState('太酷了，有人一起吗？求搭子交流！')
  const [testPreview, setTestPreview] = useState<PreviewResult | null>(null)
  const [testBusy, setTestBusy] = useState(false)

  const draftRef = useRef<Bundle | null>(null)
  const token = useRef('')
  const editSequence = useRef(0)
  const planRef = useRef<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem(THEME_KEY, theme) } catch {}
  }, [theme])

  // 成功提示自动消失，错误提示保留到手动关闭
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => setMessage(''), 4200)
    return () => clearTimeout(timer)
  }, [message])

  // API 客户端
  async function api<T>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/json', 'X-Control-Token': token.current } : {},
      body: body ? JSON.stringify(body) : undefined
    })
    const data = await res.json()
    if (!res.ok) throw Error(data.error || '请求失败')
    return data as T
  }

  function loadDraft(bundle: Bundle) {
    const next = clone(bundle)
    next.task = next.task || makeTask(next)
    draftRef.current = next
    setDraft(next)
    setGoalText(next.task.description || '')
    setJsonText(JSON.stringify(next, null, 2))
    editSequence.current++
    setError('')
  }

  // 统一草稿修改派发
  function updateDraft(mutator: (b: Bundle) => void) {
    if (!draftRef.current) return
    const next = clone(draftRef.current)
    mutator(next)
    draftRef.current = next
    setDraft(next)
    setJsonText(JSON.stringify(next, null, 2))
    editSequence.current++
    setMessage('')
    setError('')
  }

  // 初始化加载配置与状态
  useEffect(() => {
    let disposed = false
    Promise.all([api<State>('/api/state'), api<Version[]>('/api/versions')])
      .then(([state, hist]) => {
        if (disposed) return
        token.current = state.token || ''
        setActive(state)
        loadDraft(state.bundle)
        setVersions(hist)
        api<RunState | null>('/api/tasks/latest')
          .then(v => { if (!disposed && v) setRunState(v) })
          .catch(() => {})
      })
      .catch(e => { if (!disposed) setError(String(e.message)) })
    return () => { disposed = true }
  }, [])

  // 唯一的运行状态与实时数据轮询源
  const liveRunId = useRef<string | null>(null)
  useEffect(() => {
    if (!runState) return
    const runId = runState.run_id
    if (liveRunId.current !== runId) {
      liveRunId.current = runId
      setLiveData(null)
    }
    const running = ['queued', 'running'].includes(runState.status)
    let cancelled = false
    let pending = false
    const tick = async () => {
      if (pending) return
      pending = true
      try {
        const [status, live] = await Promise.all([
          running ? api<RunState>('/api/tasks/status', { run_id: runId }) : Promise.resolve(null),
          api<LiveData>('/api/tasks/live', { run_id: runId })
        ])
        if (cancelled) return
        if (live) setLiveData(live)
        if (status) setRunState(status)
        setLiveError('')
      } catch (e) {
        if (!cancelled) setLiveError((e as Error).message)
      } finally {
        pending = false
      }
    }
    void tick()
    const timer = running ? setInterval(tick, 1500) : null
    return () => {
      cancelled = true
      if (timer) clearInterval(timer)
    }
  }, [runState?.run_id, runState?.status])

  const dirty = !!draft && !!active && JSON.stringify(draft) !== JSON.stringify(active.bundle)
  const running = !!runState && ['queued', 'running'].includes(runState.status)

  // 四类纯文字标签（与底层规则双向转换）
  const topicWords = patternsToTags(draft?.policy.topic_patterns || [])
  const exactSignalWords = patternsToTags(draft?.policy.rules?.intent_seeking_company || [])
  const praiseWords = patternsToTags(draft?.policy.rules?.stance_positive || [])
  const excludeWords = Array.from(new Set([...(draft?.task?.exclusions.terms || []), ...patternsToTags(draft?.policy.rules?.promotion_yes || [])]))

  // 纯文字标签修改同步回底层规则与门禁（只局部更新对应类别，不破坏其他维度规则）
  const handleUpdateWords = (type: 'topic' | 'exact' | 'praise' | 'exclude', newTags: string[]) => {
    updateDraft(b => {
      b.policy.rules = b.policy.rules || {}
      b.policy.decision_gates = b.policy.decision_gates || {}
      if (type === 'topic') {
        b.policy.topic_patterns = newTags.length > 0 ? updatePatterns(b.policy.topic_patterns, newTags) : ['搭子']
      } else if (type === 'exact') {
        b.policy.decision_gates.intent_seeking_company = updatePatterns(b.policy.decision_gates.intent_seeking_company || b.policy.rules.intent_seeking_company || [], newTags)
        b.policy.rules.intent_seeking_company = updatePatterns(b.policy.rules.intent_seeking_company || [], newTags)
      } else if (type === 'praise') {
        b.policy.decision_gates.stance_positive = updatePatterns(b.policy.decision_gates.stance_positive || b.policy.rules.stance_positive || [], newTags)
        b.policy.rules.stance_positive = updatePatterns(b.policy.rules.stance_positive || [], newTags)
      } else if (type === 'exclude') {
        b.policy.decision_gates.promotion_yes = updatePatterns(b.policy.rules.promotion_yes || [], newTags)
        b.policy.rules.promotion_yes = updatePatterns(b.policy.rules.promotion_yes || [], newTags)
        if (b.task && b.task.exclusions) {
          b.task.exclusions.terms = Array.from(new Set(newTags.filter(w => !/^已有复杂规则 \d+$/.test(w)).map(w => w.trim()).filter(Boolean)))
          b.task.exclusions.promotions = true
        }
      }
    })
  }

  const applyPreset = (preset: PresetTemplate) => {
    updateDraft(b => {
      b.policy.name = preset.name.replace(/^[\p{Emoji}\s]+/u, '')
      b.collection.keywords = clone(preset.keywords)
      if (b.task) {
        b.task.description = preset.description
        b.task.human_overrides = []
        setGoalText(preset.description)
      }
      syncSimpleWordsToPolicy(
        b.policy,
        { topicWords: preset.topicWords, exactSignalWords: preset.exactSignalWords, praiseWords: preset.praiseWords, excludeWords: preset.excludeWords },
        b.task
      )
    })
    setMessage(`已载入「${preset.name}」词库，人工锁定已清空`)
  }

  // 保存草稿；保存往返期间若有新编辑则保留新编辑
  async function persistDraft(): Promise<State> {
    const seq = editSequence.current
    const snapshot = clone(draft!)
    const saved = await api<State>(planRef.current ? '/api/tasks/save-draft' : '/api/save', {
      bundle: snapshot,
      etag: active!.etag,
      ...(planRef.current ? { plan_id: planRef.current } : {})
    })
    planRef.current = null
    setActive({ ...saved, token: token.current })
    if (editSequence.current === seq) {
      loadDraft(saved.bundle)
    } else if (draftRef.current) {
      const kept = mergeSavedEdits(draftRef.current, saved.bundle, snapshot)
      draftRef.current = kept
      setDraft(kept)
      setJsonText(JSON.stringify(kept, null, 2))
    }
    return saved
  }

  async function saveDraft() {
    if (!draft || !active) return
    setBusy(true)
    setError('')
    try {
      await persistDraft()
      setMessage('已保存，下次抓取将使用这一版本')
      try { setVersions(await api<Version[]>('/api/versions')) } catch {}
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function saveAndStart() {
    if (!draft || !active) return
    setBusy(true)
    setError('')
    try {
      const saved = await persistDraft()
      const startedRun = await api<RunState>('/api/tasks/run', { revision: saved.revision, request_id: crypto.randomUUID() })
      setRunState(startedRun)
      setTab('monitor')
      setMessage('已开始抓取，浏览器在后台运行；只有需要登录或验证时才会弹出')
      try { setVersions(await api<Version[]>('/api/versions')) } catch {}
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function handleAIGenerate() {
    if (!active || !goalText.trim()) return
    setBusy(true)
    setError('')
    try {
      let currentEtag = active.etag
      // 有未保存修改时先保存，确保 base_etag 一致
      if (dirty && draft) {
        const savedState = await api<State>('/api/save', { bundle: clone(draft), etag: active.etag })
        setActive({ ...savedState, token: token.current })
        currentEtag = savedState.etag
      }
      const proposal = await api<{ plan_id: string; bundle: Bundle; can_apply: boolean }>('/api/tasks/plan', { goal: goalText, base_etag: currentEtag })
      loadDraft(proposal.bundle)
      planRef.current = proposal.plan_id
      setMessage('AI 已生成推荐词库与参数，请核对后保存')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function preview(text: string, context?: string) {
    if (!draft) return
    setTestBusy(true)
    try {
      setTestPreview(await api<PreviewResult>('/api/preview', { policy: draft.policy, text, context_text: context?.trim() || undefined, task: draft.task }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setTestBusy(false)
    }
  }

  const handleSendToTest = (comment: string, context?: string) => {
    setTestCommentText(comment)
    setTestContextText(context || '')
    setTab('workbench')
    setMessage('已送入试金石并自动判定')
    void preview(comment, context)
  }

  const handleAddBlacklistWord = (input: string) => {
    if (!input || !draft) return
    const newItems = input.split(/[\s,，、|]+/).map(w => w.trim()).filter(Boolean)
    if (newItems.length === 0) return
    updateDraft(b => {
      const merged = Array.from(new Set([...(b.task?.exclusions.terms || []), ...newItems]))
      if (b.task?.exclusions) {
        b.task.exclusions.terms = merged
        b.task.exclusions.promotions = true
      }
      const patterns = tagsToPatterns(merged)
      b.policy.rules = b.policy.rules || {}
      b.policy.decision_gates = b.policy.decision_gates || {}
      b.policy.rules.promotion_yes = Array.from(new Set([...(b.policy.rules.promotion_yes || []), ...patterns]))
      b.policy.decision_gates.promotion_yes = Array.from(new Set([...(b.policy.decision_gates.promotion_yes || []), ...patterns]))
    })
    setMessage(`已将「${newItems.join('、')}」加入黑名单，保存后生效`)
  }

  const handleOpenLogin = async (platformId: string) => {
    try {
      const reply = await api<{ message: string }>('/api/platforms/open-login', { platform: platformId })
      setMessage(reply.message || `已弹出${platformNames[platformId] || platformId}登录窗口`)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  async function importFile(file: File) {
    setBusy(true)
    setError('')
    try {
      const parsed = JSON.parse(await file.text())
      const b: Bundle = parsed.schema_version === 2 ? { ...clone(draftRef.current!), policy: parsed } : parsed.bundle || (parsed.policy && parsed.collection ? parsed : null)
      if (!b) throw Error('无效的规则 JSON 文件')
      await api('/api/validate', { bundle: b })
      loadDraft(b)
      setMessage(`已导入 ${file.name}，检查后保存即可生效`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function exportFile() {
    if (!draft) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${draft.policy.id || 'target-npl'}-rules.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (!draft || !active) {
    return (
      <main className="boot">
        <span className="brand-mark"><Crosshair size={22} /></span>
        <b style={{ color: 'var(--text)' }}>Target-NPL</b>
        <span>{error || '正在连接本地引擎…'}</span>
      </main>
    )
  }

  const task = draft.task!
  const loginNeeded = (liveData?.collection?.platform_statuses || []).filter(p => p.status === 'needs_user_login').map(p => p.platform)
  const navItems: [View, string, ReactNode][] = [
    ['workbench', '任务配置', <SlidersHorizontal size={16} key="w" />],
    ['monitor', '实时情报流', <Activity size={16} key="m" />],
    ['history', '版本与备份', <History size={16} key="h" />]
  ]
  const verdict = testPreview
    ? testPreview.accepted
      ? testPreview.evidence_level === 'explicit' || testPreview.quality_flags?.includes('tier_exact')
        ? { label: '精准目标', tier: 'var(--exact)' }
        : { label: '潜在沾边', tier: 'var(--potential)' }
      : { label: '已剔除', tier: 'var(--danger)' }
    : null

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <span className="brand-mark"><Crosshair size={17} /></span>
          <div>
            <b>TARGET·NPL</b>
            <small>公开内容找同好</small>
          </div>
        </div>
        <nav className="nav">
          {navItems.map(([id, label, icon]) => (
            <button key={id} type="button" className="nav-item" aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}>
              {icon}
              <span>{label}</span>
              {id === 'monitor' && running && <span className="live-dot on" />}
            </button>
          ))}
        </nav>
        <div className="side-foot">
          <button type="button" className="run-mini" onClick={() => setTab('monitor')}>
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
            <button type="button" className="btn btn-ghost btn-sm icon-btn" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} aria-label="切换明暗主题" title="切换明暗主题">
              {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="page-title">
            <h1>{pages[tab][0]}</h1>
            <p>{pages[tab][1]}</p>
          </div>
          <div className="top-actions">
            {dirty && <span className="dirty"><i />有未保存的修改</span>}
            <button type="button" className="btn" onClick={saveDraft} disabled={busy || !dirty}>
              <Save size={14} />保存草稿
            </button>
            <button type="button" className="btn btn-primary" onClick={saveAndStart} disabled={busy || running}>
              {busy ? <Loader2 size={14} className="spin" /> : <Play size={14} fill="currentColor" />}
              {running ? '运行中' : '开始抓取'}
            </button>
          </div>
        </header>

        <TopStatusBar
          runState={runState}
          liveStats={{
            captured_total: liveData?.captured_total,
            exact_count: liveData?.exact_count,
            potential_count: liveData?.potential_count,
            platform_statuses: liveData?.collection?.platform_statuses
          }}
          onOpenResultsTab={() => setTab('monitor')}
          onRequestOpenLogin={handleOpenLogin}
        />

        <main className="content">
          {tab === 'workbench' && (
            <div className="workbench">
              <div className="stack">
                <Card step="01" title="筛选目标" desc="用一句话描述要找的人，AI 会提炼出搜索词和判定词库；关键词与规则由使用者在本机配置。">
                  <div className="presets">
                    {PRESET_TEMPLATES.map(preset => (
                      <button key={preset.id} type="button" className="preset" onClick={() => applyPreset(preset)}>
                        <b>{preset.name}</b>
                        <span>{preset.description}</span>
                      </button>
                    ))}
                  </div>
                  <div className="goal">
                    <input
                      className="input input-lg"
                      value={goalText}
                      onChange={e => {
                        setGoalText(e.target.value)
                        updateDraft(b => { if (b.task) b.task.description = e.target.value })
                      }}
                      placeholder="输入要分析的公开内容主题与证据条件"
                      aria-label="筛选目标"
                    />
                    <button type="button" className="btn" style={{ height: 44 }} onClick={handleAIGenerate} disabled={busy || !goalText.trim()}>
                      <Sparkles size={14} style={{ color: 'var(--accent)' }} />AI 提炼词库
                    </button>
                  </div>
                  {task.human_overrides.length > 0 && (
                    <div className="lock-note">
                      <span><Lock size={13} />{task.human_overrides.length} 项人工修改已锁定，AI 生成时不会覆盖</span>
                      <button type="button" onClick={() => { updateDraft(b => { if (b.task) b.task.human_overrides = [] }); setMessage('已解除全部人工锁定') }}>
                        全部解锁
                      </button>
                    </div>
                  )}
                </Card>

                <Card step="02" title="词库" desc="回车添加，支持一次粘贴多个；会自动写入底层规则，不需要写正则。">
                  <TagInput label="平台搜索关键词" hint="送到各平台搜索框召回内容" tags={draft.collection.keywords || []} onChange={tags => updateDraft(b => { b.collection.keywords = tags })} placeholder="如：示例活动搭子、找同好" tone="accent" />
                  <TagInput label="主题核心词" hint="内容必须围绕的话题，评论可继承原帖主题" tags={topicWords} onChange={tags => handleUpdateWords('topic', tags)} placeholder="如：示例活动、打球、球馆" tone="violet" />
                  <TagInput label="精准信号词" hint="找搭子、求带等明确表达，命中归入精准目标" tags={exactSignalWords} onChange={tags => handleUpdateWords('exact', tags)} placeholder="如：找搭子、求带、缺人、求拉群" tone="exact" />
                  <TagInput label="潜在信号词" hint="正向赞美或感兴趣，命中归入潜在沾边" tags={praiseWords} onChange={tags => handleUpdateWords('praise', tags)} placeholder="如：太帅了、好喜欢、种草、想去" tone="potential" />
                  <TagInput label="黑名单排除词" hint="出现即一票否决，同时拦截商业推广" tags={excludeWords} onChange={tags => handleUpdateWords('exclude', tags)} placeholder="如：私信领福利、代练、卖课、加微" tone="danger" />
                </Card>

                <Card step="03" title="平台与范围" desc="采集全程在后台进行；只有需要登录或验证时才会弹出对应平台的浏览器。">
                  <PlatformPicker
                    selected={draft.collection.platforms || [draft.collection.platform]}
                    request={api}
                    loginNeeded={loginNeeded}
                    onNotice={(text, isError) => (isError ? setError(text) : setMessage(text))}
                    onChange={ids => updateDraft(b => {
                      b.collection.platforms = ids
                      if (!ids.includes(b.collection.platform)) b.collection.platform = ids[0]
                    })}
                  />
                  <div className="divider" />
                  <div className="grid-2">
                    <TagInput label="活动地点" hint="正文或原帖提到才保留，留空不限" tags={task.scope.locations} onChange={tags => updateDraft(b => { (b.task ??= makeTask(b)).scope.locations = tags })} placeholder="如：示例城市甲、示例城市乙" tone="neutral" />
                    <label className="field">
                      <span className="field-label">最近天数<small>留空不限时间</small></span>
                      <input
                        className="input"
                        type="number"
                        min={1}
                        max={3650}
                        value={task.scope.recent_days ?? ''}
                        onChange={e => updateDraft(b => { (b.task ??= makeTask(b)).scope.recent_days = e.target.value ? Number(e.target.value) : null })}
                        placeholder="不限"
                      />
                    </label>
                  </div>
                  <div className="divider" />
                  <button type="button" className="disclosure" aria-expanded={advancedOpen} onClick={() => setAdvancedOpen(!advancedOpen)}>
                    高级额度与分析方式
                    <small>作品 {draft.collection.max_posts} · 单帖评论 {draft.collection.comments_per_post} · 请求 {draft.collection.max_requests} · 模型 {task.execution.max_api_calls} 次</small>
                    <ChevronDown size={15} />
                  </button>
                  {advancedOpen && (
                    <div className="adv-grid">
                      {([
                        ['作品总上限', 'max_posts', 1, 500],
                        ['单帖评论上限', 'comments_per_post', 0, 100],
                        ['每词页数', 'pages_per_keyword', 1, 20],
                        ['请求上限', 'max_requests', 1, 500]
                      ] as const).map(([label, key, min, max]) => (
                        <label key={key} className="field">
                          <span className="field-label">{label}</span>
                          <input className="input mono" type="number" min={min} max={max} value={draft.collection[key]} onChange={e => updateDraft(b => { b.collection[key] = Number(e.target.value) })} />
                        </label>
                      ))}
                      <label className="field span-2">
                        <span className="field-label">数据来源</span>
                        <select className="select" value={task.execution.source} onChange={e => updateDraft(b => { (b.task ??= makeTask(b)).execution.source = e.target.value as 'collect' | 'existing' })}>
                          <option value="collect">新增多平台采集</option>
                          <option value="existing">复用已有采集数据（免费重筛）</option>
                        </select>
                      </label>
                      <label className="field span-2">
                        <span className="field-label">分析方式</span>
                        <select className="select" value={task.execution.mode} onChange={e => updateDraft(b => { (b.task ??= makeTask(b)).execution.mode = e.target.value as 'ark' | 'rules' })}>
                          <option value="ark">方舟大模型 + 本地规则</option>
                          <option value="rules">仅本地规则（不消耗模型额度）</option>
                        </select>
                      </label>
                      {task.execution.source === 'existing' && (
                        <label className="field span-2">
                          <span className="field-label">复用批次</span>
                          <input className="input mono" value={task.execution.dataset_id} onChange={e => updateDraft(b => { (b.task ??= makeTask(b)).execution.dataset_id = e.target.value })} placeholder="latest 或批次时间戳" />
                        </label>
                      )}
                      <label className="field span-2">
                        <span className="field-label">模型调用预算</span>
                        <input className="input mono" type="number" min={1} max={500} value={task.execution.max_api_calls} onChange={e => updateDraft(b => { (b.task ??= makeTask(b)).execution.max_api_calls = Number(e.target.value) })} />
                      </label>
                    </div>
                  )}
                </Card>
              </div>

              <div className="sandbox">
                <Card title="试金石" desc="用当前词库即时判定一句话会落到哪一栏，不会保存或采集。" aside={<FlaskConical size={15} />}>
                  <div className="stack" style={{ gap: 14 }}>
                    <label className="field">
                      <span className="field-label">原帖标题 / 上下文<small>可选</small></span>
                      <textarea className="textarea" rows={2} value={testContextText} onChange={e => setTestContextText(e.target.value)} placeholder="如：五一示例城市甲示例活动馆约球" />
                    </label>
                    <label className="field">
                      <span className="field-label">待测发言</span>
                      <textarea className="textarea" rows={3} value={testCommentText} onChange={e => setTestCommentText(e.target.value)} placeholder="如：有人一起吗？求搭子" />
                    </label>
                    <button type="button" className="btn" onClick={() => preview(testCommentText, testContextText)} disabled={testBusy || !testCommentText.trim()}>
                      {testBusy ? <Loader2 size={14} className="spin" /> : <FlaskConical size={14} />}
                      {testBusy ? '判定中…' : '运行判定'}
                    </button>
                  </div>
                  {testPreview && verdict && (
                    <div className="verdict" style={{ '--tier': verdict.tier } as CSSProperties}>
                      <span className="verdict-tag">{verdict.label}</span>
                      <dl>
                        <dt>主题</dt><dd>{testPreview.topic}</dd>
                        <dt>意向</dt><dd>{testPreview.intent}</dd>
                        <dt>态度</dt><dd>{testPreview.stance}</dd>
                        <dt>推荐分</dt><dd style={{ color: verdict.tier }}>{testPreview.score ?? '—'}</dd>
                      </dl>
                      <p>{testPreview.reason}</p>
                      {testPreview.scope_check && testPreview.scope_check.status !== 'matched' && (
                        <div className="scope">范围：{testPreview.scope_check.reasons.join('；')}</div>
                      )}
                    </div>
                  )}
                </Card>
              </div>
            </div>
          )}

          {tab === 'monitor' && (
            <RunMonitor
              run={runState}
              live={liveData}
              error={liveError}
              onSendToTest={handleSendToTest}
              onAddBlacklistWord={handleAddBlacklistWord}
              onRequestOpenLogin={handleOpenLogin}
            />
          )}

          {tab === 'history' && (
            <div className="history">
              <Card title="版本快照" desc="最新的在最上面，恢复后需要再次保存才会生效。" aside={`${versions.length} 个`}>
                {versions.length === 0 ? (
                  <div className="empty">暂无历史版本</div>
                ) : (
                  <div className="timeline">
                    {versions.map(v => (
                      <div key={v.revision} className="tl-item">
                        <div style={{ minWidth: 0 }}>
                          <b>{v.name}</b>
                          <small>{new Date(v.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })} · {v.revision.slice(0, 8)}</small>
                        </div>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={async () => {
                            if (!confirm(`确定载入版本「${v.name}」吗？`)) return
                            try {
                              loadDraft(await api<Bundle>(`/api/versions/${v.revision}`))
                              setTab('workbench')
                              setMessage(`已载入版本 ${v.revision.slice(0, 8)}，保存后生效`)
                            } catch (e) {
                              setError((e as Error).message)
                            }
                          }}
                        >
                          <RotateCcw size={12} />恢复
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <Card
                title="完整配置 JSON"
                desc="可直接编辑后校验载入，或导入导出文件做备份。"
                aside={
                  <div className="top-actions">
                    <input
                      ref={fileRef}
                      type="file"
                      accept=".json,application/json"
                      hidden
                      onChange={e => {
                        const f = e.target.files?.[0]
                        if (f) void importFile(f)
                        e.target.value = ''
                      }}
                    />
                    <button type="button" className="btn btn-sm" onClick={() => fileRef.current?.click()}><Upload size={12} />导入</button>
                    <button type="button" className="btn btn-sm" onClick={exportFile}><Download size={12} />导出</button>
                  </div>
                }
              >
                <textarea className="textarea json" value={jsonText} onChange={e => setJsonText(e.target.value)} rows={20} spellCheck={false} aria-label="配置 JSON" />
                <button
                  type="button"
                  className="btn"
                  style={{ marginTop: 12, width: '100%' }}
                  onClick={async () => {
                    try {
                      const parsed = JSON.parse(jsonText)
                      await api('/api/validate', { bundle: parsed })
                      loadDraft(parsed)
                      setMessage('JSON 校验通过，已载入草稿')
                    } catch (e) {
                      setError((e as Error).message)
                    }
                  }}
                >
                  校验并载入
                </button>
              </Card>
            </div>
          )}
        </main>
      </div>

      <div className="toasts" aria-live="polite">
        {message && (
          <div className="toast" role="status">
            <CheckCircle2 size={16} />
            <span>{message}</span>
            <button type="button" onClick={() => setMessage('')} aria-label="关闭提示"><X size={14} /></button>
          </div>
        )}
        {error && (
          <div className="toast error" role="alert">
            <AlertTriangle size={16} />
            <span>{error}</span>
            <button type="button" onClick={() => setError('')} aria-label="关闭错误"><X size={14} /></button>
          </div>
        )}
      </div>
    </div>
  )
}
