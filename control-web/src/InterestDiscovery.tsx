import { useRef, useState } from 'react'
import { AlertTriangle, ArrowLeft, ArrowRight, CheckCircle2, Clock3, ExternalLink, FileText, Loader2, Search, UserRound, X } from 'lucide-react'
import { platformNames } from './platforms'

type Api = <T,>(path: string, body?: unknown) => Promise<T>
type Metrics = {
  interest_score: number
  stance: string
  evidence_count: number
  independent_content_count: number
  active_days: number
  coverage: string
  trend: string
  last_activity: string | null
}
type Interest = Metrics & {
  topic: string
  profile_version: string
  updated_at: string
  synthetic: boolean
  windows?: Record<string, Metrics>
}
type Candidate = Metrics & {
  account_key: string
  account_id: string
  platform: string
  nickname: string
  profile_url: string
  topic: string
  updated_at: string
  synthetic: boolean
  representative_evidence?: { quote: string; source_url: string; published_display: string } | null
}
type SearchResult = {
  topic: string
  status: 'not_analyzed' | 'running' | 'completed' | 'failed'
  message?: string
  items: Candidate[]
  total: number
  limit: number
  offset: number
  analysis?: { stats?: { accounts?: number; candidate_contents?: number; empty_public_account_data?: boolean }; error?: string }
}
type Account = {
  account: { account_key: string; platform: string; account_id: string; nickname: string; profile_url: string }
  content_count: number
  active_days: number
  interests: Interest[]
  attribution_conflicts: Array<Record<string, unknown>>
  scope_note: string
}
type Evidence = Metrics & {
  account_key: string
  topic: string
  score_note: string
  items: Array<{
    id: number
    object_type: string
    platform: string
    quote: string
    full_text: string
    published_display: string
    relevance: number
    depth: number
    quality: number
    stance: string
    contribution?: number
    raw_score: number
    source_url: string
    quality_flags: string[]
    review_state: string
    review_note: string
  }>
}

const stanceNames: Record<string, string> = { positive: '正向', negative: '负向', neutral: '中性', mixed: '混合', unclear: '不明确' }
const trendNames: Record<string, string> = { rising: '近期上升', stable: '相对稳定', declining: '近期下降', insufficient_data: '数据不足' }
const coverageNames: Record<string, string> = { low: '证据较少', medium: '证据中等', high: '证据较充分', insufficient: '证据不足' }
const objectNames: Record<string, string> = { post: '原创作品', comment: '评论', reply: '评论回复', repost: '带观点转发' }
const shortDate = (value: string | null) => value ? value.slice(0, 10) : '时间未知'

export default function InterestDiscovery({ api }: { api: Api }) {
  const [topic, setTopic] = useState('')
  const [aliases, setAliases] = useState('')
  const [description, setDescription] = useState('')
  const [minScore, setMinScore] = useState(0)
  const [platform, setPlatform] = useState('')
  const [window, setWindow] = useState('all')
  const [stance, setStance] = useState('')
  const [minEvidence, setMinEvidence] = useState(1)
  const [sort, setSort] = useState('score')
  const [offset, setOffset] = useState(0)
  const [limit] = useState(12)
  const [result, setResult] = useState<SearchResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [selectedKey, setSelectedKey] = useState('')
  const [account, setAccount] = useState<Account | null>(null)
  const [detailBusy, setDetailBusy] = useState(false)
  const [selectedTopic, setSelectedTopic] = useState('')
  const [evidence, setEvidence] = useState<Evidence | null>(null)
  const [evidenceBusy, setEvidenceBusy] = useState(false)
  const [feedbackBusy, setFeedbackBusy] = useState(0)
  const requestId = useRef(0)

  async function search(nextOffset = 0, overrideTopic?: string) {
    const subject = (overrideTopic ?? topic).trim()
    if (!subject) { setError('请输入一个兴趣主题'); return }
    const id = ++requestId.current
    setBusy(true); setError(''); setNotice('')
    try {
      const params = new URLSearchParams({ topic: subject, min_score: String(minScore), platform, window, min_evidence: String(minEvidence), stance, sort, limit: String(limit), offset: String(nextOffset) })
      const data = await api<SearchResult>('/api/interests/search?' + params)
      if (id !== requestId.current) return
      setResult(data); setOffset(nextOffset)
    } catch (e) { if (id === requestId.current) setError(e instanceof Error ? e.message : '查询失败') }
    finally { if (id === requestId.current) setBusy(false) }
  }

  async function analyze() {
    if (!topic.trim()) { setError('请输入一个兴趣主题'); return }
    setBusy(true); setError(''); setNotice('')
    try {
      const data = await api<SearchResult>('/api/interests/analyze', {
        topic: topic.trim(), aliases: aliases.split(/[,，]/).map(v => v.trim()).filter(Boolean), description
      })
      setResult(data); setOffset(0)
      setNotice(data.analysis?.stats?.empty_public_account_data ? '当前没有可靠公开账号 ID 的内容；未生成虚构人物画像。' : '分析完成，可继续调整条件筛选，不会重新调用模型。')
    } catch (e) { setError(e instanceof Error ? e.message : '分析失败') }
    finally { setBusy(false) }
  }

  async function openAccount(key: string) {
    setSelectedKey(key); setAccount(null); setSelectedTopic(''); setEvidence(null); setDetailBusy(true); setError('')
    try { setAccount(await api<Account>('/api/accounts/' + encodeURIComponent(key))) }
    catch (e) { setError(e instanceof Error ? e.message : '人物详情读取失败') }
    finally { setDetailBusy(false) }
  }

  async function openEvidence(subject: string, key = selectedKey) {
    setSelectedTopic(subject); setEvidence(null); setEvidenceBusy(true); setError('')
    try { setEvidence(await api<Evidence>(`/api/accounts/${encodeURIComponent(key)}/interests/${encodeURIComponent(subject)}/evidence?window=${window}`)) }
    catch (e) { setError(e instanceof Error ? e.message : '证据读取失败') }
    finally { setEvidenceBusy(false) }
  }

  async function feedback(id: number, action: string) {
    setFeedbackBusy(id); setError('')
    try {
      const updated = await api<Account>('/api/interests/evidence-feedback', { evidence_id: id, action, note: action === 'restore' ? '恢复为有效证据' : '工作台人工标记' })
      setAccount(updated)
      if (selectedTopic) await openEvidence(selectedTopic)
      await search(offset)
      setNotice('人工反馈已保存，相关画像已重新计算。')
    } catch (e) { setError(e instanceof Error ? e.message : '反馈保存失败') }
    finally { setFeedbackBusy(0) }
  }

  const analysisStats = result?.analysis?.stats
  return <div className="interest-page">
    <section className="card">
      <div className="card-head"><div><div className="card-title"><h2>从任意兴趣发现公开账号</h2></div><p className="card-desc">仅分析已入库、具有稳定公开账号 ID 的内容；不推断匿名身份或跨平台同一人。</p></div></div>
      <div className="card-body">
        <div className="interest-topic-row">
          <label className="field interest-topic-input"><span className="field-label">兴趣主题</span><input className="input input-lg" value={topic} onChange={e => setTopic(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') search() }} placeholder="如：胶片摄影、徒步露营、古典音乐" /></label>
          <button type="button" className="btn btn-primary interest-analyze" onClick={analyze} disabled={busy || !topic.trim()}>{busy ? <Loader2 size={15} className="spin" /> : <Search size={15} />}分析已有数据</button>
          <button type="button" className="btn interest-query" onClick={() => search()} disabled={busy || !topic.trim()}>查询已有画像</button>
        </div>
        <div className="interest-advanced">
          <label className="field"><span className="field-label">主题别名<small>可选，逗号分隔</small></span><input className="input" value={aliases} onChange={e => setAliases(e.target.value)} placeholder="如：胶片相机、底片摄影" /></label>
          <label className="field"><span className="field-label">补充说明<small>随主题定义保存</small></span><input className="input" value={description} onChange={e => setDescription(e.target.value)} placeholder="描述你希望观察的实践或讨论" /></label>
        </div>
        <div className="interest-filters">
          <label className="field"><span className="field-label">最低指数</span><input className="input" type="number" min={0} max={100} value={minScore} onChange={e => setMinScore(Number(e.target.value))} /></label>
          <label className="field"><span className="field-label">平台</span><select className="select" value={platform} onChange={e => setPlatform(e.target.value)}><option value="">全部平台</option>{Object.entries(platformNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
          <label className="field"><span className="field-label">时间窗口</span><select className="select" value={window} onChange={e => setWindow(e.target.value)}><option value="all">全部已知历史</option><option value="30">近 30 天</option><option value="90">近 90 天</option><option value="180">近 180 天</option></select></label>
          <label className="field"><span className="field-label">态度</span><select className="select" value={stance} onChange={e => setStance(e.target.value)}><option value="">全部态度</option><option value="positive">正向</option><option value="negative">负向</option><option value="neutral">中性</option><option value="mixed">混合</option></select></label>
          <label className="field"><span className="field-label">最少证据</span><input className="input" type="number" min={0} max={1000} value={minEvidence} onChange={e => setMinEvidence(Number(e.target.value))} /></label>
          <label className="field"><span className="field-label">排序</span><select className="select" value={sort} onChange={e => setSort(e.target.value)}><option value="score">兴趣指数</option><option value="recent">近期活跃</option><option value="evidence">证据数量</option></select></label>
          <button type="button" className="btn interest-apply" onClick={() => search()} disabled={busy || !topic.trim()}>应用筛选</button>
        </div>
        <p className="interest-method-note">指数表示已观察到的兴趣参与程度，不是概率、人格属性或正向好感。参与强度、态度和证据充分程度分别展示。</p>
      </div>
    </section>

    {error && <div className="interest-alert error" role="alert"><AlertTriangle size={16} /><span>{error}</span><button className="btn btn-ghost btn-sm icon-btn" onClick={() => setError('')} aria-label="关闭错误"><X size={14} /></button></div>}
    {notice && <div className="interest-alert"><CheckCircle2 size={16} /><span>{notice}</span></div>}

    <section className="card">
      <div className="card-head"><div><div className="card-title"><h2>人物结果</h2></div><p className="card-desc">分数旁即可看到一条代表性原始证据，点击账号查看完整画像。</p></div>{result && <span className="card-aside">匹配 {result.total} 个账号</span>}</div>
      <div className="card-body interest-results-body">
        {!result && <div className="empty"><UserRound size={28} /><b>先输入兴趣主题</b><span>查询只读取已分析画像；开始分析才会处理当前已有数据。<br />进入本页面不会自动全库分析或请求外部模型。</span></div>}
        {busy && <div className="empty"><Loader2 className="spin" size={26} /><span>正在处理，请稍候…</span></div>}
        {result && !busy && result.status === 'not_analyzed' && <div className="empty"><Search size={28} /><b>“{result.topic}”尚未分析</b><span>这不代表没有人感兴趣。点击“分析已有数据”后再查看候选账号。</span></div>}
        {result && !busy && result.status === 'failed' && <div className="empty"><AlertTriangle size={28} /><b>分析失败</b><span>{result.analysis?.error || '请稍后重试。'}</span></div>}
        {result && !busy && result.status !== 'not_analyzed' && result.status !== 'failed' && result.total === 0 && <div className="empty"><FileText size={28} /><b>{analysisStats?.empty_public_account_data ? '暂无可靠公开账号数据' : '已分析，当前条件下无匹配'}</b><span>{analysisStats?.empty_public_account_data ? '历史匿名内容不能转化为已识别人物。等待采集源提供稳定账号 ID 后再分析。' : '可降低分数或证据阈值、切换时间窗口；无匹配不等于没有兴趣。'}</span></div>}
        {result && !busy && result.items.length > 0 && <div className="candidate-list">{result.items.map(item => <article className="candidate" key={item.account_key}>
          <div className="candidate-main">
            <div className="candidate-identity"><span className="account-avatar"><UserRound size={19} /></span><div><button className="candidate-name" onClick={() => openAccount(item.account_key)}>{item.nickname || '公开账号'}</button><span className="candidate-id mono">{platformNames[item.platform] || item.platform} · {item.account_key}</span></div>{item.synthetic && <span className="data-badge">合成测试</span>}</div>
            <div className="candidate-metrics"><span className={`stance ${item.stance}`}>{stanceNames[item.stance] || item.stance}</span><span>{item.evidence_count} 条有效证据</span><span>{coverageNames[item.coverage]}</span><span><Clock3 size={12} />{shortDate(item.last_activity)}</span></div>
            {item.representative_evidence && <blockquote className="candidate-quote">“{item.representative_evidence.quote}”{item.representative_evidence.source_url && <a href={item.representative_evidence.source_url} target="_blank" rel="noreferrer" aria-label="打开公开来源"><ExternalLink size={13} /></a>}</blockquote>}
          </div>
          <button className="score-block" onClick={() => { openAccount(item.account_key); void openEvidence(item.topic, item.account_key) }} aria-label={`查看 ${item.account_key} 的 ${item.topic} 评分证据`}><strong>{item.interest_score}</strong><span>兴趣参与指数</span><small>查看评分依据 <ArrowRight size={12} /></small></button>
        </article>)}</div>}
        {result && result.total > limit && <div className="pagination"><button className="btn btn-sm" disabled={offset === 0 || busy} onClick={() => search(Math.max(0, offset - limit))}><ArrowLeft size={13} />上一页</button><span>第 {Math.floor(offset / limit) + 1} / {Math.ceil(result.total / limit)} 页</span><button className="btn btn-sm" disabled={offset + limit >= result.total || busy} onClick={() => search(offset + limit)}>下一页<ArrowRight size={13} /></button></div>}
      </div>
    </section>

    {(selectedKey || detailBusy) && <div className="drawer-backdrop" onClick={() => { setSelectedKey(''); setAccount(null); setEvidence(null) }}>
      <aside className="profile-drawer" role="dialog" aria-modal="true" aria-label="人物兴趣画像" onClick={e => e.stopPropagation()}>
        <div className="drawer-head"><div><span className="eyebrow">人物兴趣画像</span><h2>{account?.account.nickname || selectedKey}</h2><p className="mono">{selectedKey}</p></div><button className="btn btn-ghost btn-sm icon-btn" onClick={() => { setSelectedKey(''); setAccount(null); setEvidence(null) }} aria-label="关闭人物画像"><X size={18} /></button></div>
        {detailBusy && <div className="empty"><Loader2 size={26} className="spin" />正在读取画像…</div>}
        {account && <div className="drawer-content">
          <div className="account-summary"><div><strong>{account.content_count}</strong><span>有效内容</span></div><div><strong>{account.active_days}</strong><span>活跃天数</span></div><div><strong>{account.interests.filter(i => i.evidence_count > 0).length}</strong><span>有证据兴趣</span></div></div>
          <p className="scope-note">{account.scope_note}</p>
          {account.attribution_conflicts.length > 0 && <div className="interest-alert error"><AlertTriangle size={15} />有 {account.attribution_conflicts.length} 项身份归属待人工确认，冲突内容不参与计分。</div>}
          {account.account.profile_url && <a className="btn btn-sm" href={account.account.profile_url} target="_blank" rel="noreferrer"><ExternalLink size={13} />公开主页</a>}
          <h3 className="drawer-section-title">已分析的兴趣主题</h3>
          {account.interests.filter(i => i.evidence_count > 0).length === 0 ? <div className="empty compact"><FileText size={22} /><span>暂无有效兴趣证据，不生成虚构画像。</span></div> : <div className="profile-interest-list">{account.interests.filter(i => i.evidence_count > 0).map(interest => <button key={interest.topic} className={`profile-interest ${selectedTopic === interest.topic ? 'selected' : ''}`} onClick={() => openEvidence(interest.topic)}>
            <div><strong>{interest.topic}</strong><span>{stanceNames[interest.stance]} · {coverageNames[interest.coverage]} · {trendNames[interest.trend]}</span><small>{interest.evidence_count} 条证据 · 更新于 {shortDate(interest.updated_at)}</small></div><b>{interest.interest_score}</b>
          </button>)}</div>}
          {selectedTopic && <section className="evidence-section"><div className="evidence-heading"><div><h3>{selectedTopic} · 评分依据</h3><p>{evidence ? `${evidence.evidence_count} 条计入当前窗口 · ${coverageNames[evidence.coverage]}` : '正在读取…'}</p></div>{evidence && <strong>{evidence.interest_score}</strong>}</div>
            {evidenceBusy && <div className="empty compact"><Loader2 size={22} className="spin" />正在读取证据…</div>}
            {evidence && <><p className="scope-note">{evidence.score_note}</p><div className="evidence-list">{evidence.items.length === 0 && <div className="empty compact">当前时间窗口没有证据，请切换全部已知历史。</div>}{evidence.items.map(item => <article className={`evidence-item ${item.review_state !== 'accepted' ? 'excluded' : ''}`} key={item.id}>
              <div className="evidence-meta"><span>{platformNames[item.platform] || item.platform} · {objectNames[item.object_type] || item.object_type}</span><span>{item.published_display === '时间未知' ? '时间未知' : shortDate(item.published_display)}</span><span className={`stance ${item.stance}`}>{stanceNames[item.stance]}</span><b>贡献 {item.contribution ?? 0}</b></div>
              <blockquote>“{item.quote}”</blockquote>
              <div className="evidence-quality">相关性 {Math.round(item.relevance * 100)}% · 参与深度 {Math.round(item.depth * 100)}% · 证据质量 {Math.round(item.quality * 100)}%</div>
              {item.quality_flags.length > 0 && <div className="quality-flags">{item.quality_flags.map(flag => <span key={flag}>{flag === 'published_time_unknown' ? '时间未知，质量降权' : flag === 'stance_not_expressed' ? '未明确表达态度' : flag}</span>)}</div>}
              {item.review_state !== 'accepted' && <p className="review-note">已人工标记：{item.review_state}，当前不计分。</p>}
              <div className="evidence-actions">{item.source_url && <a className="btn btn-ghost btn-sm" href={item.source_url} target="_blank" rel="noreferrer"><ExternalLink size={12} />公开来源</a>}{item.review_state === 'accepted' ? <><button className="btn btn-sm" disabled={feedbackBusy === item.id} onClick={() => feedback(item.id, 'irrelevant')}>不相关</button><button className="btn btn-sm" disabled={feedbackBusy === item.id} onClick={() => feedback(item.id, 'stance_error')}>态度判断错误</button><button className="btn btn-sm" disabled={feedbackBusy === item.id} onClick={() => feedback(item.id, 'attribution_question')}>身份归属有疑问</button></> : <button className="btn btn-sm" disabled={feedbackBusy === item.id} onClick={() => feedback(item.id, 'restore')}>恢复为有效证据</button>}</div>
            </article>)}</div></>}
          </section>}
        </div>}
      </aside>
    </div>}
  </div>
}
